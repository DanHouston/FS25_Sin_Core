[CmdletBinding()]
param(
    [string]$MetadataPath = "C:\SiN\deployment.json",
    [string]$AgentRoot = "",
    [string]$ApiUrl = "",
    [string]$MailboxDir = "",
    [double]$PollInterval = 0
)

$ErrorActionPreference = "Stop"
$logRoot = "C:\SiN\Logs"

if (Test-Path -LiteralPath $MetadataPath -PathType Leaf) {
    $metadata = Get-Content -LiteralPath $MetadataPath -Raw | ConvertFrom-Json
    if (-not $AgentRoot -and $metadata.agent_root) { $AgentRoot = [string]$metadata.agent_root }
    if (-not $ApiUrl -and $metadata.backend_url) { $ApiUrl = [string]$metadata.backend_url }
    if (-not $MailboxDir -and $metadata.mailbox_dir) { $MailboxDir = [string]$metadata.mailbox_dir }
    if ($PollInterval -le 0 -and $metadata.poll_interval) { $PollInterval = [double]$metadata.poll_interval }
}
if (-not $AgentRoot) { $AgentRoot = "C:\SiN\Agent" }
if (-not $ApiUrl) { throw "Agent backend URL is missing. Pass -ApiUrl or provide deployment metadata." }
if (-not $MailboxDir) { throw "Agent mailbox directory is missing. Pass -MailboxDir or provide deployment metadata." }
if ($PollInterval -le 0) { $PollInterval = 2 }

$leaf = Split-Path -Leaf ($MailboxDir.TrimEnd([char]92, [char]47))
if ($leaf -ne "FS25_SiN_Server") { throw "MailboxDir must be the canonical FS25_SiN_Server mailbox root." }

function Get-AgentProcess {
    @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match "fs25_network_core\.agent" -and $_.CommandLine -match "--watch" })
}

foreach ($process in @(Get-AgentProcess)) {
    Stop-Process -Id ([int]$process.ProcessId) -ErrorAction SilentlyContinue
}
$deadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Milliseconds 500
    $remaining = @(Get-AgentProcess)
} while ($remaining.Count -gt 0 -and (Get-Date) -lt $deadline)
if ($remaining.Count -gt 0) {
    foreach ($process in $remaining) { Stop-Process -Id ([int]$process.ProcessId) -Force }
    Start-Sleep -Seconds 1
}

New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
New-Item -ItemType Directory -Force -Path $MailboxDir | Out-Null
$oldBackend = $env:SIN_BACKEND_URL
$oldMailbox = $env:SIN_MAILBOX_DIR
$oldPoll = $env:SIN_POLL_INTERVAL
try {
    $env:SIN_BACKEND_URL = $ApiUrl
    $env:SIN_MAILBOX_DIR = $MailboxDir
    $env:SIN_POLL_INTERVAL = [string]$PollInterval
    Start-Process -FilePath "python.exe" -ArgumentList @("-m", "fs25_network_core.agent", "--watch") `
        -WorkingDirectory $AgentRoot -RedirectStandardOutput (Join-Path $logRoot "agent.log") `
        -RedirectStandardError (Join-Path $logRoot "agent-error.log") -WindowStyle Hidden | Out-Null
} finally {
    $env:SIN_BACKEND_URL = $oldBackend
    $env:SIN_MAILBOX_DIR = $oldMailbox
    $env:SIN_POLL_INTERVAL = $oldPoll
}
Start-Sleep -Seconds 3
$running = @(Get-AgentProcess)
if ($running.Count -ne 1) { throw "Expected one Agent watcher after restart; inspect C:\SiN\Logs\agent-error.log" }
Write-Host "SiN Agent watcher restarted with durable mailbox recovery configuration."
