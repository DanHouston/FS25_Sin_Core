[CmdletBinding()]
param(
    [string]$SourceMods = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\mods",
    [string]$Destination = "G:\My Drive\SiN Mods\sin-fs25-01",
    [string]$ServerKey = "sin-fs25-01",
    [string]$ServerName = "SiN Test Server 01",
    [string]$PackName = "SiN Test Server 01-Modpack.zip"
)

$ErrorActionPreference = "Stop"

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-Zip([string]$Path) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        foreach ($entry in $archive.Entries) {
            if ($entry.FullName.EndsWith("/")) { continue }
            $stream = $entry.Open()
            try {
                $buffer = New-Object byte[] 65536
                while ($stream.Read($buffer, 0, $buffer.Length) -gt 0) { }
            } finally { $stream.Dispose() }
        }
    } finally { $archive.Dispose() }
}

if (-not (Test-Path -LiteralPath $SourceMods -PathType Container)) {
    throw "Server mod folder does not exist: $SourceMods"
}

$sourceResolved = (Resolve-Path -LiteralPath $SourceMods).Path
$destinationParent = Split-Path -Parent $Destination
if (-not $destinationParent) { throw "Destination must be a directory path: $Destination" }
New-Item -ItemType Directory -Force -Path $destinationParent | Out-Null
$destinationResolved = [IO.Path]::GetFullPath($Destination)
$sourcePrefix = $sourceResolved.TrimEnd('\') + '\'
if ($destinationResolved.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination must not be inside the server mod folder."
}

$mods = @(Get-ChildItem -LiteralPath $sourceResolved -Filter "*.zip" -File | Sort-Object Name)
if ($mods.Count -eq 0) { throw "No .zip mods found in $sourceResolved" }

$runId = [guid]::NewGuid().ToString("N")
$stage = Join-Path $destinationParent (".sin-modpack-stage-" + $runId)
$backup = Join-Path $destinationParent (".sin-modpack-backup-" + $runId)
$stageMods = Join-Path $stage "mods"
$destinationPack = Join-Path $Destination $PackName

try {
    New-Item -ItemType Directory -Force -Path $stageMods | Out-Null
    $records = @()
    foreach ($mod in $mods) {
        Assert-Zip $mod.FullName
        $target = Join-Path $stageMods $mod.Name
        Copy-Item -LiteralPath $mod.FullName -Destination $target
        $records += [ordered]@{
            filename = $mod.Name
            size = [int64](Get-Item -LiteralPath $target).Length
            sha256 = Get-Sha256 $target
        }
    }

    $manifest = [ordered]@{
        manifest_schema = "sin.fs25-modpack/1"
        server_key = $ServerKey
        server_display_name = $ServerName
        modpack_filename = $PackName
        mods = @($records)
    }
    $manifestPath = Join-Path $stage "manifest.json"
    $manifestJson = ($manifest | ConvertTo-Json -Depth 6) + [Environment]::NewLine
    [IO.File]::WriteAllText($manifestPath, $manifestJson, (New-Object Text.UTF8Encoding($false)))

    $stagePack = Join-Path $stage $PackName
    Compress-Archive -LiteralPath $manifestPath, $stageMods -DestinationPath $stagePack -CompressionLevel Fastest
    Assert-Zip $stagePack

    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    if (Test-Path -LiteralPath $Destination -PathType Container) {
        foreach ($name in @("mods", "manifest.json", $PackName)) {
            $old = Join-Path $Destination $name
            if (Test-Path -LiteralPath $old) { Move-Item -LiteralPath $old -Destination $backup }
        }
    } else {
        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    }

    Move-Item -LiteralPath $stageMods -Destination (Join-Path $Destination "mods")
    Move-Item -LiteralPath $manifestPath -Destination (Join-Path $Destination "manifest.json")
    Move-Item -LiteralPath $stagePack -Destination $destinationPack
    Remove-Item -LiteralPath $backup -Recurse -Force

    $publishedManifest = Join-Path $Destination "manifest.json"
    [pscustomobject]@{
        destination = $Destination
        server_key = $ServerKey
        mod_count = $records.Count
        manifest = $publishedManifest
        manifest_sha256 = Get-Sha256 $publishedManifest
        modpack = $destinationPack
        modpack_sha256 = Get-Sha256 $destinationPack
    } | ConvertTo-Json
} catch {
    if (Test-Path -LiteralPath $backup -PathType Container) {
        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
        foreach ($name in @("mods", "manifest.json", $PackName)) {
            $old = Join-Path $Destination $name
            $saved = Join-Path $backup $name
            if (Test-Path -LiteralPath $saved) {
                if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
                Move-Item -LiteralPath $saved -Destination $old
            }
        }
    }
    throw
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue }
}
