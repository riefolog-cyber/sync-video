# Converte un .pptx in .pdf usando Microsoft PowerPoint via COM.
#
# Perche' uno script e non una riga in Python: serve pilotare COM, e in
# Python quello richiederebbe pywin32, che il progetto non vuole come
# dipendenza. PowerShell c'e' gia' su ogni Windows e parla COM nativamente,
# quindi questo file evita di aggiungere un pacchetto.
#
# Uso (lo chiama ocr.py, non va lanciato a mano):
#   powershell -NoProfile -ExecutionPolicy Bypass -File <questo> <pptx> <pdf>
#
# Restituisce 0 se il PDF e' stato scritto, 1 altrimenti. Il successo si
# valuta in ocr.py controllando la presenza del file, per coerenza con gli
# altri converter.

# Solo ASCII e senza BOM: PowerShell 5.1 legge i .ps1 in ANSI se non c'e' il
# BOM, quindi un file UTF-8 accentato verrebbe letto con caratteri sbagliati.
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Dest
)

$ErrorActionPreference = 'Stop'
$pp = $null
$pres = $null

try {
    $pp = New-Object -ComObject PowerPoint.Application

    # Open(FileName, ReadOnly, Untitled, WithWindow):
    #   ReadOnly  = -1 (msoTrue)  non modifica il file originale
    #   Untitled  =  0 (msoFalse)
    #   WithWindow = 0 (msoFalse) niente finestra: puo' fallire su Office
    #               dove l'applicazione non puo' restare invisibile, quindi
    #               in quel caso si prosegue senza WithWindow.
    try {
        $pres = $pp.Presentations.Open($Source, -1, 0, 0)
    } catch {
        Write-Output "Apertura senza finestra non riuscita, riprovo con finestra: $($_.Exception.Message)"
        $pres = $pp.Presentations.Open($Source, -1, 0, -1)
    }

    # 32 = ppSaveAsPDF. La costante e' gia' nella libreria di PowerPoint,
    # quindi il numero e' quello documentato da Microsoft.
    $pres.SaveAs($Dest, 32)
    $pres.Close()
    Write-Output "PDF scritto: $Dest"
    exit 0
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
finally {
    # Senza questo PowerPoint resta aperto in background e l'utente si
    # ritrova l'app avviata dopo aver chiuso il programma.
    if ($pp -ne $null) {
        try { $pp.Quit() } catch { }
    }
    if ($pres -ne $null) {
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($pres) } catch { }
    }
    if ($pp -ne $null) {
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($pp) } catch { }
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}