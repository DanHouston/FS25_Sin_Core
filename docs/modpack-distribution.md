# Deterministic FS25 modpack distribution

SiN modpacks are filesystem publications. Google Drive is used only as a
mounted/synchronized filesystem; this feature does not use a Google API.

The publisher uses the existing Central `sin_servers` record as its server
identity. A production caller passes the record returned by
`ServerRegistry.info(server_key)` (`server_key` and `display_name`). It does not
maintain a second server roster.

## Publication layout

For a server record such as `server_key=sin-fs25-01` and
`display_name=SiN Test Server 01`, a release is:

```text
<publication-root>\sin-fs25-01\Current\
    SiN Test Server 01-Modpack.zip
    manifest.json
    mods\
        FS25_SiN_Server.zip
        ThirdPartyMod.zip
<publication-root>\sin-fs25-01\Releases\2026.09.22\
    SiN Test Server 01-Modpack.zip
    manifest.json
    mods\
        ...
```

The individual files under `mods` are copied byte-for-byte from the approved
source directory. The combined ZIP contains those same files at
`mods/<filename>`. `manifest.json` is canonical JSON and contains the server
identity, version, combined ZIP name, and for every selected mod its filename,
size, and SHA-256. It contains no capture timestamp, so identical approved
inputs produce identical output bytes.

## Configuration

No operator path is compiled into the code. Configure paths with environment
variables or pass the command-line options:

```powershell
$env:SIN_MODPACK_SOURCE_DIR = '<tested-client-mod-directory>'
$env:SIN_MODPACK_PUBLICATION_ROOT = 'H:\My Drive\SiN Mods'
$env:SIN_MODPACK_CLIENT_MOD_DIR = '<client-mod-directory-for-sync>'
```

The current operator publication root is `H:\My Drive\SiN Mods`.  The drive
letter is not part of the packager contract: pass a different mounted drive
with `SIN_MODPACK_PUBLICATION_ROOT` or `--publication-root` whenever the
Google Drive desktop mount changes.  The tested/source directory remains
independent of the publication root.

The operator's current paths may be supplied explicitly when performing a
live-approved operation, but they must not be used by automated development
tests. Tests use temporary directories.

## Explicit approval workflow

Adding a ZIP to the source directory never publishes it. The approved list is
explicit and may contain both SiN and third-party ZIPs. Do not omit `--mod`:
omitting it fails closed rather than selecting every ZIP.

```powershell
python -m fs25_network_core.modpack capture `
  --source-dir $env:SIN_MODPACK_SOURCE_DIR `
  --publication-root $env:SIN_MODPACK_PUBLICATION_ROOT `
  --server-key sin-fs25-01 `
  --server-name 'SiN Test Server 01' `
  --version 2026.09.22 `
  --mod FS25_SiN_Server.zip `
  --mod ThirdPartyMod.zip
```

`capture` validates every selected ZIP and writes only the versioned
`Releases/<version>` directory. It does not change `Current`. Review/test that
release, then explicitly publish it:

```powershell
python -m fs25_network_core.modpack publish `
  --source-dir $env:SIN_MODPACK_SOURCE_DIR `
  --publication-root $env:SIN_MODPACK_PUBLICATION_ROOT `
  --server-key sin-fs25-01 `
  --server-name 'SiN Test Server 01' `
  --version 2026.09.22
```

Publication validates the complete release, stages a complete replacement,
validates the stage again, and then swaps `Current`. A corrupt source, release,
manifest, hash, ZIP, or destination fails without publishing a partial pack.
Re-publishing the same manifest is a no-op.

## One-command client update and approved-pack refresh

When the existing approved mod list is unchanged but one ZIP has been rebuilt,
the client updater can install the release ZIP, recapture the exact filenames
from the current approved manifest, validate them, and publish the new version
to `Current`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File 'C:\repos\FS25_SiN_Core\scripts\Update-SiN-Client.ps1' `
  -Version v0.1.32 `
  -ModsPath 'C:\Users\Dan\OneDrive\Documents\My Games\SiN' `
  -PublishModpack -RefreshApprovedModpack `
  -ModpackRepositoryRoot 'C:\repos\FS25_SiN_Core' `
  -ModpackPublicationRoot 'H:\My Drive\SiN Mods' `
  -ModpackServerKey 'sin-fs25-01' `
  -ModpackServerName 'SiN Test Server 01' `
  -ModpackVersion v0.1.32
```

If the chosen source/client folder itself is the operator-approved complete
modset, use the explicit source-approval mode instead. This is the one-command
path for a first publication or for intentionally approving every ZIP currently
in that folder:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File 'C:\repos\FS25_SiN_Core\scripts\Update-SiN-Client.ps1' `
  -Version v0.1.32 `
  -ModsPath 'C:\Users\Dan\OneDrive\Documents\My Games\SiN' `
  -PublishModpack -ApproveAllSourceMods `
  -ModpackRepositoryRoot 'C:\repos\FS25_SiN_Core' `
  -ModpackPublicationRoot 'H:\My Drive\SiN Mods' `
  -ModpackServerKey 'sin-fs25-01' `
  -ModpackServerName 'SiN Test Server 01' `
  -ModpackVersion v0.1.32
```

This enumerates only `*.zip` files in the explicitly supplied folder at the
moment the command runs. It is not a watcher and does not publish when files
appear; the switch is the approval action.

`-RefreshApprovedModpack` is explicit and fail-closed: it reads the current
`manifest.json` as the approval allowlist, so new ZIPs in the source directory
are not published and removed approved ZIPs cause the operation to stop. The
source/client directory is updated before capture; the publication root is
first modified when the validated `Releases/<version>` capture is written.
`Current` is replaced only after the complete release validates. The command
does not synchronize unrelated local ZIPs or deploy to the dedicated server.

For a deliberately constrained approved set, omit both refresh/source-approval
modes and provide every approved filename explicitly with repeated `-ApprovedMod`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File 'C:\repos\FS25_SiN_Core\scripts\Update-SiN-Client.ps1' `
  -Version v0.1.33 -ModsPath 'C:\Users\Dan\OneDrive\Documents\My Games\SiN' `
  -PublishModpack -ModpackRepositoryRoot 'C:\repos\FS25_SiN_Core' `
  -ModpackPublicationRoot 'H:\My Drive\SiN Mods' `
  -ModpackServerKey 'sin-fs25-01' -ModpackServerName 'SiN Test Server 01' `
  -ModpackVersion v0.1.33 `
  -ApprovedMod 'FS25_SiN_Server.zip' -ApprovedMod 'ThirdPartyMod.zip'
```

## Client synchronization

Validate the current pack and synchronize managed ZIPs with:

```powershell
python -m fs25_network_core.modpack validate `
  --source-dir $env:SIN_MODPACK_SOURCE_DIR `
  --publication-root $env:SIN_MODPACK_PUBLICATION_ROOT `
  --server-key sin-fs25-01 `
  --server-name 'SiN Test Server 01'

python -m fs25_network_core.modpack sync `
  --source-dir $env:SIN_MODPACK_SOURCE_DIR `
  --publication-root $env:SIN_MODPACK_PUBLICATION_ROOT `
  --client-dir $env:SIN_MODPACK_CLIENT_MOD_DIR `
  --server-key sin-fs25-01 `
  --server-name 'SiN Test Server 01'
```

Default synchronization copies and hash-verifies only the manifest-listed
managed ZIPs. It never deletes unrelated local ZIPs. `--strict` is an explicit
opt-in that removes local `.zip` files not listed in the current manifest.

## Safety boundaries

- Source files are read only; capture does not modify the source/client folder.
- The publication root must be separate from (and outside) the source folder;
  the publisher rejects a nested destination to prevent accidental source
  mutation.
- No automatic watcher or publication-on-file-appearance exists.
- The client updater's modpack mode is still explicit; it never watches or
  publishes merely because a source ZIP appeared.
- Publication root access and all hashes are validated before `Current` changes.
- Server identity comes from the existing server registry, not a new config
  roster. The CLI's `--server-name` is an explicit offline/operator input and
  must match the registry record used by Central.
- The implementation has no Google Drive SDK/API dependency.
