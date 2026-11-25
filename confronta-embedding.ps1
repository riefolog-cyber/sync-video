# Confronto A/B dei modelli embedding (fastembed) sulla pipeline reale.
# Lancia un dry-run del flusso ordinato (--flow slide-audio) per ogni
# modello e stampa una tabella comparativa: similarità media, durata min/max,
# anomalie (slide troppo corte) e tempi. In coda suggerisce la scelta migliore.
#
# Uso:  pwsh .\confronta-embedding.ps1
#       pwsh .\confronta-embedding.ps1 -Models "e5-large" -AnomalyThresholdSec 20
#       pwsh .\confronta-embedding.ps1 -KeepEnv
#
# Con -KeepEnv al termine resta impostato il modello dell'ultima prova: la
# variabile NON viene ripristinata al valore iniziale. Senza -KeepEnv, invece,
# si torna al valore iniziale di EMBEDDING_MODEL (o la si rimuove se non era
# impostata).
#
# I modelli supportati (nome breve -> nome fastembed):
#   e5-large, mpnet, minilm
param(
    [string[]]$Models = @("e5-large", "mpnet", "minilm"),
    [int]$AnomalyThresholdSec = 30,
    [string]$Flow = "slide-audio",
    [switch]$KeepEnv
)

$ErrorActionPreference = "Stop"

$MODEL_MAP = @{
    "e5-large" = "intfloat/multilingual-e5-large"
    "mpnet"    = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
    "minilm"   = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
}

function Get-Durations([string]$Output) {
    # La timeline è stampata 2 volte nel log: uso solo l'ULTIMA stampa,
    # così i valori non si duplicano. Il vecchio taglio su "similarit" non
    # esiste più nel formato attuale del log.
    $durations = @()
    $idx = $Output.LastIndexOf("-> Slide")
    if ($idx -ge 0) { $Output = $Output.Substring($idx) }
    foreach ($m in [regex]::Matches($Output, '-> Slide \d+: da [\d.]+s a [\d.]+s \(durata: ([\d.]+)s\)')) {
        $durations += [double]$m.Groups[1].Value
    }
    return $durations
}

function Get-Sim([string]$Output) {
    # Il programma stampa "somiglianza media 0.836" (non più "similarità
    # media"), e con tutti i cambi ancorati la misura non c'è affatto: in
    # quel caso si usa il punteggio z, che è il numero che conta davvero.
    $m = [regex]::Match($Output, 'somiglianza media\s+([\d]+\.[\d]+)')
    if (-not $m.Success) { $m = [regex]::Match($Output, 'punteggio\s+([\d]+\.[\d]+)') }
    if ($m.Success) { return [double]$m.Groups[1].Value }
    return $null
}

function Get-Time([string]$Output, [string]$Label) {
    # Formato valore: "Xs" oppure "NmXs" (da _format_time di main.py).
    # Ancora alla fine riga (i log sono prefissati con "INFO | ...").
    $m = [regex]::Match($Output, '(?m)' + [regex]::Escape($Label) + '[^\d]*([0-9]+[mh]?[0-9]*s)[^\d]*$')
    if ($m.Success) { return $m.Groups[1].Value }
    return "-"
}

Push-Location $PSScriptRoot

# Usa l'ambiente dedicato del progetto quando c'e': e' li' che vivono le
# dipendenze pinnate. Senza .venv si ripiega sul python del PATH.
$PYTHON = "python"
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".venv\Scripts\python.exe")) {
    $PYTHON = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
}

# Salva il valore iniziale: di default a fine run si torna a questo (o si
# rimuove la variabile se non era impostata). Con -KeepEnv, invece, resta il
# modello dell'ultima prova e il backup non viene ripristinato.
$envBackup = $env:EMBEDDING_MODEL

$results = @()
foreach ($short in $Models) {
    $short = $short.ToLower()
    if (-not $MODEL_MAP.ContainsKey($short)) {
        Write-Host "Modello sconosciuto: $short (attesi: $($MODEL_MAP.Keys -join ', '))" -ForegroundColor Yellow
        continue
    }
    $modelName = $MODEL_MAP[$short]

    Write-Host ""
    Write-Host "=== Modello: $short ($modelName) ===" -ForegroundColor Cyan

    $env:EMBEDDING_MODEL = $modelName
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    # Nessuna domanda: lo script è un confronto automatico e non deve fermarsi
    # su PyPI/bootstrap come succedeva con la sola invocazione --dry-run.
    $raw = & $PYTHON src\main.py --dry-run --debug --flow $Flow --no-update-check --no-confirm 2>&1 | Out-String
    $sw.Stop()

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERRORE nell'esecuzione per $short (exit $LASTEXITCODE)" -ForegroundColor Red
        $results += [PSCustomObject]@{
            Modello = $short
            SimMedia = $null
            DurMin = $null
            DurMax = $null
            DurMedie = ""
            Anomalie = "ERRORE"
            Sync = "-"
            Embedding = "-"
            Totale = "-"
        }
        continue
    }

    $durations = Get-Durations -Output $raw
    $sim = Get-Sim -Output $raw

    $anomalies = @($durations | Where-Object { $_ -lt $AnomalyThresholdSec })
    $durMin = if ($durations.Count) { ($durations | Measure-Object -Minimum).Minimum } else { $null }
    $durMax = if ($durations.Count) { ($durations | Measure-Object -Maximum).Maximum } else { $null }
    $avg    = if ($durations.Count) { [math]::Round((($durations | Measure-Object -Average).Average), 1) } else { $null }

    Write-Host ("  similarità media : {0}" -f $(if ($null -ne $sim) { "{0:N3}" -f $sim } else { "-" }))
    Write-Host ("  durate          : {0}" -f (($durations | ForEach-Object { "$($_)s" }) -join ", "))
    Write-Host ("  durata min/max  : {0}s / {1}s  (media {2}s)" -f $durMin, $durMax, $avg)
    if ($anomalies.Count) {
        Write-Host "  ANOMALIE         : $($anomalies.Count) slide sotto ${AnomalyThresholdSec}s ($(($anomalies | ForEach-Object { "$($_)s" }) -join ', '))" -ForegroundColor Yellow
    } else {
        Write-Host "  anomalie         : nessuna" -ForegroundColor Green
    }
    Write-Host ("  tempi           : sincroniz. {0} | embedding {1} | totale {2} (wall {3}s)" -f `
        (Get-Time $raw "Sincronizzaz"), (Get-Time $raw "Embedding"), (Get-Time $raw "TOTALE"), $sw.Elapsed.TotalSeconds)

    $results += [PSCustomObject]@{
        Modello = $short
        SimMedia = $sim
        DurMin = $durMin
        DurMax = $durMax
        DurMedie = $avg
        Anomalie = if ($anomalies.Count) { $anomalies.Count.ToString() } else { "0" }
        Sync = Get-Time $raw "Sincronizzaz"
        Embedding = Get-Time $raw "Embedding"
        Totale = Get-Time $raw "TOTALE"
    }
}

# Ripristina l'ambiente solo se non è stato chiesto di tenere l'ultimo modello.
if (-not $KeepEnv) {
    if ($null -ne $envBackup) {
        $env:EMBEDDING_MODEL = $envBackup
    } else {
        Remove-Item Env:EMBEDDING_MODEL -ErrorAction SilentlyContinue
    }
}

Pop-Location

Write-Host ""
Write-Host "===============================" -ForegroundColor Cyan
Write-Host " TABELLA COMPARATIVA" -ForegroundColor Cyan
Write-Host "===============================" -ForegroundColor Cyan
$results | Format-Table -AutoSize |
    Out-String -Width 120 |
    ForEach-Object { Write-Host $_.TrimEnd("`r`n") }

# Raccomandazione: zero anomalie > sim media più alta
$candidates = $results | Where-Object { $_.Anomalie -eq "0" -and $null -ne $_.SimMedia }
if (-not $candidates) { $candidates = $results | Where-Object { $null -ne $_.SimMedia } }
if ($candidates) {
    $best = $candidates | Sort-Object SimMedia -Descending | Select-Object -First 1
    Write-Host ""
    Write-Host "Suggerimento: $($best.Modello) (sim media $(('{0:N3}' -f $best.SimMedia)), anomalie $($best.Anomalie))" -ForegroundColor Green
} else {
    Write-Host "Nessun modello valutabile." -ForegroundColor Yellow
}
