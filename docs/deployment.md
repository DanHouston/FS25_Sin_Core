# GitHub release deployment

GitHub Actions validates every push and pull request, but never connects to or
modifies the FS25 VM. A `vMAJOR.MINOR.PATCH` tag creates a release containing:

```text
sin-agent.zip
FS25_SiN_NetworkLocal.zip
build-manifest.json
SHA256SUMS.txt
Update-SiN.ps1
Update-SiN-Client.ps1
```

The Agent archive contains only `fs25_network_core/__init__.py` and
`fs25_network_core/agent.py`; it has no Mongo dependency or credentials. The
NetworkLocal archive is rebuilt from `mods/FS25_SiN_NetworkLocal` and verified
to contain `modDesc.xml` and `NetworkLocal.lua` at its archive root.

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

On `SiN-FS25-01`, with the required FS25 mods directory configured:

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
NetworkLocal ZIP prints `FS25 RESTART REQUIRED`; the updater never restarts the
FS25 dedicated server. Identical mod hashes do not require a restart.

The newest updater is downloaded as `C:\SiN\Deploy\Update-SiN.next.ps1` so the
currently running script is not replaced mid-execution.

### Canonical NetworkLocal mailbox migration

NetworkLocal's GIANTS-authorized mailbox root is:

```text
C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\modSettings\FS25_SiN_NetworkLocal
```

The previous `FS25SiNNetworkLocal` directory is not a valid active runtime
root on the dedicated server. Release Candidate A's updater accepts the
opt-in `-MigrateLegacyMailbox` switch. It copies only files that are absent
from the canonical directory, never reads or prints `serverBinding.xml`, and
retains the old directory as a recovery backup. Existing canonical files are
never overwritten, so a conflict must be reviewed manually. Requests and
responses, events, receipts, and permission commands are not copied into the
active directory; they remain in the retained legacy backup for manual review
so stale work cannot be replayed automatically. Review or remove those files
only after the Agent is stopped and their status is understood.

If the VM still has an older updater whose default points at the legacy root,
bootstrap the next updater over HTTPS, then run it with migration enabled:

```powershell
New-Item -ItemType Directory -Force C:\SiN\Deploy | Out-Null
Invoke-WebRequest `
  -Uri "https://github.com/DanHouston/FS25_SiN_Core/releases/download/<next-version>/Update-SiN.ps1" `
  -OutFile "C:\SiN\Deploy\Update-SiN.next.ps1"

$env:SIN_FS25_MODS_DIR = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\mods"
powershell.exe -ExecutionPolicy Bypass `
  -File "C:\SiN\Deploy\Update-SiN.next.ps1" `
  -Version <next-version> `
  -MigrateLegacyMailbox
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

The same generic NetworkLocal ZIP must be installed on the dedicated server
and connecting clients. The release also includes a client-only updater; it
does not read credentials or restart FS25:

```powershell
$env:SIN_FS25_CLIENT_MODS_DIR = "C:\Users\Dan\OneDrive\Documents\My Games\FOC_mods_mine"
powershell.exe -ExecutionPolicy Bypass `
  -File "C:\SiN\Deploy\Update-SiN-Client.ps1" -Version <next-version>
```

Pass `-ModsPath` instead of setting the environment variable when the client
uses another mod directory. The updater downloads the public release over
HTTPS, validates the ZIP against `SHA256SUMS.txt`, checks root contents, and
replaces only the NetworkLocal ZIP. FS25 must be reloaded afterward.
