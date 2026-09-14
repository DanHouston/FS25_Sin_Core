[CmdletBinding()]
param(
    [string]$Version = "latest",
    [string]$Repository = "DanHouston/FS25_Sin_Core",
    [string]$ModsPath = $env:SIN_FS25_CLIENT_MODS_DIR
)

$ErrorActionPreference = "Stop"

function Get-Release($releaseVersion) {
    $headers = @{ "User-Agent" = "SiN-FS25-Client-Updater"; "Accept" = "application/vnd.github+json" }
    if ($releaseVersion -eq "latest") {
        $uri = "https://api.github.com/repos/$Repository/releases/latest"
    } else {
        if ($releaseVersion -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') {
            throw "Version must be latest or an exact vMAJOR.MINOR.PATCH tag"
        }
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

function Get-ExpectedHash($checksumFile, $name) {
    $line = Get-Content -LiteralPath $checksumFile |
        Where-Object { $_ -match ("\s" + [regex]::Escape($name) + "$") } |
        Select-Object -First 1
    if (-not $line -or $line -notmatch '^([0-9a-fA-F]{64})\s+(.+)$') {
        throw "Checksum entry missing for $name"
    }
    return $Matches[1].ToLowerInvariant()
}

if (-not $ModsPath) {
    throw "FS25 mods path is required. Pass -ModsPath or set SIN_FS25_CLIENT_MODS_DIR."
}

$temporary = Join-Path ([IO.Path]::GetTempPath()) ("sin-client-" + [guid]::NewGuid().ToString("N"))
try {
    New-Item -ItemType Directory -Force -Path $temporary | Out-Null
    $release = Get-Release $Version
    $resolvedVersion = [string]$release.tag_name
    $zipPath = Join-Path $temporary "FS25_SiN_Server.zip"
    $sumsPath = Join-Path $temporary "SHA256SUMS.txt"
    Download-Asset $release "FS25_SiN_Server.zip" $zipPath
    Download-Asset $release "SHA256SUMS.txt" $sumsPath

    $expected = Get-ExpectedHash $sumsPath "FS25_SiN_Server.zip"
    $actual = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($expected -ne $actual) { throw "FS25_SiN_Server checksum mismatch" }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $names = @($archive.Entries | ForEach-Object { $_.FullName })
        if ($names -notcontains "modDesc.xml" -or $names -notcontains "NetworkLocal.lua") {
            throw "FS25_SiN_Server ZIP is missing required root files"
        }
    } finally { $archive.Dispose() }

    New-Item -ItemType Directory -Force -Path $ModsPath | Out-Null
    $destination = Join-Path $ModsPath "FS25_SiN_Server.zip"
    $staged = Join-Path $ModsPath "FS25_SiN_Server.zip.new"
    $previous = Join-Path $ModsPath "FS25_SiN_Server.zip.previous"
    $legacy = Join-Path $ModsPath "FS25_SiN_NetworkLocal.zip"
    Copy-Item -LiteralPath $zipPath -Destination $staged -Force
    if ((Get-FileHash -LiteralPath $staged -Algorithm SHA256).Hash.ToLowerInvariant() -ne $actual) {
        Remove-Item -LiteralPath $staged -Force
        throw "Staged FS25_SiN_Server checksum mismatch"
    }
    if (Test-Path -LiteralPath $previous) { Remove-Item -LiteralPath $previous -Force }
    if (Test-Path -LiteralPath $destination) { Move-Item -LiteralPath $destination -Destination $previous }
    try {
        Move-Item -LiteralPath $staged -Destination $destination
    } catch {
        if (Test-Path -LiteralPath $previous) { Move-Item -LiteralPath $previous -Destination $destination -Force }
        throw
    }
    Write-Host "SiN Client Mod"
    Write-Host "Release            $resolvedVersion"
    if (Test-Path -LiteralPath $legacy) { Remove-Item -LiteralPath $legacy -Force }
    Write-Host "FS25_SiN_Server     UPDATED"
    Write-Host "SHA256             $actual"
    Write-Host "FS25 reload        REQUIRED"
} finally {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue }
}
