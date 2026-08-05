param(
    [string]$ComboName = "comboact",
    [string]$BaseUrl = "http://localhost:20128",
    [int]$TimeoutSec = 30,
    [int]$Retries = 2,
    [switch]$DryRun,
    [string]$Token = "",
    [switch]$Force,
    [string]$LogFile = "",
    [int]$MaxConsecutiveFails = 3,
    [int]$LogRetentionDays = 30,
    [switch]$AddFreeModels,
    [switch]$AutoReplace,
    [string]$StateFile = ""
)

$ErrorActionPreference = "Stop"

# ============================ Constants ============================

# Categorical errors = will never work without changing account/credit/model availability
$CATEGORICAL = @(400, 401, 402, 403, 404, 405, 406, 409, 410, 415, 422)
# Temporary errors = retry, then keep if still failing
$RETRYABLE = @(408, 429, 500, 502, 503, 504, 529)

# Patterns in error text that indicate a permanent (categorical) failure
$CATEGORICAL_PATTERNS = @(
    'model_not_found',
    'no longer available',
    'has reached its end of life',
    'was retired',
    'is no longer available'
)

# Free models to add when -AddFreeModels is used (only free, zero-cost providers, already exposed by 9router)
# NOTE: OpenRouter free models discovered via official API were checked against 9router's model list:
# gpt-oss-20b:free, nemotron-nano-*:free, nemotron-3.5-content-safety:free are NOT exposed by 9router,
# so they cannot be used. Only models actually present in 9router's /api/models are candidates.
$FREE_MODELS_TO_ADD = @(
    # Google Gemini (free tier via AI Studio)
    'gemini/gemini-2.0-flash',
    # Kimchi (free with spend limit/budget - may reset)
    'kimchi/kimi-k2.6',
    'kimchi/minimax-m2.7',
    'kimchi/claude-opus-4-6',
    'kimchi/claude-sonnet-4-6'
)

# ============================ Default paths ============================

if (-not $LogFile) {
    $LogFile = Join-Path $PSScriptRoot "logs\comboact-update-$(Get-Date -Format 'yyyy-MM-dd').log"
}
if (-not $StateFile) {
    $StateFile = Join-Path $PSScriptRoot "logs\comboact-state.json"
}

# ============================ Functions ============================

function Write-Log([string]$message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $message"
    Write-Output $line
    $logDir = Split-Path -Parent $LogFile
    if ($logDir -and -not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    Add-Content -LiteralPath $LogFile -Value $line
}

function Clean-LogText([string]$text) {
    if (-not $text) { return "" }
    $clean = $text -replace "`r`n|`r|`n", " | "
    $clean = $clean -replace "`t", " "
    $clean = $clean -replace '\s+', ' '
    if ($clean.Length -gt 300) { $clean = $clean.Substring(0, 300) + "..." }
    return $clean.Trim()
}

function Rotate-Logs {
    $logDir = Split-Path -Parent $LogFile
    if (-not $logDir -or -not (Test-Path -LiteralPath $logDir)) { return }
    $cutoff = (Get-Date).AddDays(-$LogRetentionDays)
    Get-ChildItem -Path $logDir -Filter "comboact-update-*.log" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

function Load-State {
    if (-not (Test-Path -LiteralPath $StateFile)) { return @{} }
    try {
        $ht = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json -AsHashtable
        if ($null -eq $ht) { return @{} }
        return $ht
    } catch { return @{} }
}

function Save-State([hashtable]$state) {
    $stateDir = Split-Path -Parent $StateFile
    if ($stateDir -and -not (Test-Path -LiteralPath $stateDir)) {
        New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $StateFile -Encoding UTF8
}

function Get-Combo([string]$name) {
    $params = @{ Uri = "$BaseUrl/api/combos"; Method = "Get"; TimeoutSec = $TimeoutSec }
    if ($Token) { $params.Headers = @{ Authorization = "Bearer $Token" } }
    $resp = Invoke-RestMethod @params
    return $resp.combos | Where-Object { $_.name -eq $name } | Select-Object -First 1
}

function Get-AvailableModels {
    $params = @{ Uri = "$BaseUrl/api/models"; Method = "Get"; TimeoutSec = $TimeoutSec }
    if ($Token) { $params.Headers = @{ Authorization = "Bearer $Token" } }
    $resp = Invoke-RestMethod @params
    return $resp.models
}

function Find-Replacement([string]$errorText, $availableModels) {
    if (-not $errorText) { return $null }
    if ($errorText -match '"replacement"\s*:\s*"([^"]+)"') {
        $replacementName = $matches[1]
        $match = $availableModels |
            Where-Object { $_.model -like "*$replacementName*" -or $_.fullModel -like "*$replacementName*" } |
            Select-Object -First 1
        if ($match) { return $match.routedModel }
    }
    return $null
}

# ============================ Main ============================

Rotate-Logs
Write-Log "========== Starting combo maintenance =========="
if ($DryRun) { Write-Log "DRY-RUN mode: no changes will be written" }
Write-Log "Params: MaxConsecutiveFails=$MaxConsecutiveFails AddFreeModels=$([bool]$AddFreeModels) AutoReplace=$([bool]$AutoReplace)"
Write-Log "Log: $LogFile | State: $StateFile"

# Load persistent state
$state = Load-State
Write-Log "Loaded state for $($state.Count) models"

# Get combo
$combo = Get-Combo $ComboName
if (-not $combo) {
    Write-Log "ERROR: combo '$ComboName' not found"
    exit 1
}
if (-not $combo.models) {
    Write-Log "ERROR: combo '$ComboName' has no models (id=$($combo.id))"
    exit 1
}
Write-Log "Found combo '$ComboName' with $($combo.models.Count) models (id=$($combo.id))"

# Get available models (for -AddFreeModels and -AutoReplace)
$availableModels = $null
if ($AddFreeModels -or $AutoReplace) {
    try {
        $availableModels = Get-AvailableModels
        Write-Log "Fetched $($availableModels.Count) available models from 9router"
    } catch {
        Write-Log "WARNING: could not fetch available models: $($_.Exception.Message)"
    }
}

# Build model list to test
$modelsToTest = [System.Collections.Generic.List[string]]::new()
foreach ($m in $combo.models) { $modelsToTest.Add($m) }

# Add free models
if ($AddFreeModels -and $availableModels) {
    $availableFullNames = @($availableModels | ForEach-Object { $_.fullModel; $_.routedModel })
    $added = @()
    foreach ($fm in $FREE_MODELS_TO_ADD) {
        if ($fm -in $availableFullNames -and $fm -notin $modelsToTest) {
            $modelsToTest.Add($fm)
            $added += $fm
        }
    }
    if ($added.Count -gt 0) {
        Write-Log "Adding $($added.Count) free model candidates for testing"
        foreach ($a in $added) { Write-Log "  + FREE  $a" }
    } else {
        Write-Log "No new free models to add (all already in combo or not available)"
    }
}

# Test all models in parallel
Write-Log "Testing $($modelsToTest.Count) models (parallel, throttle 8)..."
$results = @($modelsToTest | ForEach-Object -Parallel {
    $model = $_
    $body = @{ model = $model; kind = "llm" } | ConvertTo-Json -Compress
    $reqParams = @{
        Uri                = "$using:BaseUrl/api/models/test"
        Method             = "Post"
        ContentType        = "application/json"
        Body               = $body
        TimeoutSec         = $using:TimeoutSec
        SkipHttpErrorCheck = $true
    }
    if ($using:Token) { $reqParams.Headers = @{ Authorization = "Bearer $using:Token" } }

    $attempt = 0
    $done = $false
    while (-not $done) {
        $status = 0
        $ok = $false
        $errorText = ""
        try {
            $r = Invoke-WebRequest @reqParams
            $httpStatus = [int]$r.StatusCode
            $j = $null
            try { $j = $r.Content | ConvertFrom-Json } catch { $j = $null }
            if ($j) {
                $ok = [bool]$j.ok
                $status = if ($null -ne $j.status) { [int]$j.status } else { $httpStatus }
                $errorText = [string]$j.error
            }
            else {
                $status = $httpStatus
                $errorText = "HTTP $httpStatus"
            }
        }
        catch {
            $errorText = "REQUEST-FAILED: $($_.Exception.Message)"
        }

        if ($ok) {
            [PSCustomObject]@{ Model = $model; Ok = $true; Status = $status; Error = "" }
            $done = $true
        }
        elseif ((($using:RETRYABLE -contains $status) -or ($status -eq 0 -and $errorText -like "REQUEST-FAILED*")) -and $attempt -lt $using:Retries) {
            Start-Sleep -Seconds (2 * ($attempt + 1))
            $attempt++
        }
        else {
            [PSCustomObject]@{ Model = $model; Ok = $false; Status = $status; Error = $errorText }
            $done = $true
        }
    }
} -ThrottleLimit 8)

# Update persistent state
# NOTE: 429 (rate-limit) and 503 (temporarily unavailable) are quota/availability errors that
# reset daily. They must NOT accumulate as chronic failures, otherwise free models that are
# simply rate-limited (e.g. OpenRouter 50 free req/day) would be wrongly removed after 3 days.
$today = Get-Date -Format 'yyyy-MM-dd'
foreach ($r in $results) {
    $cleanError = Clean-LogText $r.Error
    if ($r.Ok) {
        $state[$r.Model] = @{
            consecutiveFails = 0
            lastOk           = $today
            lastError        = ""
            lastStatus       = [int]$r.Status
        }
    }
    else {
        $prev = $state[$r.Model]
        $prevFails = if ($prev -and $prev.ContainsKey('consecutiveFails')) { [int]$prev.consecutiveFails } else { 0 }
        $prevOk = if ($prev -and $prev.ContainsKey('lastOk')) { [string]$prev.lastOk } else { "" }
        # Rate-limit (429) and temporarily unavailable (503) reset daily - do not count as chronic
        $isQuotaError = ($r.Status -eq 429) -or ($r.Status -eq 503)
        $newFails = if ($isQuotaError) { 0 } else { $prevFails + 1 }
        $state[$r.Model] = @{
            consecutiveFails = $newFails
            lastOk           = $prevOk
            lastError        = $cleanError
            lastStatus       = [int]$r.Status
        }
    }
}

# Classify results
$classResults = @()
foreach ($r in $results) {
    if ($r.Ok) {
        $classResults += [PSCustomObject]@{
            Model = $r.Model; Ok = $true; Status = $r.Status; Error = $r.Error
            Remove = $false; Reason = "OK"; Fails = 0
        }
        continue
    }
    $isCategorical = $CATEGORICAL -contains $r.Status
    if (-not $isCategorical -and $r.Error) {
        foreach ($pattern in $CATEGORICAL_PATTERNS) {
            if ($r.Error -match $pattern) { $isCategorical = $true; break }
        }
    }
    $consecutiveFails = 1
    if ($state.ContainsKey($r.Model) -and $state[$r.Model].ContainsKey('consecutiveFails')) {
        $consecutiveFails = [int]$state[$r.Model].consecutiveFails
    }
    $isChronic = $consecutiveFails -ge $MaxConsecutiveFails
    $remove = $isCategorical -or $isChronic
    $reason = if ($isCategorical) {
        "CATEGORICAL"
    } elseif ($isChronic) {
        "CHRONIC($consecutiveFails/$MaxConsecutiveFails)"
    } else {
        "TEMPORARY($consecutiveFails/$MaxConsecutiveFails)"
    }
    $classResults += [PSCustomObject]@{
        Model = $r.Model; Ok = $false; Status = $r.Status; Error = $r.Error
        Remove = $remove; Reason = $reason; Fails = $consecutiveFails
    }
}

$working   = @($classResults | Where-Object { $_.Ok })
$temporary = @($classResults | Where-Object { -not $_.Ok -and -not $_.Remove })
$toRemove  = @($classResults | Where-Object { -not $_.Ok -and $_.Remove })

Write-Log "Working: $($working.Count) | Temporary (kept): $($temporary.Count) | To remove: $($toRemove.Count)"

# Log removals
foreach ($m in $toRemove) {
    $cleanErr = Clean-LogText $m.Error
    Write-Log "  REMOVE  [$($m.Status)] $($m.Model)  $($m.Reason)  $cleanErr"
}
# Log temporary (kept)
foreach ($m in $temporary) {
    $cleanErr = Clean-LogText $m.Error
    Write-Log "  KEEP    [$($m.Status)] $($m.Model)  $($m.Reason)  $cleanErr"
}

# Auto-replace retired models
$replacements = @()
if ($AutoReplace -and $availableModels) {
    foreach ($r in $toRemove) {
        $rep = Find-Replacement $r.Error $availableModels
        if ($rep) {
            $replacements += $rep
            Write-Log "  REPLACE $($r.Model) -> $rep"
        }
    }
}

# Build new model list
$removeNames = @($toRemove | ForEach-Object { $_.Model })
$newModels = @($classResults |
    Where-Object { $_.Model -notin $removeNames } |
    ForEach-Object { $_.Model })
if ($replacements.Count -gt 0) {
    $newModels += $replacements
}
$newModels = @($newModels | Select-Object -Unique)

# Sort by reliability: 0 consecutiveFails first, then alphabetical
$newModels = @($newModels | Sort-Object `
    @{Expression = {
        $s = $state[$_]
        if ($s -and $s.ContainsKey('consecutiveFails')) { [int]$s.consecutiveFails } else { 0 }
    }; Ascending = $true},
    { $_ }
)

# Clean up stale state entries (models no longer in combo)
$newSet = [System.Collections.Generic.HashSet[string]]::new([string[]]$newModels)
$staleKeys = @($state.Keys | Where-Object { -not $newSet.Contains($_) })
foreach ($key in $staleKeys) {
    $state.Remove($key) | Out-Null
}
if ($staleKeys.Count -gt 0) {
    Write-Log "Cleaned $($staleKeys.Count) stale state entries"
}

# Check for empty combo
if ($newModels.Count -eq 0 -and -not $Force) {
    Write-Log "ERROR: all models would be removed, combo would be empty (use -Force to allow)"
    Save-State $state
    exit 1
}

# Check if anything changed
$comboChanged = ($toRemove.Count -gt 0) -or ($replacements.Count -gt 0)
if ($AddFreeModels) {
    $originalSet = [System.Collections.Generic.HashSet[string]]::new([string[]]@($combo.models))
    foreach ($m in $newModels) {
        if (-not $originalSet.Contains($m)) { $comboChanged = $true; break }
    }
}

if (-not $comboChanged) {
    Write-Log "Nothing to change, combo left unchanged"
    Save-State $state
    exit 0
}

# Count added models for logging
$addedCount = 0
$origSet = [System.Collections.Generic.HashSet[string]]::new([string[]]@($combo.models))
foreach ($m in $newModels) {
    if (-not $origSet.Contains($m)) { $addedCount++ }
}

if ($DryRun) {
    Write-Log "DRY-RUN: would remove $($toRemove.Count), keep $($newModels.Count) (added $addedCount free, replaced $($replacements.Count))"
    Save-State $state
    exit 0
}

# Update combo (set kind="llm" for proper LLM routing)
$body = @{ name = $combo.name; kind = "llm"; models = $newModels } | ConvertTo-Json -Depth 5
$updateParams = @{
    Uri         = "$BaseUrl/api/combos/$($combo.id)"
    Method      = "Put"
    ContentType = "application/json"
    Body        = $body
    TimeoutSec  = $TimeoutSec
}
if ($Token) { $updateParams.Headers = @{ Authorization = "Bearer $Token" } }

try {
    $update = Invoke-RestMethod @updateParams
    Write-Log "Combo updated: $($update.models.Count) models now (removed $($toRemove.Count), added $addedCount free, replaced $($replacements.Count))"
} catch {
    Write-Log "ERROR updating combo: $($_.Exception.Message)"
    Save-State $state
    exit 1
}

# State summary
$healthy = @($state.Values | Where-Object { $_.ContainsKey('consecutiveFails') -and [int]$_.consecutiveFails -eq 0 }).Count
$warning = @($state.Values | Where-Object { $_.ContainsKey('consecutiveFails') -and [int]$_.consecutiveFails -gt 0 -and [int]$_.consecutiveFails -lt $MaxConsecutiveFails }).Count
Write-Log "State summary: $healthy healthy, $warning warning, $($state.Count) total tracked"

Save-State $state
Write-Log "========== Maintenance complete =========="