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
$destinationMods = Join-Path $destinationResolved "mods"
$sourcePrefix = $sourceResolved.TrimEnd('\') + '\'
if ($destinationResolved.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination must not be inside the server mod folder."
}
if ([IO.Path]::GetFullPath($destinationMods).Equals($sourceResolved, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination mods folder must differ from the server mod folder."
}

$mods = @(Get-ChildItem -LiteralPath $sourceResolved -Filter "*.zip" -File | Sort-Object Name)
if ($mods.Count -eq 0) { throw "No .zip mods found in $sourceResolved" }

$runId = [guid]::NewGuid().ToString("N")
$stage = Join-Path $destinationParent (".sin-modpack-stage-" + $runId)
$backup = Join-Path $destinationParent (".sin-modpack-backup-" + $runId)
$incomingMods = Join-Path $stage "incoming-mods"
$packMods = Join-Path $stage "mods"
$destinationPack = Join-Path $Destination $PackName
$updatedNames = @()
$addedNames = @()
$metadataReplaced = @()

try {
    New-Item -ItemType Directory -Force -Path $incomingMods | Out-Null
    New-Item -ItemType Directory -Force -Path $backup | Out-Null

    # Validate and stage only the source ZIPs. Existing destination-only mods
    # remain part of the approved pack and are never removed by this publisher.
    foreach ($mod in $mods) {
        Assert-Zip $mod.FullName
        $target = Join-Path $incomingMods $mod.Name
        Copy-Item -LiteralPath $mod.FullName -Destination $target
    }

    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    New-Item -ItemType Directory -Force -Path $destinationMods | Out-Null

    foreach ($mod in $mods) {
        $incoming = Join-Path $incomingMods $mod.Name
        $target = Join-Path $destinationMods $mod.Name
        if ((Test-Path -LiteralPath $target -PathType Leaf) -and (Get-Sha256 $target) -eq (Get-Sha256 $incoming)) {
            continue
        }
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            Copy-Item -LiteralPath $target -Destination (Join-Path $backup $mod.Name)
            $updatedNames += $mod.Name
        } else {
            $addedNames += $mod.Name
        }
        Move-Item -LiteralPath $incoming -Destination $target -Force
    }

    # Build the new manifest and pack from the complete merged destination,
    # not just the changed inputs, so untouched mods remain available to clients.
    $destinationModsList = @(Get-ChildItem -LiteralPath $destinationMods -Filter "*.zip" -File | Sort-Object Name)
    if ($destinationModsList.Count -eq 0) { throw "No .zip mods found in $destinationMods" }
    New-Item -ItemType Directory -Force -Path $packMods | Out-Null
    $records = @()
    foreach ($mod in $destinationModsList) {
        Assert-Zip $mod.FullName
        $packMod = Join-Path $packMods $mod.Name
        Copy-Item -LiteralPath $mod.FullName -Destination $packMod
        $records += [ordered]@{
            filename = $mod.Name
            size = [int64](Get-Item -LiteralPath $mod.FullName).Length
            sha256 = Get-Sha256 $mod.FullName
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
    Compress-Archive -LiteralPath $manifestPath, $packMods -DestinationPath $stagePack -CompressionLevel Fastest
    Assert-Zip $stagePack

    foreach ($name in @("manifest.json", $PackName)) {
        $old = Join-Path $Destination $name
        if (Test-Path -LiteralPath $old -PathType Leaf) {
            Move-Item -LiteralPath $old -Destination (Join-Path $backup $name)
        }
        $metadataReplaced += $name
        $staged = if ($name -eq "manifest.json") { $manifestPath } else { $stagePack }
        Move-Item -LiteralPath $staged -Destination (Join-Path $Destination $name)
    }
    Remove-Item -LiteralPath $backup -Recurse -Force

    $publishedManifest = Join-Path $Destination "manifest.json"
    [pscustomobject]@{
        destination = $Destination
        server_key = $ServerKey
        mod_count = $records.Count
        added_mod_count = $addedNames.Count
        updated_mod_count = $updatedNames.Count
        manifest = $publishedManifest
        manifest_sha256 = Get-Sha256 $publishedManifest
        modpack = $destinationPack
        modpack_sha256 = Get-Sha256 $destinationPack
    } | ConvertTo-Json
} catch {
    New-Item -ItemType Directory -Force -Path $destinationMods | Out-Null
    foreach ($name in $updatedNames) {
        $target = Join-Path $destinationMods $name
        $saved = Join-Path $backup $name
        if (Test-Path -LiteralPath $saved -PathType Leaf) {
            Move-Item -LiteralPath $saved -Destination $target -Force
        }
    }
    foreach ($name in $addedNames) {
        $target = Join-Path $destinationMods $name
        if (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target -Force }
    }
    foreach ($name in $metadataReplaced) {
        $target = Join-Path $Destination $name
        $saved = Join-Path $backup $name
        if (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target -Force }
        if (Test-Path -LiteralPath $saved -PathType Leaf) { Move-Item -LiteralPath $saved -Destination $target -Force }
    }
    throw
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue }
}
