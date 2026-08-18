param([string]$Date = (Get-Date -Format 'yyyy-MM-dd'), [switch]$Open)

function ConvertTo-Html([string]$s) {
    if (-not $s) { return "" }
    $s = $s.ToString()
    $s = $s.Replace("&", "&amp;")
    $s = $s.Replace("<", "&lt;")
    $s = $s.Replace(">", "&gt;")
    return $s
}

$LogFile = Join-Path $PSScriptRoot "logs\comboact-update-$Date.log"
$StateFile = Join-Path $PSScriptRoot "logs\comboact-state.json"
$TrendFile = Join-Path $PSScriptRoot "logs\comboact-trend.json"
$OutputFile = Join-Path $PSScriptRoot "logs\comboact-report-$Date.html"

if (-not (Test-Path $LogFile)) {
    Write-Host "Log file not found for date: $Date" -ForegroundColor Red
    exit 1
}

$log = @(Get-Content $LogFile)
$removed = @($log | Select-String "REMOVE")
$replaced = @($log | Select-String "REPLACE")
$kept = @($log | Select-String "KEEP")

$providers = @{}
$removed | ForEach-Object {
    if ($_ -match '\[(\w+)\/') {
        $provider = $matches[1].ToLower()
        if (-not $providers[$provider]) { $providers[$provider] = @{ total = 0; removed = 0 } }
        $providers[$provider].total++
        $providers[$provider].removed++
    }
}

$trendData = @()
if (Test-Path $TrendFile) {
    try {
        $trend = Get-Content $TrendFile -Raw | ConvertFrom-Json
        $trend.PSObject.Properties | ForEach-Object {
            $trendData += [PSCustomObject]@{
                date = $_.Name
                removed = $_.Value.removed
                replaced = $_.Value.replaced
                working = $_.Value.working
            }
        }
    } catch { }
}

$trendJson = $trendData | ConvertTo-Json -Depth 3
$providersHtml = ""
$providers.Keys | Sort-Object | ForEach-Object {
    $prov = $_
    $stats = $providers[$prov]
    $reliability = if ($stats.total -gt 0) { [int](((($stats.total - $stats.removed) / $stats.total) * 100)) } else { 0 }
    $statusColor = if ($reliability -ge 90) { "#4CAF50" } elseif ($reliability -ge 70) { "#FFC107" } else { "#F44336" }
    $statusIcon = if ($reliability -ge 90) { "[OK]" } elseif ($reliability -ge 70) { "[WARN]" } else { "[ERR]" }
    $providersHtml += @"
        <tr>
            <td>$statusIcon $prov</td>
            <td style="text-align:center">$($stats.total)</td>
            <td style="text-align:center; color: $statusColor; font-weight:bold">$($stats.removed)</td>
            <td style="text-align:center">$reliability%</td>
            <td><div style="width:100%;background:#e0e0e0;border-radius:3px;overflow:hidden">
                <div style="width:$reliability%;height:20px;background:$statusColor;display:flex;align-items:center;justify-content:center;color:white;font-size:11px;font-weight:bold">$reliability%</div>
            </div></td>
        </tr>
"@
}

$removedHtml = ""
$removed | ForEach-Object {
    $removedHtml += "<div style='padding:10px;border-left:4px solid #F44336;background:#fff3e0;margin:5px 0'>$(ConvertTo-Html $_)</div>"
}

$replacedHtml = ""
$replaced | ForEach-Object {
    $replacedHtml += "<div style='padding:10px;border-left:4px solid #FF9800;background:#fff8e1;margin:5px 0'>$(ConvertTo-Html $_)</div>"
}

$keptHtml = ""
$kept | ForEach-Object {
    $keptHtml += "<div style='padding:10px;border-left:4px solid #2196F3;background:#e3f2fd;margin:5px 0'>$(ConvertTo-Html $_)</div>"
}

$html = @"
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ComboAct Report - $Date</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            min-height: 100vh;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px 30px;
            text-align: center;
        }
        .header h1 {
            font-size: 32px;
            margin-bottom: 5px;
        }
        .header p {
            font-size: 16px;
            opacity: 0.9;
        }
        .content {
            padding: 30px;
        }
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .summary-card {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        .summary-card h3 {
            font-size: 14px;
            opacity: 0.9;
            margin-bottom: 10px;
            text-transform: uppercase;
        }
        .summary-card .number {
            font-size: 36px;
            font-weight: bold;
        }
        .section {
            margin-bottom: 40px;
        }
        .section-title {
            font-size: 22px;
            font-weight: bold;
            color: #333;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 3px solid #667eea;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            background: white;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        th {
            background: #667eea;
            color: white;
            padding: 15px;
            text-align: left;
            font-weight: 600;
        }
        td {
            padding: 12px 15px;
            border-bottom: 1px solid #e0e0e0;
        }
        tr:hover {
            background: #f5f5f5;
        }
        .chart-container {
            position: relative;
            height: 400px;
            margin: 20px 0;
        }
        .removed-list {
            background: #f9f9f9;
            border-radius: 8px;
            padding: 20px;
        }
        .print-button {
            position: fixed;
            top: 20px;
            right: 20px;
            background: #667eea;
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.2);
            z-index: 100;
        }
        .print-button:hover {
            background: #764ba2;
        }
        @media print {
            body {
                background: white;
                padding: 0;
            }
            .print-button {
                display: none;
            }
            .container {
                box-shadow: none;
                max-width: 100%;
            }
        }
    </style>
</head>
<body>
    <button class="print-button" onclick="window.print()">[PRINT] Stampa / PDF</button>
    
    <div class="container">
        <div class="header">
            <h1>ComboAct Maintenance Report</h1>
            <p>Report generato il $Date alle $(Get-Date -Format 'HH:mm:ss')</p>
        </div>
        
        <div class="content">
            <!-- SUMMARY -->
            <div class="summary-grid">
                <div class="summary-card">
                    <h3>[RIMOSSI] Modelli Rimossi</h3>
                    <div class="number">$($removed.Count)</div>
                </div>
                <div class="summary-card">
                    <h3>[REPLACE] Modelli Sostituiti</h3>
                    <div class="number">$($replaced.Count)</div>
                </div>
                <div class="summary-card">
                    <h3>[TEMP] Problemi Temporanei</h3>
                    <div class="number">$($kept.Count)</div>
                </div>
                <div class="summary-card">
                    <h3>[STATS] Provider Monitorati</h3>
                    <div class="number">$($providers.Count)</div>
                </div>
            </div>

            <!-- TREND CHART -->
            <div class="section">
                <h2 class="section-title">[TREND] Trend Ultimi 30 Giorni</h2>
                <div class="chart-container">
                    <canvas id="trendChart"></canvas>
                </div>
            </div>

            <!-- PROVIDER RELIABILITY -->
            <div class="section">
                <h2 class="section-title">[HEALTH] Affidabilità Provider</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Provider</th>
                            <th>Testati</th>
                            <th>Rimossi</th>
                            <th>Affidabilità</th>
                            <th>Visualizzazione</th>
                        </tr>
                    </thead>
                    <tbody>
                        $providersHtml
                    </tbody>
                </table>
            </div>

            <!-- REPLACED MODELS -->
            <div class="section">
                <h2 class="section-title">[REPLACED] Modelli Sostituiti ($($replaced.Count))</h2>
                <div class="removed-list">
                    $replacedHtml
                </div>
            </div>

            <!-- REMOVED MODELS -->
            <div class="section">
                <h2 class="section-title">[REMOVED] Modelli Rimossi ($($removed.Count))</h2>
                <div class="removed-list">
                    $removedHtml
                </div>
            </div>

            <!-- KEPT MODELS -->
            <div class="section">
                <h2 class="section-title">[KEPT] Modelli Mantenuti ($($kept.Count))</h2>
                <div class="removed-list">
                    $keptHtml
                </div>
            </div>
        </div>
    </div>

    <script>
        const trendData = $trendJson;
        
        if (trendData && trendData.length > 0) {
            const ctx = document.getElementById('trendChart').getContext('2d');
            new Chart(ctx, {
                type: 'line',
                data: {
                    labels: trendData.map(d => d.date),
                    datasets: [
                        {
                            label: 'Modelli Rimossi',
                            data: trendData.map(d => d.removed),
                            borderColor: '#F44336',
                            backgroundColor: 'rgba(244, 67, 54, 0.1)',
                            tension: 0.4,
                            fill: true,
                            borderWidth: 2
                        },
                        {
                            label: 'Modelli Funzionanti',
                            data: trendData.map(d => d.working),
                            borderColor: '#4CAF50',
                            backgroundColor: 'rgba(76, 175, 80, 0.1)',
                            tension: 0.4,
                            fill: true,
                            borderWidth: 2
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            position: 'bottom',
                            labels: { font: { size: 14 }, padding: 20 }
                        }
                    },
                    scales: {
                        y: {
                            beginAtZero: true,
                            ticks: { font: { size: 12 } }
                        },
                        x: {
                            ticks: { font: { size: 12 } }
                        }
                    }
                }
            });
        }
    </script>
</body>
</html>
"@

$html | Set-Content $OutputFile -Encoding UTF8
Write-Host "[OK] Report HTML generato: $OutputFile" -ForegroundColor Green

if ($Open) {
    Start-Process $OutputFile
}
