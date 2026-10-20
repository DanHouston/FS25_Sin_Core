[CmdletBinding()]
param(
    [string]$MailboxDir = 'C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\modSettings\FS25_SiN_Server',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$eventDir = (Resolve-Path -LiteralPath (Join-Path $MailboxDir 'events')).Path
$backupDir = Join-Path $MailboxDir ('recovery-archive\finance-day-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N'))

# Run only after deploying the Central day-counter fix. Default mode previews
# candidates. Never print XML: mailbox envelopes contain server credentials.
foreach ($file in @(Get-ChildItem -LiteralPath $eventDir -Filter '*.xml.failed*' -File)) {
    if ($file.Name -notmatch '\.xml\.failed(?:\.\d+)?$') { continue }
    try {
        $financeXml = [xml](Get-Content -LiteralPath $file.FullName -Raw -ErrorAction Stop)
    } catch {
        Write-Warning ('Skipping unreadable XML: ' + $file.Name)
        continue
    }
    $envelope = $financeXml.DocumentElement
    if ($envelope.Name -ne 'serverEvent' -or $envelope.GetAttribute('event_type') -ne 'farm_finance_batch') { continue }
    $rows = @($envelope.SelectNodes('changes/change'))
    if ($rows.Count -lt 1 -or $rows.Count -gt 128) { continue }
    $days = @()
    $validDays = $true
    foreach ($row in $rows) {
        $text = $row.GetAttribute('game_day')
        if ($text -eq '') { continue }
        $day = [long]0
        if (-not [long]::TryParse($text, [ref]$day) -or $day -lt 0 -or $day -gt 2147483647 -or [string]$day -cne $text) {
            $validDays = $false
            break
        }
        $days += $day
    }
    if (-not $validDays -or @($days | Where-Object { $_ -gt 31 }).Count -eq 0) { continue }
    $retryName = $file.Name -replace '\.failed(?:\.\d+)?$', ''
    if (Test-Path -LiteralPath (Join-Path $eventDir $retryName)) {
        Write-Warning ('Pending destination already exists; skipping: ' + $file.Name)
        continue
    }
    $action = 'Preview only'
    if ($Apply) {
        if (-not (Test-Path -LiteralPath $backupDir)) {
            New-Item -ItemType Directory -Path $backupDir | Out-Null
        }
        Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $backupDir $file.Name)
        Rename-Item -LiteralPath $file.FullName -NewName $retryName
        $action = 'Requeued; original bytes backed up'
    }
    [pscustomobject]@{
        File = $file.Name
        GameDays = (($days | Sort-Object -Unique) -join ', ')
        Action = $action
    }
}
if ($Apply -and (Test-Path -LiteralPath $backupDir)) {
    Write-Host ('Recovery backup: ' + $backupDir)
}
