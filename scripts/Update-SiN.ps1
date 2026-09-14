[CmdletBinding()]
param(
    [string]$Version = "latest",
    [string]$Repository = "DanHouston/FS25_Sin_Core",
    [string]$AgentRoot = "C:\SiN\Agent",
    [string]$DeployRoot = "C:\SiN\Deploy",
    [string]$BackupRoot = "C:\SiN\Backups",
    [string]$DownloadRoot = "C:\SiN\Downloads",
    [string]$MailboxDir = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\modSettings\FS25_SiN_Server",
    [string]$ApiUrl = "http://192.168.1.185:8080",
    [double]$PollInterval = 2,
    [string]$ModsPath = $env:SIN_FS25_MODS_DIR,
    [int]$KeepBackups = 5,
    [switch]$Rollback,
    [switch]$MigrateLegacyMailbox,
    [switch]$MigrationOnly
)

$ErrorActionPreference = "Stop"
$assets = @("sin-agent.zip", "FS25_SiN_Server.zip", "build-manifest.json", "SHA256SUMS.txt", "Update-SiN.ps1")
$checksumAssets = @("sin-agent.zip", "FS25_SiN_Server.zip", "Update-SiN.ps1")
$logRoot = "C:\SiN\Logs"

function Get-AgentProcess {
    @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match "fs25_network_core\.agent" -and $_.CommandLine -match "--watch" })
}

function Assert-CanonicalMailbox {
    $leaf = Split-Path -Leaf ($MailboxDir.TrimEnd([char]92, [char]47))
    if ($leaf -ne "FS25_SiN_Server") {
        throw "MailboxDir must be the canonical FS25_SiN_Server mailbox root."
    }
}

function Get-MailboxFiles {
    param([Parameter(Mandatory = $true)][string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { return @() }
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd([char]92, [char]47)
    return @(Get-ChildItem -LiteralPath $rootFull -Force -Recurse -File | ForEach-Object {
        $relative = $_.FullName.Substring($rootFull.Length).TrimStart([char]92, [char]47)
        [pscustomobject]@{
            RelativePath = $relative
            FullName = $_.FullName
            Length = [int64]$_.Length
            LastWriteTimeUtc = $_.LastWriteTimeUtc
            Hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    })
}

function Get-MailboxBindingHash {
    param(
        [Parameter(Mandatory = $true)][object[]]$Files,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $bindings = @($Files | Where-Object { $_.RelativePath.ToLowerInvariant() -eq "serverbinding.xml" })
    if ($bindings.Count -ne 1 -or $bindings[0].Length -le 0) {
        throw "Legacy mailbox '$Root' is non-empty but does not contain exactly one non-empty serverBinding.xml."
    }
    return $bindings[0].Hash
}

function Test-ValidMailboxXml {
    param([Parameter(Mandatory = $true)][object]$File)

    if ($File.Length -le 0) { return $false }
    try {
        [xml](Get-Content -LiteralPath $File.FullName -Raw) | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Select-RuntimeMailboxFile {
    param(
        [Parameter(Mandatory = $true)][object]$Current,
        [Parameter(Mandatory = $true)][object]$Candidate,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    $currentValid = Test-ValidMailboxXml -File $Current
    $candidateValid = Test-ValidMailboxXml -File $Candidate
    if (-not $currentValid -and -not $candidateValid) {
        throw "Runtime mailbox file '$RelativePath' is not valid XML in either legacy directory."
    }
    if ($candidateValid -and -not $currentValid) { return $Candidate }
    if ($currentValid -and -not $candidateValid) { return $Current }
    if ($Candidate.LastWriteTimeUtc -gt $Current.LastWriteTimeUtc) { return $Candidate }
    if ($Current.LastWriteTimeUtc -gt $Candidate.LastWriteTimeUtc) { return $Current }
    if ([StringComparer]::OrdinalIgnoreCase.Compare($Candidate.FullName, $Current.FullName) -lt 0) {
        return $Candidate
    }
    return $Current
}

function Resolve-MailboxUnion {
    param([Parameter(Mandatory = $true)][object[]]$Roots)

    $selected = @{}
    foreach ($root in ($Roots | Sort-Object Path)) {
        foreach ($file in ($root.Files | Sort-Object RelativePath)) {
            $key = $file.RelativePath.ToLowerInvariant()
            if (-not $selected.ContainsKey($key)) {
                $selected[$key] = $file
                continue
            }
            $current = $selected[$key]
            if ($current.Hash -eq $file.Hash) { continue }
            if ($key -eq "snapshot.xml" -or $key -eq "clock-policy.xml") {
                $selected[$key] = Select-RuntimeMailboxFile -Current $current -Candidate $file -RelativePath $file.RelativePath
                continue
            }
            if ($key -eq "manager-authority.xml") {
                throw "Conflicting manager-authority.xml state was found; refusing to select by timestamp."
            }
            if ($key -eq "serverbinding.xml") {
                throw "Conflicting serverBinding.xml state was found; refusing to select a credential."
            }
            throw "Conflicting durable mailbox file '$($file.RelativePath)' was found; refusing to choose a copy."
        }
    }
    return @($selected.Values | Sort-Object RelativePath)
}

function Write-MailboxStage {
    param(
        [Parameter(Mandatory = $true)][object[]]$Files,
        [Parameter(Mandatory = $true)][string]$Stage
    )

    if (Test-Path -LiteralPath $Stage) {
        $existing = @(Get-MailboxFiles -Root $Stage)
        if ($existing.Count -ne 0) {
            throw "Mailbox staging directory is non-empty and cannot be replaced safely: $Stage"
        }
        Remove-Item -LiteralPath $Stage -Force
    }
    New-Item -ItemType Directory -Force -Path $Stage | Out-Null
    foreach ($file in $Files) {
        $target = Join-Path $Stage $file.RelativePath
        $targetParent = Split-Path -Parent $target
        New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $target
    }
    $stagedFiles = @(Get-MailboxFiles -Root $Stage)
    if ($stagedFiles.Count -ne $Files.Count) {
        throw "Mailbox staging validation failed because files were not fully materialized."
    }
    return $stagedFiles
}

function Assert-StageCoversRoots {
    param(
        [Parameter(Mandatory = $true)][object[]]$Roots,
        [Parameter(Mandatory = $true)][object[]]$StageFiles
    )

    $stageByPath = @{}
    foreach ($file in $StageFiles) { $stageByPath[$file.RelativePath.ToLowerInvariant()] = $file }
    foreach ($root in $Roots) {
        foreach ($file in $root.Files) {
            $key = $file.RelativePath.ToLowerInvariant()
            if (-not $stageByPath.ContainsKey($key)) {
                throw "Interrupted mailbox staging is missing durable file '$($file.RelativePath)'."
            }
            $staged = $stageByPath[$key]
            if ($staged.Hash -eq $file.Hash) { continue }
            if ($key -eq "snapshot.xml" -or $key -eq "clock-policy.xml") { continue }
            throw "Interrupted mailbox staging conflicts with durable file '$($file.RelativePath)'."
        }
    }
}

function New-MailboxArchiveBatch {
    param([Parameter(Mandatory = $true)][string]$Parent)

    $archiveRoot = Join-Path $Parent "FS25_SiN_Server.migration-archive"
    New-Item -ItemType Directory -Force -Path $archiveRoot | Out-Null
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmssfff")
    $batch = Join-Path $archiveRoot ("migration-" + $stamp)
    $suffix = 1
    while (Test-Path -LiteralPath $batch) {
        $batch = Join-Path $archiveRoot ("migration-" + $stamp + "-" + $suffix)
        $suffix++
    }
    New-Item -ItemType Directory -Force -Path $batch | Out-Null
    return $batch
}

function Archive-MailboxRoots {
    param(
        [Parameter(Mandatory = $true)][string[]]$Roots,
        [Parameter(Mandatory = $true)][string]$Batch
    )

    foreach ($root in ($Roots | Sort-Object)) {
        $target = Join-Path $Batch (Split-Path -Leaf $root)
        Move-MailboxDirectory -Source $root -Destination $target
    }
}

function Move-MailboxDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $attempts = 5
    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        try {
            Move-Item -LiteralPath $Source -Destination $Destination
            return
        } catch {
            $isTransientFileLock = $_.Exception -is [UnauthorizedAccessException] -or
                $_.Exception -is [IO.IOException]
            if (-not $isTransientFileLock -or $attempt -eq $attempts) { throw }
            Start-Sleep -Milliseconds (200 * $attempt)
        }
    }
}

function Invoke-LegacyMailboxMigration {
    param([Parameter(Mandatory = $true)][string]$Destination)

    $destination = [IO.Path]::GetFullPath($Destination).TrimEnd([char]92, [char]47)
    $parent = Split-Path -Parent $destination
    $stage = $destination + ".migrating"
    $legacyCandidates = @(
        (Join-Path $parent "FS25_SiN_NetworkLocal"),
        (Join-Path $parent "FS25SiNNetworkLocal")
    )
    $legacyPaths = @($legacyCandidates | Where-Object {
        -not [StringComparer]::OrdinalIgnoreCase.Equals($_, $destination) -and
        -not [StringComparer]::OrdinalIgnoreCase.Equals($_, $stage) -and
        (Test-Path -LiteralPath $_ -PathType Container)
    })

    $destinationExists = Test-Path -LiteralPath $destination -PathType Container
    $destinationFiles = if ($destinationExists) { @(Get-MailboxFiles -Root $destination) } else { @() }
    # A legacy directory can be recreated by an old mod's startup code with
    # only its mailbox subdirectories.  Directory existence is not durable
    # mailbox state: only files participate in the binding/conflict rules.
    # Classify the roots before deciding whether a populated canonical mailbox
    # is in conflict with legacy state.
    $nonEmptyLegacyPaths = @()
    $emptyLegacyPaths = @()
    foreach ($path in $legacyPaths) {
        if (@(Get-MailboxFiles -Root $path).Count -eq 0) {
            $emptyLegacyPaths += $path
        } else {
            $nonEmptyLegacyPaths += $path
        }
    }
    if ($destinationFiles.Count -ne 0) {
        if ($nonEmptyLegacyPaths.Count -ne 0) {
            throw "Canonical mailbox is already populated while legacy mailbox directories remain; refusing to merge or overwrite."
        }
        if (Test-Path -LiteralPath $stage -PathType Container) {
            $staleStage = @(Get-MailboxFiles -Root $stage)
            $batch = New-MailboxArchiveBatch -Parent $parent
            Move-MailboxDirectory -Source $stage -Destination (Join-Path $batch (Split-Path -Leaf $stage))
        }
        if ($emptyLegacyPaths.Count -ne 0) {
            $batch = New-MailboxArchiveBatch -Parent $parent
            Archive-MailboxRoots -Roots $emptyLegacyPaths -Batch $batch
        }
        Write-Host "Canonical mailbox already exists; migration is complete."
        return
    }
    if ($destinationExists) { Remove-Item -LiteralPath $destination -Force }

    $rootInfos = @()
    $emptyRoots = @()
    foreach ($path in $legacyPaths) {
        $files = @(Get-MailboxFiles -Root $path)
        if ($files.Count -eq 0) {
            $emptyRoots += $path
            continue
        }
        $rootInfos += [pscustomobject]@{
            Path = $path
            Files = $files
            BindingHash = Get-MailboxBindingHash -Files $files -Root $path
        }
    }

    $bindingHash = $null
    foreach ($root in $rootInfos) {
        if ($null -eq $bindingHash) { $bindingHash = $root.BindingHash; continue }
        if ($bindingHash -ne $root.BindingHash) {
            throw "Legacy mailbox directories contain different server bindings; refusing to consolidate them."
        }
    }

    $stageExists = Test-Path -LiteralPath $stage -PathType Container
    $stageFiles = if ($stageExists) { @(Get-MailboxFiles -Root $stage) } else { @() }
    if ($stageFiles.Count -ne 0) {
        $stageBinding = Get-MailboxBindingHash -Files $stageFiles -Root $stage
        if ($null -ne $bindingHash -and $stageBinding -ne $bindingHash) {
            throw "Mailbox staging contains a different server binding; refusing to resume it."
        }
        if ($null -eq $bindingHash) { $bindingHash = $stageBinding }
        foreach ($runtimeFile in @($stageFiles | Where-Object {
            $_.RelativePath.ToLowerInvariant() -eq "snapshot.xml" -or
            $_.RelativePath.ToLowerInvariant() -eq "clock-policy.xml"
        })) {
            if (-not (Test-ValidMailboxXml -File $runtimeFile)) {
                throw "Mailbox staging contains invalid runtime XML '$($runtimeFile.RelativePath)'."
            }
        }
        if ($rootInfos.Count -ne 0) {
            Assert-StageCoversRoots -Roots $rootInfos -StageFiles $stageFiles
        }
    } elseif ($stageExists) {
        Remove-Item -LiteralPath $stage -Force
    }

    if ($stageFiles.Count -eq 0) {
        if ($rootInfos.Count -eq 0) {
            New-Item -ItemType Directory -Force -Path $destination | Out-Null
            $archiveRoots = @($emptyRoots)
        } else {
            $union = Resolve-MailboxUnion -Roots $rootInfos
            $stageFiles = Write-MailboxStage -Files $union -Stage $stage
            $archiveRoots = @($rootInfos | ForEach-Object { $_.Path }) + $emptyRoots
        }
    } else {
        $archiveRoots = @($rootInfos | ForEach-Object { $_.Path }) + $emptyRoots
    }

    if ($archiveRoots.Count -ne 0) {
        $batch = New-MailboxArchiveBatch -Parent $parent
        Archive-MailboxRoots -Roots $archiveRoots -Batch $batch
    }
    if (-not (Test-Path -LiteralPath $destination -PathType Container)) {
        if (-not (Test-Path -LiteralPath $stage -PathType Container)) {
            New-Item -ItemType Directory -Force -Path $destination | Out-Null
        } else {
            Move-MailboxDirectory -Source $stage -Destination $destination
        }
    }
    $finalFiles = @(Get-MailboxFiles -Root $destination)
    if ($bindingHash -ne $null) {
        $finalBinding = Get-MailboxBindingHash -Files $finalFiles -Root $destination
        if ($finalBinding -ne $bindingHash) {
            throw "Canonical mailbox binding validation failed after migration."
        }
    }
    Write-Host "Legacy mailbox state consolidated into the canonical mailbox."
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
        if ($old.server_path) { $script:ModsPath = Split-Path -Parent $old.server_path }
        # Read the old deployment metadata key only to locate a pre-rename
        # installation; new records use server_path above.
        elseif ($old.networklocal_path) { $script:ModsPath = Split-Path -Parent $old.networklocal_path }
    }
    if (-not $ModsPath) { throw "FS25 mods path is required. Pass -ModsPath or set SIN_FS25_MODS_DIR; it is not guessed." }
}

function Write-Deployment($release, $manifest, $agentHash, $modHash, $modChanged, $backupPath) {
    $record = [ordered]@{
        version = [string]$release.tag_name
        deployed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        git_commit = [string]$manifest.git_commit
        agent_sha256 = $agentHash
        server_sha256 = $modHash
        server_path = (Join-Path $ModsPath "FS25_SiN_Server.zip")
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
    $savedMod = Join-Path $backup.FullName "FS25_SiN_Server.zip"
    if (-not (Test-Path $savedMod)) { $savedMod = Join-Path $backup.FullName "FS25_SiN_NetworkLocal.zip" }
    if (-not (Test-Path $savedCore) -or -not (Test-Path $savedMod)) { throw "Latest backup is incomplete" }
    Stop-Agent
    $liveCore = Join-Path $AgentRoot "fs25_network_core"
    if (Test-Path $liveCore) { Remove-Item -LiteralPath $liveCore -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $AgentRoot | Out-Null
    Copy-Item -LiteralPath $savedCore -Destination $liveCore -Recurse
    New-Item -ItemType Directory -Force -Path $ModsPath | Out-Null
    $targetMod = Join-Path $ModsPath "FS25_SiN_Server.zip"
    $legacyMod = Join-Path $ModsPath "FS25_SiN_NetworkLocal.zip"
    if (Test-Path -LiteralPath $legacyMod) { Remove-Item -LiteralPath $legacyMod -Force }
    $changed = -not (Test-Path $targetMod) -or ((Get-FileHash $targetMod).Hash -ne (Get-FileHash $savedMod).Hash)
    Copy-Item -LiteralPath $savedMod -Destination $targetMod -Force
    Start-Agent
    Write-Host "Rollback restored $($backup.Name)"
    if ($changed) { Write-Host "FS25 RESTART REQUIRED" }
}

try {
    Assert-CanonicalMailbox
    if ($MigrationOnly) {
        Invoke-LegacyMailboxMigration -Destination $MailboxDir
        exit 0
    }
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
    if ($manifest.server_sha256.ToLowerInvariant() -ne (Get-FileHash (Join-Path $downloadDirectory "FS25_SiN_Server.zip")).Hash.ToLowerInvariant()) { throw "FS25_SiN_Server manifest hash mismatch" }
    if ($manifest.updater_sha256.ToLowerInvariant() -ne (Get-FileHash (Join-Path $downloadDirectory "Update-SiN.ps1")).Hash.ToLowerInvariant()) { throw "Updater manifest hash mismatch" }
    Test-AgentPackage $downloadDirectory
    New-Item -ItemType Directory -Force -Path $DeployRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $downloadDirectory "Update-SiN.ps1") -Destination (Join-Path $DeployRoot "Update-SiN.next.ps1") -Force

    $timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $backup = Join-Path $BackupRoot "$timestamp-$resolvedVersion"
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    $liveCore = Join-Path $AgentRoot "fs25_network_core"
    if (Test-Path $liveCore) { Copy-Item -LiteralPath $liveCore -Destination (Join-Path $backup "fs25_network_core") -Recurse }
    $targetMod = Join-Path $ModsPath "FS25_SiN_Server.zip"
    $legacyMod = Join-Path $ModsPath "FS25_SiN_NetworkLocal.zip"
    if (Test-Path $targetMod) { Copy-Item -LiteralPath $targetMod -Destination (Join-Path $backup "FS25_SiN_Server.zip") }
    if (Test-Path $legacyMod) { Copy-Item -LiteralPath $legacyMod -Destination (Join-Path $backup "FS25_SiN_NetworkLocal.zip") }
    if (Test-Path "C:\SiN\deployment.json") { Copy-Item "C:\SiN\deployment.json" (Join-Path $backup "previous-deployment.json") }

    Stop-Agent
    # Migration is automatic so the Agent cannot start against an empty new
    # mailbox while durable work remains under the old root. The legacy switch
    # remains accepted for command-line compatibility.
    Invoke-LegacyMailboxMigration -Destination $MailboxDir
    if (Test-Path $liveCore) { Remove-Item -LiteralPath $liveCore -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $AgentRoot | Out-Null
    $agentExtract = Join-Path $downloadDirectory "agent-validate\fs25_network_core"
    Copy-Item -LiteralPath $agentExtract -Destination $liveCore -Recurse
    New-Item -ItemType Directory -Force -Path $ModsPath | Out-Null
    $oldModHash = if (Test-Path $targetMod) { (Get-FileHash $targetMod).Hash } else { "" }
    if (Test-Path $legacyMod) { Remove-Item -LiteralPath $legacyMod -Force }
    Copy-Item -LiteralPath (Join-Path $downloadDirectory "FS25_SiN_Server.zip") -Destination $targetMod -Force
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
    Write-Host "FS25_SiN_Server     UPDATED"
    if ($modChanged) { Write-Host "FS25 restart       REQUIRED" } else { Write-Host "FS25 restart       NOT REQUIRED" }
    Write-Host "API reachable      $(if ($apiReachable) { 'YES' } else { 'NO' })"
    Write-Host "Rollback backup    $backup"
    Write-Host "Result             $(if ($apiReachable) { 'SUCCESS' } else { 'SUCCESS (API UNREACHABLE)' })"
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
