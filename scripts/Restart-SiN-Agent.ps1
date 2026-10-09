[CmdletBinding()]
param(
    [string]$MetadataPath = "C:\SiN\deployment.json",
    [string]$AgentRoot = "",
    [string]$ApiUrl = "https://sin-central.duckdns.org",
    [string]$MailboxDir = "",
    [double]$PollInterval = 0,
    [string]$AgentTaskName = "SiN FS25 Agent"
)

$ErrorActionPreference = "Stop"

if (Test-Path -LiteralPath $MetadataPath -PathType Leaf) {
    $metadata = Get-Content -LiteralPath $MetadataPath -Raw | ConvertFrom-Json
    if (-not $AgentRoot -and $metadata.agent_root) { $AgentRoot = [string]$metadata.agent_root }
    if (-not $ApiUrl -and $metadata.backend_url) { $ApiUrl = [string]$metadata.backend_url }
    if (-not $MailboxDir -and $metadata.mailbox_dir) { $MailboxDir = [string]$metadata.mailbox_dir }
    if ($PollInterval -le 0 -and $metadata.poll_interval) { $PollInterval = [double]$metadata.poll_interval }
}
if (-not $AgentRoot) { $AgentRoot = "C:\SiN\Agent" }
if (-not $MailboxDir) { throw "Agent mailbox directory is missing. Pass -MailboxDir or provide deployment metadata." }
if ($PollInterval -le 0) { $PollInterval = 2 }

$leaf = Split-Path -Leaf ($MailboxDir.TrimEnd([char]92, [char]47))
if ($leaf -ne "FS25_SiN_Server") { throw "MailboxDir must be the canonical FS25_SiN_Server mailbox root." }

function Get-AgentTask {
    $task = Get-ScheduledTask -TaskName $AgentTaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) { throw "Required Agent scheduled task '$AgentTaskName' is not registered." }
    return $task
}

function Get-AgentProcess {
    @(Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -and $_.CommandLine -match "fs25_network_core\.agent" -and $_.CommandLine -match "--watch" })
}

function Get-AgentPythonPath {
    $task = Get-AgentTask
    $actions = @($task.Actions)
    if ($actions.Count -ne 1 -or -not $actions[0].Execute) {
        throw "Agent task '$AgentTaskName' must have exactly one executable action."
    }
    $python = [Environment]::ExpandEnvironmentVariables([string]$actions[0].Execute)
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Agent task Python executable is unavailable: $python"
    }
    return $python
}

function Stop-AgentTask {
    $task = Get-AgentTask
    if ($task.State -eq "Running") { Stop-ScheduledTask -TaskName $AgentTaskName -ErrorAction Stop }
    $deadline = (Get-Date).AddSeconds(20)
    do {
        $task = Get-ScheduledTask -TaskName $AgentTaskName -ErrorAction Stop
        $remaining = @(Get-AgentProcess)
        if ($task.State -ne "Running" -and $remaining.Count -eq 0) { return }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    foreach ($process in $remaining) { Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
    $task = Get-ScheduledTask -TaskName $AgentTaskName -ErrorAction SilentlyContinue
    if ($null -eq $task -or $task.State -eq "Running" -or @(Get-AgentProcess).Count -gt 0) {
        throw "Agent scheduled task '$AgentTaskName' did not stop."
    }
}

function Start-AgentTask {
    if ($ApiUrl.Contains('"') -or $MailboxDir.Contains('"')) {
        throw "Agent URL and mailbox path cannot contain quote characters."
    }
    $python = Get-AgentPythonPath
    $arguments = '-m fs25_network_core.agent --watch --backend-url "{0}" --mailbox-dir "{1}" --interval {2}' -f `
        $ApiUrl, $MailboxDir, $PollInterval
    $action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $AgentRoot
    Set-ScheduledTask -TaskName $AgentTaskName -Action $action -ErrorAction Stop | Out-Null
    Start-ScheduledTask -TaskName $AgentTaskName -ErrorAction Stop
    Start-Sleep -Seconds 3
    $task = Get-ScheduledTask -TaskName $AgentTaskName -ErrorAction Stop
    if ($task.State -ne "Running") {
        $info = Get-ScheduledTaskInfo -TaskName $AgentTaskName
        throw "Agent scheduled task did not stay running (state=$($task.State), lastTaskResult=$($info.LastTaskResult))."
    }
}

New-Item -ItemType Directory -Force -Path $MailboxDir | Out-Null
Stop-AgentTask
Start-AgentTask
Write-Host "SiN Agent scheduled task '$AgentTaskName' restarted with backend $ApiUrl and canonical mailbox."
