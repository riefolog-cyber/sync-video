param(
    [string]$ComboName = "comboact",
    [switch]$DryRun,
    [switch]$NoReport,
    [switch]$AddFreeModels,
    [switch]$AutoReplace,
    [int]$MaxConsecutiveFails = 0
)
$ErrorActionPreference = "Stop"

$LogDir = Join-Path $PSScriptRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory $LogDir -Force | Out-Null }

$LogFile = Join-Path $LogDir "comboact-update-$(Get-Date -Format 'yyyy-MM-dd').log"
$StateFile = Join-Path $LogDir "comboact-state.json"
$TrendFile = Join-Path $LogDir "comboact-trend.json"
$LockFile = Join-Path $LogDir "comboact.lock"
$ConfigFile = Join-Path $PSScriptRoot "config.json"

$Config = @{ baseUrl = "http://localhost:20128"; timeoutSec = 30; retries = 2; maxConsecutiveFails = 3 }
if (Test-Path $ConfigFile) {
    $Config = Get-Content $ConfigFile -Raw | ConvertFrom-Json
}

# === AUTH (9Router CLI token) ===
# 9Router autorizza via header "x-9r-cli-token" derivato da machine-id + cli-secret
# (Bearer token resta supportato come fallback se configurato)
function Get-9RouterCliToken() {
    $routerHome = if ($Config.routerHome) { $Config.routerHome } else { Join-Path $env:APPDATA "9router" }
    $machineFile = Join-Path $routerHome "machine-id"
    $secretFile = Join-Path $routerHome "auth\cli-secret"
    if (-not (Test-Path $machineFile) -or -not (Test-Path $secretFile)) { return "" }
    $machineId = (Get-Content $machineFile -Raw).Trim()
    $cliSecret = (Get-Content $secretFile -Raw).Trim()
    $raw = "${machineId}9r-cli-auth${cliSecret}"
    $sha = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($raw))
    return ([BitConverter]::ToString($sha) -replace '-', '').ToLower().Substring(0, 16)
}

function Get-AuthHeaders() {
    $headers = @{}
    if ($Config.token) { $headers["Authorization"] = "Bearer $($Config.token)" }
    $cliToken = Get-9RouterCliToken
    if ($cliToken) { $headers["x-9r-cli-token"] = $cliToken }
    return $headers
}

# CLI param overrides
if ($AddFreeModels) { $Config | Add-Member -NotePropertyName addFreeModels -NotePropertyValue $true -Force }
if ($AutoReplace) { $Config | Add-Member -NotePropertyName autoReplace -NotePropertyValue $true -Force }
if ($MaxConsecutiveFails -gt 0) { $Config | Add-Member -NotePropertyName maxConsecutiveFails -NotePropertyValue $MaxConsecutiveFails -Force }

# === LOCK PROTECTION ===
function Acquire-Lock() {
    if (Test-Path $LockFile) {
        $lockTime = (Get-Item $LockFile).CreationTime
        if ((Get-Date) - $lockTime -lt (New-TimeSpan -Minutes 10)) {
            throw "Maintenance already running. Delete: $LockFile"
        }
    }
    @{ acquired = (Get-Date) } | ConvertTo-Json | Set-Content $LockFile -Encoding UTF8
}

function Release-Lock() {
    if (Test-Path $LockFile) { Remove-Item $LockFile -Force -ErrorAction SilentlyContinue }
}

# === LOGGING ===
function Write-Log([string]$msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Write-Output $line
    Add-Content -LiteralPath $LogFile -Value $line
}

# === HEALTH CHECK ===
function Test-9RouterHealth() {
    try {
        $response = Invoke-WebRequest -Uri "$($Config.baseUrl)/api/health" -TimeoutSec 5 -ErrorAction Stop -UseBasicParsing
        Write-Log "[OK] 9Router Health Check: OK"
        return $true
    } catch {
        Write-Log "[ERR] 9Router Health Check FAILED: $($_.Exception.Message)"
        return $false
    }
}

# === FETCH MODELS & COMBOS ===
function Get-CurrentCombo() {
    try {
        $headers = Get-AuthHeaders
        $response = Invoke-WebRequest -Uri "$($Config.baseUrl)/api/combos" -Headers $headers -TimeoutSec $Config.timeoutSec -ErrorAction Stop -UseBasicParsing
        $parsed = $response.Content | ConvertFrom-Json
        $combos = if ($parsed.combos) { $parsed.combos } else { $parsed }
        $combo = @($combos) | Where-Object { $_.name -eq $ComboName } | Select-Object -First 1
        if ($combo) {
            $modelCount = if ($combo.models) { @($combo.models).Count } else { 0 }
            Write-Log "Found combo: $ComboName with $modelCount models"
            return $combo
        }
    } catch {
        Write-Log "[ERR] Failed to fetch combos: $($_.Exception.Message)"
    }
    return $null
}

function Get-AllModels() {
    try {
        $headers = Get-AuthHeaders
        $response = Invoke-WebRequest -Uri "$($Config.baseUrl)/api/models" -Headers $headers -TimeoutSec $Config.timeoutSec -ErrorAction Stop -UseBasicParsing
        $parsed = $response.Content | ConvertFrom-Json
        $models = if ($parsed.models) { $parsed.models } else { $parsed }
        Write-Log "Fetched $($models.Count) available models"
        return $models
    } catch {
        Write-Log "[ERR] Failed to fetch models: $($_.Exception.Message)"
        return @()
    }
    }

    # === APPLY CHANGES TO ROUTER ===
    function Update-Combo([string]$comboName, [array]$models) {
        try {
            $headers = Get-AuthHeaders
            $body = @{ name = $comboName; models = $models } | ConvertTo-Json -Depth 3
            $uri = "$($Config.baseUrl)/api/combos/$comboName"
            $response = Invoke-WebRequest -Uri $uri -Method Put -Body $body -ContentType 'application/json' -TimeoutSec $Config.timeoutSec -ErrorAction Stop -UseBasicParsing -Headers $headers
            Write-Log "[OK] Combo updated: $comboName ($($models.Count) models)"
            return $true
        } catch {
            Write-Log "[ERR] Failed to update combo: $($_.Exception.Message)"
            return $false
        }
    }

    # === TEST MODEL WITH RETRY ===
function Test-Model([string]$modelName, [int]$attempt = 1) {
    try {
        $headers = Get-AuthHeaders
        $body = @{ model = $modelName; kind = "llm" } | ConvertTo-Json
        $uri = "$($Config.baseUrl)/api/models/test"
        $response = Invoke-WebRequest -Uri $uri -Method Post -Body $body -ContentType 'application/json' -TimeoutSec $Config.timeoutSec -ErrorAction Stop -WarningAction SilentlyContinue -UseBasicParsing -Headers $headers
        $parsed = $response.Content | ConvertFrom-Json -ErrorAction SilentlyContinue
        if ($parsed -and $parsed.ok) {
            return @{ success = $true; statusCode = 200; error = ""; replacement = $null }
        }
        $statusCode = if ($parsed.status) { $parsed.status } else { $response.StatusCode }
        return @{ success = $false; statusCode = $statusCode; error = $parsed.error; replacement = $null }
    } catch {
        $statusCode = $null
        $errorMsg = $_.Exception.Message
        $replacement = $null
        if ($_.Exception.Response) {
            $statusCode = $_.Exception.Response.StatusCode.value__
            try {
                $body = $_.Exception.Response.Content | ConvertFrom-Json -ErrorAction SilentlyContinue
                $errorMsg = if ($body.error.message) { $body.error.message } elseif ($body.message) { $body.message } else { $errorMsg }
                $replacement = if ($body.error.replacement) { $body.error.replacement } elseif ($body.replacement) { $body.replacement } else { $null }
            } catch { }
        }

        if (-not $statusCode) {
            $statusCode = 500
        }

        if (($statusCode -in @(408, 429, 500, 502, 503, 504, 529)) -and $attempt -lt $Config.retries) {
            Start-Sleep -Seconds (2 * $attempt)
            return Test-Model $modelName ($attempt + 1)
        }

        return @{ success = $false; statusCode = $statusCode; error = $errorMsg; replacement = $replacement }
    }
}

# === DECISION LOGIC ===
function Decide-ModelFate([string]$modelName, [object]$testResult, [hashtable]$state) {
    $modelState = $state[$modelName]
    if (-not $modelState) {
        $modelState = @{ lastOk = (Get-Date -Format 'yyyy-MM-dd'); lastStatus = 200; lastError = ""; consecutiveFails = 0 }
        $state[$modelName] = $modelState
    }
    
    if ($testResult.success) {
        $modelState.lastOk = Get-Date -Format 'yyyy-MM-dd'
        $modelState.lastStatus = 200
        $modelState.lastError = ""
        $modelState.consecutiveFails = 0
        return @{ action = "KEEP"; reason = "OK (200)" }
    }
    
    $statusCode = $testResult.statusCode
    $errorMsg = $testResult.error
    $replacement = if ($testResult.PSObject.Properties['replacement']) { $testResult.replacement } else { $null }
    $modelState.lastStatus = $statusCode
    $modelState.lastError = $errorMsg

    if ($replacement) {
        return @{ action = "REPLACE"; reason = "REPLACEMENT ($replacement)"; replacement = $replacement }
    }

    # CATEGORICAL ERRORS
    if ($statusCode -in @(400, 401, 402, 403, 404, 405, 406, 409, 410, 415, 422)) {
        return @{ action = "REMOVE"; reason = "CATEGORICAL HTTP $statusCode" }
    }

    # PATTERN MATCHING
    $patterns = @("not_found", "no longer available", "end of life", "was retired", "reached its end", "not supported", "no active credentials")
    foreach ($pattern in $patterns) {
        if ($errorMsg -match $pattern) {
            return @{ action = "REMOVE"; reason = "CATEGORICAL ($pattern)" }
        }
    }

    # RATE LIMIT (don't count as failure)
    if ($statusCode -in @(429, 503)) {
        $modelState.consecutiveFails = 0
        return @{ action = "KEEP"; reason = "TEMPORARY (rate limit)" }
    }

    # CHRONIC ERRORS
    $modelState.consecutiveFails++
    if ($modelState.consecutiveFails -ge $Config.maxConsecutiveFails) {
        return @{ action = "REMOVE"; reason = "CHRONIC ($($modelState.consecutiveFails)/$($Config.maxConsecutiveFails))" }
    }

    return @{ action = "KEEP"; reason = "TEMPORARY ($($modelState.consecutiveFails)/$($Config.maxConsecutiveFails))" }
}

# === UTILITY FUNCTIONS ===
function Update-TrendFile([int]$removed, [int]$replaced, [int]$working) {
    $today = Get-Date -Format 'yyyy-MM-dd'
    $trend = @{}

    if (Test-Path $TrendFile) {
        try {
            $trend = Get-Content $TrendFile -Raw | ConvertFrom-Json | ConvertTo-Hashtable
        } catch { }
    }

    $trend[$today] = @{ removed = $removed; replaced = $replaced; working = $working }
    $trend | ConvertTo-Json -Depth 3 | Set-Content $TrendFile -Encoding UTF8
}

function ConvertTo-Hashtable($obj) {
    $ht = @{}
    if ($obj) { $obj.PSObject.Properties | ForEach-Object { $ht[$_.Name] = $_.Value } }
    return $ht
}

function Check-Alerts([int]$removed) {
    if (-not $Config.alerts) { return }
    $threshold = if ($Config.alerts.removedPerRunThreshold) { $Config.alerts.removedPerRunThreshold } else { 5 }
    if ($removed -ge $threshold) {
        Write-Log "[ALERT] $removed models removed (threshold: $threshold)"
    }
}

function Clean-OldLogs() {
    if ($Config.logCleanup -and $Config.logCleanup.enableAutoDelete) {
        $retentionDays = if ($Config.logCleanup.retentionDays) { $Config.logCleanup.retentionDays } else { 30 }
        $cutoffDate = (Get-Date).AddDays(-$retentionDays)
        Get-ChildItem $LogDir -Filter "comboact-update-*.log" -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt $cutoffDate } |
            ForEach-Object { Remove-Item $_ -Force -ErrorAction SilentlyContinue; Write-Log "Deleted: $($_.Name)" }
    }
}

function Send-Notification([string]$subject, [string]$body, [hashtable]$data) {
    if (-not $Config.notifications -or -not $Config.notifications.enabled) { return }
    if (-not $Config.notifications.webhookUrl) { return }

    $payload = @{
        subject = $subject
        body = $body
        timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        data = $data
    } | ConvertTo-Json -Depth 3

    try {
        Invoke-RestMethod -Uri $Config.notifications.webhookUrl -Method Post -Body $payload -ContentType 'application/json' -ErrorAction Stop
        Write-Log "[OK] Notification sent: $subject"
    } catch {
        Write-Log "[WARN] Notification failed: $($_.Exception.Message)"
    }
}

function Write-StructuredLog([hashtable]$record) {
    if (-not $Config.reporting) { return }
    if ($Config.reporting.enableJsonLogging) {
        $jsonLine = $record | ConvertTo-Json -Compress
        $jsonFile = Join-Path $LogDir "comboact-structured-$(Get-Date -Format 'yyyy-MM-dd').jsonl"
        Add-Content -LiteralPath $jsonFile -Value $jsonLine -Encoding UTF8
    }
    if ($Config.reporting.enableCsvLogging -and $record.level -eq 'MODEL') {
        $csvFile = Join-Path $LogDir "comboact-models-$(Get-Date -Format 'yyyy-MM-dd').csv"
        $row = [pscustomobject]@{
            timestamp = $record.timestamp
            model = $record.model
            action = $record.action
            statusCode = $record.statusCode
            reason = $record.reason
        }
        if (-not (Test-Path $csvFile)) {
            $row | Export-Csv -Path $csvFile -NoTypeInformation -Encoding UTF8
        } else {
            $row | Export-Csv -Path $csvFile -NoTypeInformation -Append -Encoding UTF8
        }
    }
}

# === MAIN EXECUTION ===
try {
    Acquire-Lock
    
    Write-Host ""
    Write-Host "9Router ComboAct Maintenance - PRO Edition" -ForegroundColor Cyan
    Write-Host ""
    
    Write-Log "===== Starting combo maintenance (PRO) ====="
    
    if (-not (Test-9RouterHealth)) {
        Write-Log "[ERR] ABORT: 9Router not responding"
        exit 1
    }
    
    Write-Log "DryRun: $DryRun | AddFreeModels: $($Config.addFreeModels) | AutoReplace: $($Config.autoReplace)"
    
    # Load state
    $state = @{}
    if (Test-Path $StateFile) {
        try {
            $stateJson = Get-Content $StateFile -Raw | ConvertFrom-Json -ErrorAction SilentlyContinue
            if ($stateJson) {
                foreach ($prop in $stateJson.PSObject.Properties) {
                    if ($prop.Name -ne "lastUpdated") {
                        $state[$prop.Name] = $prop.Value
                    }
                }
            }
        } catch { Write-Log "[WARN] Could not parse state file" }
    }
    
    Write-Log "Loaded state for $($state.Count) models"
    
    # Get current combo
    $currentCombo = Get-CurrentCombo
    if (-not $currentCombo) {
        Write-Log "[ERR] Could not fetch current combo"
        exit 1
    }
    
    $currentModels = if ($null -ne $currentCombo.models) { @($currentCombo.models) } else { @() }
    if ($currentModels -is [string]) { $currentModels = @($currentModels) }
    Write-Log "Testing $($currentModels.Count) models..."
    
    # Test all models
    $toKeep = @{}
    $toRemove = @{}
    $toAdd = @{}
    $replaced = @{}

    $throttle = if ($Config.parallelization -and $Config.parallelization.throttleLimit) { $Config.parallelization.throttleLimit } else { 1 }
    $modelNames = @($currentModels | ForEach-Object { if ($_ -is [string]) { $_ } else { $_.ToString() } })

    $useParallel = $throttle -gt 1 -and $modelNames.Count -gt 1 -and ($PSVersionTable.PSVersion.Major -ge 7)
    $cliToken = Get-9RouterCliToken
    $results = @{}
    if ($useParallel) {
        $rawResults = $modelNames | ForEach-Object -Parallel {
            $cfg = $using:Config
            $cliToken = $using:cliToken
            $modelName = $_
            function Test-ModelInline([string]$mn, [int]$attempt = 1) {
                try {
                    $headers = @{}
                    if ($cfg.token) { $headers["Authorization"] = "Bearer $($cfg.token)" }
                    if ($cliToken) { $headers["x-9r-cli-token"] = $cliToken }
                    $body = @{ model = $mn; kind = "llm" } | ConvertTo-Json
                    $uri = "$($cfg.baseUrl)/api/models/test"
                    $response = Invoke-WebRequest -Uri $uri -Method Post -Body $body -ContentType 'application/json' -TimeoutSec $cfg.timeoutSec -ErrorAction Stop -WarningAction SilentlyContinue -UseBasicParsing -Headers $headers
                    $parsed = $response.Content | ConvertFrom-Json -ErrorAction SilentlyContinue
                    if ($parsed -and $parsed.ok) {
                        return @{ success = $true; statusCode = 200; error = ""; replacement = $null }
                    }
                    return @{ success = $false; statusCode = if ($parsed.status) { $parsed.status } else { $response.StatusCode }; error = $parsed.error; replacement = $null }
                } catch {
                    $statusCode = $null
                    $errorMsg = $_.Exception.Message
                    $replacement = $null
                    if ($_.Exception.Response) {
                        $statusCode = $_.Exception.Response.StatusCode.value__
                        try {
                            $body = $_.Exception.Response.Content | ConvertFrom-Json -ErrorAction SilentlyContinue
                            $errorMsg = if ($body.error.message) { $body.error.message } elseif ($body.message) { $body.message } else { $errorMsg }
                            $replacement = if ($body.error.replacement) { $body.error.replacement } elseif ($body.replacement) { $body.replacement } else { $null }
                        } catch { }
                    }
                    if (-not $statusCode) { $statusCode = 500 }
                    if (($statusCode -in @(408, 429, 500, 502, 503, 504, 529)) -and $attempt -lt $cfg.retries) {
                        Start-Sleep -Seconds (2 * $attempt)
                        return Test-ModelInline $mn ($attempt + 1)
                    }
                    return @{ success = $false; statusCode = $statusCode; error = $errorMsg; replacement = $replacement }
                }
            }
            @{ model = $modelName; result = (Test-ModelInline $modelName 1) }
        } -ThrottleLimit $throttle
        foreach ($r in $rawResults) { $results[$r.model] = $r.result }
    } else {
        foreach ($modelName in $modelNames) {
            $results[$modelName] = Test-Model $modelName 1
        }
    }

    foreach ($modelName in $modelNames) {
        $result = $results[$modelName]
        $decision = Decide-ModelFate $modelName $result $state
        $action = $decision.action
        $reason = $decision.reason

        if ($action -eq "KEEP") {
            $toKeep[$modelName] = $true
        } elseif ($action -eq "REMOVE") {
            $toRemove[$modelName] = $true
        }

        if ($action -eq "REPLACE" -or $result.replacement) {
            $replacementName = if ($action -eq "REPLACE") { $decision.replacement } else { $result.replacement }
            if ($replacementName) {
                $replaced[$modelName] = $replacementName
                $toAdd[$replacementName] = $true
                Write-Log "REPLACE $modelName -> $replacementName | $reason"
                Write-StructuredLog @{
                    timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
                    level = 'MODEL'
                    model = $modelName
                    action = 'REPLACE'
                    statusCode = $result.statusCode
                    reason = $reason
                }
                continue
            }
        }

        Write-Log "$action [$($result.statusCode)] $modelName | $reason"
        Write-StructuredLog @{
            timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
            level = 'MODEL'
            model = $modelName
            action = $action
            statusCode = $result.statusCode
            reason = $reason
        }
    }

    $keepCount = $toKeep.Count
    $removeCount = $toRemove.Count
    $replaceCount = $replaced.Count
    $addCount = $toAdd.Count

    Write-Log "Result: Keep=$keepCount | Remove=$removeCount | Replace=$replaceCount | Add=$addCount"

    # Auto-replace already handled above via replacement field
    if ($Config.autoReplace) {
        foreach ($old in $replaced.Keys) {
            if (-not $toKeep.ContainsKey($old) -and -not $toRemove.ContainsKey($old)) {
                $toRemove[$old] = $true
            }
        }
    }
    
    # Add free models if enabled
    if ($Config.addFreeModels) {
        $freeModels = if ($Config.freeModels) { @($Config.freeModels) } else {
            @(
                "gemini/gemini-2.5-flash",
                "gemini/gemini-2.5-flash-lite",
                "groq/llama-3.3-70b-versatile",
                "mistral/mistral-large-latest",
                "openrouter/openrouter/free",
                "oc/nemotron-3-ultra-free"
            )
        }
        foreach ($freeModel in $freeModels) {
            if (-not $toKeep.ContainsKey($freeModel) -and -not $toAdd.ContainsKey($freeModel)) {
                Write-Log "Adding free model: $freeModel"
                $toAdd[$freeModel] = $true
                Write-StructuredLog @{
                    timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
                    level = 'MODEL'
                    model = $freeModel
                    action = 'ADD'
                    statusCode = ''
                    reason = 'free model'
                }
            }
        }
    }

    # Validate proposed additions against available models
    $availableNames = $null
    if ($toAdd.Count -gt 0) {
        $allModels = Get-AllModels
        if ($allModels -and $allModels.Count -gt 0) {
            $availableNames = @($allModels | ForEach-Object { if ($_.fullModel) { $_.fullModel } elseif ($_.name) { $_.name } elseif ($_.id) { $_.id } else { $_.ToString() } })
        }
    }

    $validatedAdd = @{}
    foreach ($add in $toAdd.Keys) {
        if ($availableNames -and $availableNames -notcontains $add) {
            Write-Log "[WARN] Skipping add of unavailable model: $add"
            continue
        }
        $validatedAdd[$add] = $true
    }

    $newModels = @()
    $newModels += $toKeep.Keys
    $newModels += $validatedAdd.Keys

    if ($DryRun) {
        Write-Log "[DRYRUN] Would update combo '$ComboName' with $($newModels.Count) models (kept=$keepCount, added=$($validatedAdd.Count), removed=$removeCount, replaced=$replaceCount). No changes applied."
    } else {
        # Update state
        $newState = @{ lastUpdated = Get-Date -Format 'yyyy-MM-dd HH:mm:ss' }
        $modelsToSave = @()
        $modelsToSave += $toKeep.Keys
        $modelsToSave += $validatedAdd.Keys

        foreach ($modelName in $modelsToSave) {
            if ($state[$modelName]) {
                $newState[$modelName] = $state[$modelName]
            } else {
                $newState[$modelName] = @{ lastOk = (Get-Date -Format 'yyyy-MM-dd'); lastStatus = 200; lastError = ""; consecutiveFails = 0 }
            }
        }
        $newState | ConvertTo-Json -Depth 5 | Set-Content $StateFile -Encoding UTF8

        # Update trend
        Update-TrendFile $removeCount $replaceCount $keepCount
        Check-Alerts $removeCount
        Clean-OldLogs

        # Apply changes to the router
        $applied = Update-Combo $ComboName $newModels
        if (-not $applied) {
            Write-Log "[WARN] Combo update failed; local state/trend were still updated."
        }
    }

    if (-not $NoReport) {
        Write-Log "Generating HTML report..."
        & (Join-Path $PSScriptRoot "report-html.ps1") -Date (Get-Date -Format 'yyyy-MM-dd') -ErrorAction SilentlyContinue | Out-Null
    }

    Write-Log "===== Maintenance complete ====="
    Write-Host ""
    Write-Host "SUCCESS: Kept=$keepCount | Removed=$removeCount | Replaced=$replaceCount | Added=$($validatedAdd.Count)" -ForegroundColor Green
    Write-Host ""

    if ($Config.notifications -and $Config.notifications.enabled) {
        $subject = if ($removeCount -eq 0) { "✅ Maintenance Completed" } else { "⚠️ Maintenance Completed with removals" }
        $body = "Combo updated: $keepCount kept, $removeCount removed, $replaceCount replaced, $($validatedAdd.Count) added"
        Send-Notification -subject $subject -body $body -data @{ kept = $keepCount; removed = $removeCount; replaced = $replaceCount; added = $toAdd.Count }
    }
    
} catch {
    Write-Log "[FATAL] $($_.Exception.Message)"
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    Release-Lock
}
