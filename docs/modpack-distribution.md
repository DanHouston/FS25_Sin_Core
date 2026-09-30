# SiN modpack distribution

The modpack publisher is intentionally a small filesystem operation. It does
not use the Central API, Google Drive API, an approval allowlist, versioned
`Current`/`Releases` directories, or the Python modpack module.

Run it on the server machine after the server mod folder contains the exact
ZIPs that should be distributed. The mounted Google Drive folder is simply the
destination filesystem.

## Output

For the default server paths the command creates or replaces only these three
managed items:

```text
G:\\My Drive\\SiN Mods\\sin-fs25-01\\
    mods\\
        every ZIP from the server mod folder
    manifest.json
    SiN Test Server 01-Modpack.zip
```

The combined ZIP contains `manifest.json` at its root and each copied mod at
`mods/<filename>`. The manifest records the server identity plus each mod's
filename, byte size, and SHA-256. Source ZIPs are never modified.

## One command

The release installs `Publish-SiN-Modpack.ps1` into `C:\\SiN\\Deploy`. Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "C:\\SiN\\Deploy\\Publish-SiN-Modpack.ps1"
```

Explicit paths are useful when validating a different machine or mounted drive:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "C:\\SiN\\Deploy\\Publish-SiN-Modpack.ps1" `
  -SourceMods "C:\\Users\\SiNAdmin\\Documents\\My Games\\FarmingSimulator2025\\mods" `
  -Destination "G:\\My Drive\\SiN Mods\\sin-fs25-01" `
  -ServerKey "sin-fs25-01" `
  -ServerName "SiN Test Server 01"
```

The command fails if the source folder is missing, contains no ZIPs, contains
an invalid ZIP, or is the destination itself. It stages and validates the
complete output before replacing the managed items, and prints the manifest and
combined-ZIP hashes on success. Existing unrelated files in the destination
are left alone.

The old approval/capture/refresh publisher is no longer part of the operator
workflow. `Update-SiN-Client.ps1` only updates the client SiN server mod; it
does not publish a modpack.
