param([string]$Date = (Get-Date -Format 'yyyy-MM-dd'))

$LogFile = Join-Path $PSScriptRoot "logs\comboact-update-$Date.log"
$StateFile = Join-Path $PSScriptRoot "logs\comboact-state.json"
$TrendFile = Join-Path $PSScriptRoot "logs\comboact-trend.json"

if (-not (Test-Path $LogFile)) {
    Write-Host "Log file not found: $Date" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "COMBOACT MODELS REPORT - $Date" -ForegroundColor Cyan
Write-Host "============================================================"
Write-Host ""

$log = Get-Content $LogFile

$removed = @($log | Select-String "REMOVE")
$replaced = @($log | Select-String "REPLACE")
$kept = @($log | Select-String "KEEP")

# Extract provider stats
$providers = @{}
$removed | ForEach-Object {
    if ($_ -match '\[(\w+)\/') {
        $provider = $matches[1].ToLower()
        if (-not $providers[$provider]) { $providers[$provider] = @{ total = 0; removed = 0 } }
        $providers[$provider].total++
        $providers[$provider].removed++
    }
}
$kept | ForEach-Object {
    if ($_ -match '\[(\w+)\/') {
        $provider = $matches[1].ToLower()
        if (-not $providers[$provider]) { $providers[$provider] = @{ total = 0; removed = 0 } }
        $providers[$provider].total++
    }
}
$replaced | ForEach-Object {
    if ($_ -match '\[(\w+)\/') {
        $provider = $matches[1].ToLower()
        if (-not $providers[$provider]) { $providers[$provider] = @{ total = 0; removed = 0 } }
        $providers[$provider].total++
    }
}

Write-Host "SUMMARY:" -ForegroundColor Yellow
Write-Host "  Removed:    $($removed.Count) models"
Write-Host "  Replaced:   $($replaced.Count) models"
Write-Host "  Kept (temp): $($kept.Count) models"
Write-Host ""

if ($providers.Count -gt 0) {
    Write-Host "PROVIDER RELIABILITY:" -ForegroundColor Magenta
    $providers.Keys | Sort-Object | ForEach-Object {
        $prov = $_
        $stats = $providers[$prov]
        $reliability = if ($stats.total -gt 0) { [int](((($stats.total - $stats.removed) / $stats.total) * 100)) } else { 0 }
        $status = if ($reliability -ge 90) { "✅" } elseif ($reliability -ge 70) { "⚠️ " } else { "❌" }
        Write-Host "  $status $prov`: $($stats.total) tested, $($stats.removed) removed ($reliability%)"
    }
    Write-Host ""
}

if ($removed.Count -gt 0) {
    Write-Host "[REMOVED]" -ForegroundColor Red
    $removed
    Write-Host ""
}

if ($replaced.Count -gt 0) {
    Write-Host "[REPLACED]" -ForegroundColor Green
    $replaced
    Write-Host ""
}

if ($kept.Count -gt 0) {
    Write-Host "[TEMP ISSUES]" -ForegroundColor Cyan
    $kept
    Write-Host ""
}

if (Test-Path $TrendFile) {
    Write-Host "TREND (Last 7 days):" -ForegroundColor Magenta
    try {
        $trend = Get-Content $TrendFile -Raw | ConvertFrom-Json
        $trend.PSObject.Properties | Select-Object -Last 7 | ForEach-Object {
            $d = $_.Name
            $data = $_.Value
            Write-Host "  $d`: removed=$($data.removed), replaced=$($data.replaced), working=$($data.working)"
        }
        Write-Host ""
    } catch { }
}

Write-Host "============================================================"
