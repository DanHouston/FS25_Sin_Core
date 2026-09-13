# GitHub release deployment

GitHub Actions validates every push and pull request, but never connects to or
modifies the FS25 VM. A `vMAJOR.MINOR.PATCH` tag creates a release containing:

```text
sin-agent.zip
FS25_SiN_NetworkLocal.zip
build-manifest.json
SHA256SUMS.txt
Update-SiN.ps1
```

The Agent archive contains only `fs25_network_core/__init__.py` and
`fs25_network_core/agent.py`; it has no Mongo dependency or credentials. The
NetworkLocal archive is rebuilt from `mods/FS25_SiN_NetworkLocal` and verified
to contain `modDesc.xml` and `NetworkLocal.lua` at its archive root.

## Creating a release

From a clean committed checkout:

```powershell
git push origin main
git tag v0.1.0
git push origin v0.1.0
```

The tag workflow reruns tests, compile validation, patch validation, and
release packaging, then publishes the five assets. Ordinary pushes and pull
requests only run CI/package proof; they do not publish a release.

## One-time VM bootstrap

Using the existing emergency/manual transfer if necessary, create
`C:\SiN\Deploy` and copy the `Update-SiN.ps1` asset there as
`C:\SiN\Deploy\Update-SiN.ps1`. Set the FS25 mods directory either as
`SIN_FS25_MODS_DIR` or pass `-ModsPath`; the updater intentionally does not
guess an installation path. This bootstrap does not copy `serverBinding.xml`.

## Normal VM deployment

On `SiN-FS25-01`, with the required FS25 mods directory configured:

```powershell
$env:SIN_FS25_MODS_DIR = "C:\path\to\FarmingSimulator2025\mods"
C:\SiN\Deploy\Update-SiN.ps1
```

Specific release:

```powershell
C:\SiN\Deploy\Update-SiN.ps1 -Version v0.1.0 -ModsPath $env:SIN_FS25_MODS_DIR
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

Rollback uses the most recent backup:

```powershell
C:\SiN\Deploy\Update-SiN.ps1 -Rollback -ModsPath $env:SIN_FS25_MODS_DIR
```

Rollback restores the Agent and mod ZIP, restarts only the Agent watcher, and
does not touch `serverBinding.xml`. GitHub credentials are optional for this
public repository; if `GITHUB_TOKEN` is supplied for rate-limit relief, it is
used only in the HTTPS request and never printed or packaged.
