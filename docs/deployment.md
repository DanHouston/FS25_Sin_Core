# GitHub release deployment

GitHub Actions validates every push and pull request, but never connects to or
modifies the FS25 VM. A `vMAJOR.MINOR.PATCH` tag creates a release containing:

```text
sin-agent.zip
FS25_SiN_Server.zip
SiN_FS25_Crop_Settings.zip
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
`SiN_FS25_Crop_Settings.zip` is a separate, map-independent multiplayer mod;
install the exact release bytes on the server and all clients (normally via
the approved modpack). The current policy probe changes only Sorghum (plant
April-May, harvest August-November); policy changes require a map/save reload.
The release builder also validates every packaged Lua source against the FS25
Lua runtime dialect (including rejection of Lua 5.2+ `goto`/label syntax) both
before and after archive creation. A GIANTS runtime load test remains required,
but a source that cannot be parsed by the FS25 Lua dialect gate cannot enter a
release artifact.

Central listens internally on `SIN_API_PORT` (default **8787**). The production
Agent and updater use `https://sin-central.duckdns.org`, which terminates HTTPS
on the Google Cloud endpoint and proxies requests to Central. The Windows
`SiN FS25 Agent` scheduled task on the FS25 host is the sole Agent launcher;
deployment updates its existing action while preserving its run-as identity and
triggers.

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

## Bootstrap a newer updater safely

If the installed updater predates the scheduled-task launcher, do not run it to
fetch the update: it would restart the Agent using its old launch method. After
the corrected release is published, download its updater directly to the
staging filename and execute that staged file:

```powershell
New-Item -ItemType Directory -Force C:\SiN\Deploy | Out-Null
Invoke-WebRequest `
  -Uri "https://github.com/DanHouston/FS25_SiN_Core/releases/download/<next-version>/Update-SiN.ps1" `
  -OutFile "C:\SiN\Deploy\Update-SiN.next.ps1"

$env:SIN_FS25_MODS_DIR = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\mods"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "C:\SiN\Deploy\Update-SiN.next.ps1" `
  -Version <next-version>
```

This does not copy or inspect `serverBinding.xml`. The Agent updater does not
need an FS25 mods directory; server-mod updates are performed separately.

## Normal Agent deployment

This updater changes only the SiN Agent, its scheduled-task action, and its
deployment metadata. It does not download, inspect, back up, replace, or delete
anything in the FS25 mods directory, and does not require an FS25 restart.
Mailbox migration is a separate concern: if legacy mailbox roots still need
consolidation, stop FS25 for that migration; an already-complete canonical
mailbox can be left live. On `SiN-FS25-01`:

```powershell
powershell.exe -ExecutionPolicy Bypass -File "C:\SiN\Deploy\Update-SiN.ps1"
```

Specific release:

```powershell
powershell.exe -ExecutionPolicy Bypass -File "C:\SiN\Deploy\Update-SiN.ps1" -Version <next-version>
```

The updater resolves public GitHub Releases over HTTPS, downloads the Agent
and updater assets to `C:\SiN\Downloads\<version>`, verifies their checksums,
and backs up the live Agent and deployment metadata under
`C:\SiN\Backups\<timestamp>-<version>`. It stops the existing `SiN FS25 Agent`
task, installs the Agent under `C:\SiN\Agent\fs25_network_core`, then updates
and starts that task using its configured Python executable. It preserves the
task's principal and triggers and never starts a standalone Python process.
Existing server-mod path/hash metadata is preserved without checking the file.

Agent updates take effect after the updater restarts the Agent. Server-mod ZIP
updates remain a separate manual operation and should use the established mod
update/modpack workflow when you choose to deploy them.

For an Agent-only interruption or recovery, use the packaged explicit restart
script. It reads the backend URL, canonical mailbox, Agent root, and poll
interval from `C:\SiN\deployment.json`, so operators do not need to reconstruct
launcher environment variables:

```powershell
powershell.exe -ExecutionPolicy Bypass -File C:\SiN\Deploy\Restart-SiN-Agent.ps1
```

The release builder treats `dist/` as ephemeral generated state and cleans it
before every canonical local build. The latest ZIPs and their validation
metadata are written directly under `dist/`; nested `dist/<version>` output is
rejected so repeated validation builds do not accumulate directories. Deployment
downloads authoritative GitHub Release assets and does not consume local
`dist/` contents.

During a normal deployment the newest updater is staged as
`C:\SiN\Deploy\Update-SiN.next.ps1` so the currently running script is not
replaced mid-execution. For the first deployment of the scheduled-task-aware
updater, execute that staged file directly as shown above; do not first run an
older updater to stage it.

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

If the VM still has an older updater, bootstrap and execute the next updater
over HTTPS (do not invoke the old updater first):

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

Agent rollback uses the most recent Agent backup and does not touch FS25 mods:

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

To publish the complete modpack, run the standalone server-side publisher after
the server mod folder contains the exact ZIP set:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "C:\SiN\Deploy\Publish-SiN-Modpack.ps1" `
  -SourceMods "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\mods" `
  -Destination "G:\My Drive\SiN Mods\sin-fs25-01" `
  -ServerKey sin-fs25-01 `
  -ServerName "SiN Test Server 01"
```

The command writes `mods\`, `manifest.json`, and one combined
`SiN Test Server 01-Modpack.zip` directly under the destination. It copies every
source ZIP, validates the staged output, and leaves unrelated destination files
alone. It does not deploy the pack to the dedicated server.
