# GitHub release deployment

GitHub Actions validates every push and pull request, but never connects to or
modifies the FS25 VM. A `vMAJOR.MINOR.PATCH` tag creates a release containing:

```text
sin-agent.zip
FS25_SiN_Server.zip
build-manifest.json
SHA256SUMS.txt
Update-SiN.ps1
Update-SiN-Client.ps1
Restart-SiN-Agent.ps1
```

The Agent archive contains only `fs25_network_core/__init__.py` and
`fs25_network_core/agent.py`; it has no Mongo dependency or credentials. The
FS25_SiN_Server archive is rebuilt from `mods/FS25_SiN_Server` and verified
to contain `modDesc.xml` and `NetworkLocal.lua` at its archive root.
The release builder also validates every packaged Lua source against the FS25
Lua runtime dialect (including rejection of Lua 5.2+ `goto`/label syntax) both
before and after archive creation. A GIANTS runtime load test remains required,
but a source that cannot be parsed by the FS25 Lua dialect gate cannot enter a
release artifact.

The standard Central API port is **8787**. Central listens on the configured
`SIN_API_PORT` (default `8787`), and every Agent `SIN_BACKEND_URL`/updater
`-ApiUrl` must use the same port unless an explicit reverse proxy is in use.

## Creating a release

From a clean committed checkout:

```powershell
git push origin main
git tag <next-unused-version>
git push origin <next-unused-version>
```

The tag workflow reruns tests, compile validation, patch validation, and
release packaging, then publishes the deployment assets. Ordinary pushes and pull
requests only run CI/package proof; they do not publish a release.

## One-time VM bootstrap

If the VM has an older updater whose default points at the legacy mailbox root,
bootstrap the corrected updater from the next published release directly over HTTPS:

```powershell
New-Item -ItemType Directory -Force C:\SiN\Deploy | Out-Null
Invoke-WebRequest `
  -Uri "https://github.com/DanHouston/FS25_SiN_Core/releases/download/<next-version>/Update-SiN.ps1" `
  -OutFile "C:\SiN\Deploy\Update-SiN.ps1"
```

This does not copy or inspect `serverBinding.xml`. Set the FS25 mods directory
either as `SIN_FS25_MODS_DIR` or pass `-ModsPath`; the updater intentionally does
not guess an installation path.

## Normal VM deployment

Stop the FS25 dedicated server first. The updater stops only the SiN Agent;
it never stops or restarts FS25, so mailbox migration and ZIP replacement must
not run while the mod is writing state. On `SiN-FS25-01`, with the required
FS25 mods directory configured:

```powershell
$env:SIN_FS25_MODS_DIR = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\mods"
powershell.exe -ExecutionPolicy Bypass -File "C:\SiN\Deploy\Update-SiN.ps1"
```

Specific release:

```powershell
$env:SIN_FS25_MODS_DIR = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\mods"
powershell.exe -ExecutionPolicy Bypass -File "C:\SiN\Deploy\Update-SiN.ps1" -Version <next-version>
```

The updater resolves public GitHub Releases over HTTPS, downloads all assets to
`C:\SiN\Downloads\<version>`, verifies checksum and manifest hashes, then
backs up the live Agent, mod ZIP, and deployment metadata under
`C:\SiN\Backups\<timestamp>-<version>`. It stops only Python processes whose
command line contains both `fs25_network_core.agent` and `--watch`, installs the
Agent under `C:\SiN\Agent\fs25_network_core`, starts one watcher with the
configured `SIN_BACKEND_URL`, `SIN_MAILBOX_DIR`, and `SIN_POLL_INTERVAL`, and
writes `C:\SiN\deployment.json`.

Agent updates take effect after the updater restarts the Agent. A changed
FS25_SiN_Server ZIP prints `FS25 RESTART REQUIRED`; the updater never restarts the
FS25 dedicated server. Identical mod hashes do not require a restart.

For an Agent-only interruption or recovery, use the packaged explicit restart
script. It reads the backend URL, canonical mailbox, Agent root, and poll
interval from `C:\SiN\deployment.json`, so operators do not need to reconstruct
launcher environment variables:

```powershell
powershell.exe -ExecutionPolicy Bypass -File C:\SiN\Deploy\Restart-SiN-Agent.ps1
```

The release builder treats `dist/` as ephemeral generated state and cleans it
before a canonical local build. Deployment downloads authoritative GitHub
Release assets and does not consume local `dist/` contents.

The newest updater is downloaded as `C:\SiN\Deploy\Update-SiN.next.ps1` so the
currently running script is not replaced mid-execution.

### Canonical FS25_SiN_Server mailbox migration

The FS25_SiN_Server GIANTS-authorized mailbox root is:

```text
C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\modSettings\FS25_SiN_Server
```

Historical installations may contain both `FS25SiNNetworkLocal` and
`FS25_SiN_NetworkLocal`. The updater examines both roots while FS25 is stopped.
It only consolidates them after verifying that every non-empty legacy root has
the same `serverBinding.xml` hash. Different or missing bindings fail closed;
binding contents are never logged or manually merged.

The consolidation builds a staged union under
`FS25_SiN_Server.migrating`. Identical files are deduplicated, files present in
only one root are retained, and conflicting durable commands, events,
receipts, registration files, manager authority, or unknown files fail safely.
For the explicitly regenerable `snapshot.xml` and `clock-policy.xml`, the
newest valid XML copy wins. After validation, the stage is moved into
`FS25_SiN_Server` and the old roots are moved into the excluded
`FS25_SiN_Server.migration-archive` directory. A failed or interrupted run
leaves the sources/stage intact so a later run can resume without re-pairing.
Completed canonical migrations are not rediscovered from the archive.

If the VM still has an older updater, bootstrap the next updater over HTTPS
before running the normal deployment:

```powershell
New-Item -ItemType Directory -Force C:\SiN\Deploy | Out-Null
Invoke-WebRequest `
  -Uri "https://github.com/DanHouston/FS25_SiN_Core/releases/download/<next-version>/Update-SiN.ps1" `
  -OutFile "C:\SiN\Deploy\Update-SiN.next.ps1"

$env:SIN_FS25_MODS_DIR = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\mods"
powershell.exe -ExecutionPolicy Bypass `
  -File "C:\SiN\Deploy\Update-SiN.next.ps1" `
  -Version <next-version>
```

The running Agent is stopped before migration and restarted with the
canonical mailbox path. The updater does not restart FS25; a changed mod ZIP
still requires an FS25 server/client restart.

Rollback uses the most recent backup:

```powershell
$env:SIN_FS25_MODS_DIR = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\mods"
powershell.exe -ExecutionPolicy Bypass -File "C:\SiN\Deploy\Update-SiN.ps1" -Rollback
```

Rollback restores the Agent and mod ZIP, restarts only the Agent watcher, and
does not touch `serverBinding.xml`. GitHub credentials are optional for this
public repository; if `GITHUB_TOKEN` is supplied for rate-limit relief, it is
used only in the HTTPS request and never printed or packaged.

### Client mod update

The same generic FS25_SiN_Server ZIP must be installed on the dedicated server
and connecting clients. Remove the old `FS25_SiN_NetworkLocal.zip` from the
client mod folder before loading the new ZIP; both must not be active. The
release also includes a client-only updater; it
does not read credentials or restart FS25:

```powershell
$env:SIN_FS25_CLIENT_MODS_DIR = "C:\Users\Dan\OneDrive\Documents\My Games\FOC_mods_mine"
powershell.exe -ExecutionPolicy Bypass `
  -File "C:\SiN\Deploy\Update-SiN-Client.ps1" -Version <next-version>
```

Pass `-ModsPath` instead of setting the environment variable when the client
uses another mod directory. The updater downloads the public release over
HTTPS, validates the ZIP against `SHA256SUMS.txt`, checks root contents, and
replaces only the FS25_SiN_Server ZIP and removes the legacy ZIP so both cannot
load. FS25 must be reloaded afterward.

When the approved mod list is unchanged, the same updater can explicitly
refresh and publish the complete modpack after installing the new SiN ZIP:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "C:\repos\FS25_SiN_Core\scripts\Update-SiN-Client.ps1" `
  -Version v0.1.32 `
  -ModsPath "C:\Users\Dan\OneDrive\Documents\My Games\SiN" `
  -PublishModpack -RefreshApprovedModpack `
  -ModpackRepositoryRoot "C:\repos\FS25_SiN_Core" `
  -ModpackPublicationRoot "G:\My Drive\SiN Mods" `
  -ModpackServerKey sin-fs25-01 `
  -ModpackServerName "SiN Test Server 01" `
  -ModpackVersion v0.1.32
```

This mode reuses only the filenames in the current approved manifest. It fails
closed if an approved source ZIP is missing and never adds an unapproved ZIP.
If the explicitly supplied client/source directory is itself the complete
approved modset, use `-PublishModpack -ApproveAllSourceMods` instead; that
enumerates its ZIPs only for that invocation. For a deliberate constrained
modset, use repeated `-ApprovedMod` parameters and omit both approval modes.
The command publishes to the filesystem
publication root only after the refreshed release validates; it does not deploy
the pack to the dedicated server.
