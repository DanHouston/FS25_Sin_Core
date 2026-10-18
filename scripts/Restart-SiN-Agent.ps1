[CmdletBinding()]
param(
    [string]$MetadataPath = "C:\SiN\deployment.json",
    [string]$AgentRoot = "",
    [string]$ApiUrl = "",
    [string]$MailboxDir = "",
    [double]$PollInterval = 0,
    [string]$AgentTaskName = "SiN FS25 Agent",
    [System.Management.Automation.PSCredential]$AgentTaskCredential,
    [switch]$PromptForAgentTaskCredential
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
if (-not $ApiUrl) { $ApiUrl = "https://sin-central.duckdns.org" }
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

function Set-AgentTaskAction {
    $python = Get-AgentPythonPath
    if ($ApiUrl.Contains('"') -or $MailboxDir.Contains('"')) {
        throw "Agent URL and mailbox path cannot contain quote characters."
    }
    $arguments = '-m fs25_network_core.agent --watch --backend-url "{0}" --mailbox-dir "{1}" --interval {2}' -f `
        $ApiUrl, $MailboxDir, $PollInterval
    $action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $AgentRoot
    $task = Get-AgentTask
    $currentActions = @($task.Actions)
    $expectedExecute = [IO.Path]::GetFullPath($python)
    $expectedWorkingDirectory = [IO.Path]::GetFullPath($AgentRoot)
    $currentExecute = ""
    $currentWorkingDirectory = ""
    if ($currentActions.Count -eq 1) {
        $currentExecute = [string]$currentActions[0].Execute
        $currentWorkingDirectory = [string]$currentActions[0].WorkingDirectory
        if ($currentExecute) {
            $currentExecute = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($currentExecute))
        }
        if ($currentWorkingDirectory) {
            $currentWorkingDirectory = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($currentWorkingDirectory))
        }
    }
    $actionMatches = $currentActions.Count -eq 1 -and
        $currentExecute -ieq $expectedExecute -and
        [string]$currentActions[0].Arguments -ceq $arguments -and
        $currentWorkingDirectory -ieq $expectedWorkingDirectory
    $settings = $task.Settings
    $settingsMatch = $settings.RestartCount -eq 5 -and
        $settings.RestartInterval -eq 'PT1M' -and
        $settings.ExecutionTimeLimit -eq 'PT0S' -and
        [int]$settings.MultipleInstances -eq 2
    if (-not $AgentTaskCredential -and -not $PromptForAgentTaskCredential) {
        if ($actionMatches -and $settingsMatch) { return }
    }

    # Preserve identity, triggers, and other settings. Watch processes run
    # indefinitely; failed exits get bounded automatic recovery.
    $settings.RestartCount = 5
    $settings.RestartInterval = 'PT1M'
    $settings.ExecutionTimeLimit = 'PT0S'
    $settings.MultipleInstances = 2 # TASK_INSTANCES_IGNORE_NEW

    $principal = $task.Principal
    $credential = $AgentTaskCredential
    if ($PromptForAgentTaskCredential -and -not $credential) {
        $credential = Get-Credential -UserName $principal.UserId -Message "Windows credentials for the existing SiN Agent task account"
        if (-not $credential) { throw "Agent task credential entry was cancelled; Agent files and running process were not changed." }
    }
    $credentialParameters = @{}
    if ($credential) {
        if ([string]$principal.LogonType -ne 'Password') {
            throw "Agent task credentials are only supported for an existing Password logon task; the task identity and logon type will not be changed."
        }
        # Compare identities, including local/domain aliases, before supplying
        # a password. Never silently switch the scheduled task to another user.
        $identityType = [System.Security.Principal.SecurityIdentifier]
        $principalSid = if ([string]$principal.UserId -match '^S-1-') {
            [System.Security.Principal.SecurityIdentifier]::new([string]$principal.UserId)
        } else {
            [System.Security.Principal.NTAccount]::new([string]$principal.UserId).Translate($identityType)
        }
        $credentialSid = [System.Security.Principal.NTAccount]::new($credential.UserName).Translate($identityType)
        if ($principalSid.Value -ne $credentialSid.Value) {
            throw "The supplied credentials must belong to the existing Agent task account '$($principal.UserId)'."
        }
        $credentialParameters.User = [string]$principal.UserId
        $credentialParameters.Password = $credential.GetNetworkCredential().Password
    }
    try {
        Set-ScheduledTask -TaskName $AgentTaskName -Action $action -Settings $settings -TaskPath $task.TaskPath @credentialParameters -ErrorAction Stop | Out-Null
    } catch {
        throw "Could not update the Agent scheduled-task action for '$($principal.UserId)' (logon type $($principal.LogonType)): $($_.Exception.Message). For a Password logon task, rerun with -PromptForAgentTaskCredential and enter that account's Windows password. The task's saved Windows credentials may need to be refreshed in Task Scheduler. Agent files and running process were not changed."
    } finally {
        $credentialParameters.Clear()
        $credential = $null
    }
}

function Start-AgentTask {
    Start-ScheduledTask -TaskName $AgentTaskName -ErrorAction Stop
    Start-Sleep -Seconds 3
    $task = Get-ScheduledTask -TaskName $AgentTaskName -ErrorAction Stop
    if ($task.State -ne "Running") {
        $info = Get-ScheduledTaskInfo -TaskName $AgentTaskName
        throw "Agent scheduled task did not stay running (state=$($task.State), lastTaskResult=$($info.LastTaskResult))."
    }
}

New-Item -ItemType Directory -Force -Path $MailboxDir | Out-Null
Set-AgentTaskAction
Stop-AgentTask
Start-AgentTask
Write-Host "SiN Agent scheduled task '$AgentTaskName' restarted with backend $ApiUrl and canonical mailbox."
