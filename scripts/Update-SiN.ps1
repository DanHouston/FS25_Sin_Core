[CmdletBinding()]
param(
    [string]$Version = "latest",
    [string]$Repository = "DanHouston/FS25_Sin_Core",
    [string]$AgentRoot = "C:\SiN\Agent",
    [string]$DeployRoot = "C:\SiN\Deploy",
    [string]$BackupRoot = "C:\SiN\Backups",
    [string]$DownloadRoot = "C:\SiN\Downloads",
    [string]$MailboxDir = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\modSettings\FS25SiNNetworkLocal",
    [string]$ApiUrl = "http://192.168.1.185:8080",
    [double]$PollInterval = 2,
    [string]$ModsPath = $env:SIN_FS25_MODS_DIR,
    [int]$KeepBackups = 5,
    [switch]$Rollback
)

$ErrorActionPreference = "Stop"
$assets = @("sin-agent.zip", "FS25_SiN_NetworkLocal.zip", "build-manifest.json", "SHA256SUMS.txt", "Update-SiN.ps1")
$checksumAssets = @("sin-agent.zip", "FS25_SiN_NetworkLocal.zip", "Update-SiN.ps1")
$logRoot = "C:\SiN\Logs"

function Get-AgentProcess {
    @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match "fs25_network_core\.agent" -and $_.CommandLine -match "--watch" })
}

function Stop-Agent {
    $processes = Get-AgentProcess
    foreach ($process in $processes) {
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
}

function Get-Release($releaseVersion) {
    $headers = @{ "User-Agent" = "SiN-FS25-VM-Deployer"; "Accept" = "application/vnd.github+json" }
    $token = $env:GITHUB_TOKEN
    if ($token) { $headers["Authorization"] = "Bearer $token" }
    if ($releaseVersion -eq "latest") {
        $uri = "https://api.github.com/repos/$Repository/releases/latest"
    } else {
        if ($releaseVersion -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') { throw "Version must be latest or an exact vMAJOR.MINOR.PATCH tag" }
        $uri = "https://api.github.com/repos/$Repository/releases/tags/$releaseVersion"
    }
    try {
        $release = Invoke-RestMethod -Uri $uri -Headers $headers -Method Get
    } catch {
        throw "GitHub release lookup failed. Check repository, version, network, or rate limit."
    }
    if ($release.draft -eq $true) { throw "The selected GitHub release is a draft" }
    return $release
}

function Download-Asset($release, $name, $destination) {
    $asset = @($release.assets | Where-Object { $_.name -eq $name })
    if ($asset.Count -ne 1) { throw "Required release asset missing: $name" }
    try {
        Invoke-WebRequest -Uri $asset[0].browser_download_url -OutFile $destination -UseBasicParsing
    } catch {
        throw "Download failed for release asset: $name"
    }
}

function Verify-Checksums($directory) {
    $lines = Get-Content -LiteralPath (Join-Path $directory "SHA256SUMS.txt")
    foreach ($name in $checksumAssets) {
        $line = $lines | Where-Object { $_ -match ("\s" + [regex]::Escape($name) + "$") } | Select-Object -First 1
        if (-not $line -or $line -notmatch '^([0-9a-fA-F]{64})\s+(.+)$') { throw "Checksum entry missing for $name" }
        $expected = $Matches[1].ToLowerInvariant()
        $actual = (Get-FileHash -LiteralPath (Join-Path $directory $name) -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($expected -ne $actual) { throw "Checksum mismatch for $name" }
    }
}

function Test-AgentPackage($directory) {
    $extract = Join-Path $directory "agent-validate"
    Expand-Archive -LiteralPath (Join-Path $directory "sin-agent.zip") -DestinationPath $extract -Force
    if (-not (Test-Path (Join-Path $extract "fs25_network_core\agent.py"))) { throw "Agent package layout is invalid" }
    & python -m compileall -q (Join-Path $extract "fs25_network_core")
    if ($LASTEXITCODE -ne 0) { throw "Deployed Agent compile validation failed" }
    Get-ChildItem -LiteralPath $extract -Directory -Filter "__pycache__" -Recurse -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force
}

function Start-Agent {
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
    if ($running.Count -eq 0) { throw "Agent watcher did not start; inspect C:\SiN\Logs\agent-error.log" }
    if ($running.Count -gt 1) { throw "More than one Agent watcher is running" }
}

function Test-ApiReachable {
    $uri = [Uri]$ApiUrl
    $port = if ($uri.Port -gt 0) { $uri.Port } elseif ($uri.Scheme -eq "https") { 443 } else { 80 }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect($uri.Host, $port)
        return $true
    } catch { return $false } finally { $client.Dispose() }
}

function Resolve-ModsPath {
    if ($ModsPath) { return }
    $metadata = "C:\SiN\deployment.json"
    if (Test-Path $metadata) {
        $old = Get-Content $metadata -Raw | ConvertFrom-Json
        if ($old.networklocal_path) { $script:ModsPath = Split-Path -Parent $old.networklocal_path }
    }
    if (-not $ModsPath) { throw "FS25 mods path is required. Pass -ModsPath or set SIN_FS25_MODS_DIR; it is not guessed." }
}

function Write-Deployment($release, $manifest, $agentHash, $modHash, $modChanged, $backupPath) {
    $record = [ordered]@{
        version = [string]$release.tag_name
        deployed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        git_commit = [string]$manifest.git_commit
        agent_sha256 = $agentHash
        networklocal_sha256 = $modHash
        networklocal_path = (Join-Path $ModsPath "FS25_SiN_NetworkLocal.zip")
        fs25_restart_required = [bool]$modChanged
        backup_path = $backupPath
    }
    $record | ConvertTo-Json | Set-Content -LiteralPath "C:\SiN\deployment.json" -Encoding UTF8
}

function Invoke-Rollback {
    Resolve-ModsPath
    $backup = Get-ChildItem -LiteralPath $BackupRoot -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $backup) { throw "No rollback backup exists" }
    $savedCore = Join-Path $backup.FullName "fs25_network_core"
    $savedMod = Join-Path $backup.FullName "FS25_SiN_NetworkLocal.zip"
    if (-not (Test-Path $savedCore) -or -not (Test-Path $savedMod)) { throw "Latest backup is incomplete" }
    Stop-Agent
    $liveCore = Join-Path $AgentRoot "fs25_network_core"
    if (Test-Path $liveCore) { Remove-Item -LiteralPath $liveCore -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $AgentRoot | Out-Null
    Copy-Item -LiteralPath $savedCore -Destination $liveCore -Recurse
    New-Item -ItemType Directory -Force -Path $ModsPath | Out-Null
    $targetMod = Join-Path $ModsPath "FS25_SiN_NetworkLocal.zip"
    $changed = -not (Test-Path $targetMod) -or ((Get-FileHash $targetMod).Hash -ne (Get-FileHash $savedMod).Hash)
    Copy-Item -LiteralPath $savedMod -Destination $targetMod -Force
    Start-Agent
    Write-Host "Rollback restored $($backup.Name)"
    if ($changed) { Write-Host "FS25 RESTART REQUIRED" }
}

try {
    Resolve-ModsPath
    if ($Rollback) { Invoke-Rollback; exit 0 }
    $release = Get-Release $Version
    $resolvedVersion = [string]$release.tag_name
    $downloadDirectory = Join-Path $DownloadRoot $resolvedVersion
    if (Test-Path $downloadDirectory) { Remove-Item -LiteralPath $downloadDirectory -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $downloadDirectory | Out-Null
    foreach ($name in $assets) { Download-Asset $release $name (Join-Path $downloadDirectory $name) }
    Verify-Checksums $downloadDirectory
    $manifest = Get-Content -LiteralPath (Join-Path $downloadDirectory "build-manifest.json") -Raw | ConvertFrom-Json
    if ([int]$manifest.release_format_version -ne 1) { throw "Unsupported release format" }
    if ($manifest.agent_sha256.ToLowerInvariant() -ne (Get-FileHash (Join-Path $downloadDirectory "sin-agent.zip")).Hash.ToLowerInvariant()) { throw "Agent manifest hash mismatch" }
    if ($manifest.networklocal_sha256.ToLowerInvariant() -ne (Get-FileHash (Join-Path $downloadDirectory "FS25_SiN_NetworkLocal.zip")).Hash.ToLowerInvariant()) { throw "NetworkLocal manifest hash mismatch" }
    if ($manifest.updater_sha256.ToLowerInvariant() -ne (Get-FileHash (Join-Path $downloadDirectory "Update-SiN.ps1")).Hash.ToLowerInvariant()) { throw "Updater manifest hash mismatch" }
    Test-AgentPackage $downloadDirectory
    New-Item -ItemType Directory -Force -Path $DeployRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $downloadDirectory "Update-SiN.ps1") -Destination (Join-Path $DeployRoot "Update-SiN.next.ps1") -Force

    $timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $backup = Join-Path $BackupRoot "$timestamp-$resolvedVersion"
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    $liveCore = Join-Path $AgentRoot "fs25_network_core"
    if (Test-Path $liveCore) { Copy-Item -LiteralPath $liveCore -Destination (Join-Path $backup "fs25_network_core") -Recurse }
    $targetMod = Join-Path $ModsPath "FS25_SiN_NetworkLocal.zip"
    if (Test-Path $targetMod) { Copy-Item -LiteralPath $targetMod -Destination (Join-Path $backup "FS25_SiN_NetworkLocal.zip") }
    if (Test-Path "C:\SiN\deployment.json") { Copy-Item "C:\SiN\deployment.json" (Join-Path $backup "previous-deployment.json") }

    Stop-Agent
    if (Test-Path $liveCore) { Remove-Item -LiteralPath $liveCore -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $AgentRoot | Out-Null
    $agentExtract = Join-Path $downloadDirectory "agent-validate\fs25_network_core"
    Copy-Item -LiteralPath $agentExtract -Destination $liveCore -Recurse
    New-Item -ItemType Directory -Force -Path $ModsPath | Out-Null
    $oldModHash = if (Test-Path $targetMod) { (Get-FileHash $targetMod).Hash } else { "" }
    Copy-Item -LiteralPath (Join-Path $downloadDirectory "FS25_SiN_NetworkLocal.zip") -Destination $targetMod -Force
    $newModHash = (Get-FileHash $targetMod).Hash
    $modChanged = $oldModHash.ToLowerInvariant() -ne $newModHash.ToLowerInvariant()
    Write-Deployment $release $manifest $manifest.agent_sha256 $newModHash $modChanged $backup
    Start-Agent
    $apiReachable = Test-ApiReachable
    Get-ChildItem -LiteralPath $BackupRoot -Directory | Sort-Object LastWriteTime -Descending | Select-Object -Skip $KeepBackups |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "SiN Deployment"
    Write-Host "Release            $resolvedVersion"
    Write-Host "Commit             $($manifest.git_commit)"
    Write-Host "Agent              UPDATED"
    Write-Host "Agent running      YES"
    Write-Host "NetworkLocal       UPDATED"
    if ($modChanged) { Write-Host "FS25 restart       REQUIRED" } else { Write-Host "FS25 restart       NOT REQUIRED" }
    Write-Host "API reachable      $(if ($apiReachable) { 'YES' } else { 'NO' })"
    Write-Host "Rollback backup    $backup"
    Write-Host "Result             $(if ($apiReachable) { 'SUCCESS' } else { 'SUCCESS (API UNREACHABLE)' })"
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
