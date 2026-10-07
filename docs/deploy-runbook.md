# SiN deployment runbook

This is the copy/paste operator path for a released SiN build. It does not
deploy automatically, stop FS25, or modify live systems until an operator runs
the commands below.

Set the release version to an existing, published GitHub Release. A tag without
a GitHub Release and its assets is not deployable by the updater.

## Operator variables

Run these in the appropriate PowerShell session and change only the paths that
are different on that machine:

```powershell
$Version = "v0.1.41"
$Repo = "C:\repos\FS25_SiN_Core"
$CentralUrl = "http://192.168.1.185:8787"
$ServerKey = "sin-fs25-01"
$ServerName = "SiN Test Server 01"
$ServerMods = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\mods"
$Mailbox = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\modSettings\FS25_SiN_Server"
$ClientMods = "C:\Users\Dan\OneDrive\Documents\My Games\SiN"
$PublicationRoot = "G:\My Drive\SiN Mods\sin-fs25-01"
```

The standard Central API port is `8787`. The Agent URL, updater `-ApiUrl`, and
Central `SIN_API_PORT` must agree.

## 1. Validate a clean release candidate

Run from the exact checkout that will be tagged:

```powershell
Set-Location $Repo
git status --short
git rev-parse HEAD

python scripts/validate_repository.py
python -m compileall -q fs25_network_core tests scripts
git diff --check

$ReleaseDir = Join-Path $env:TEMP ("sin-release-" + $Version)
if (Test-Path -LiteralPath $ReleaseDir) {
    Remove-Item -LiteralPath $ReleaseDir -Recurse -Force
}
python scripts/build_release.py --version $Version --output $ReleaseDir
python scripts/validate_release.py $ReleaseDir --version $Version --require-clean
```

The packaged validation checks the actual `FS25_SiN_Server.zip`, every shipped
Lua file, archive structure, checksums, and build manifest.

## 2. Push and tag the validated commit

Inspect the remote tags first and select the next unused `vMAJOR.MINOR.PATCH`:

```powershell
git ls-remote --tags --refs origin
git push origin main
git tag -a $Version -m "Release $Version"
git push origin $Version
```

Wait for the tag workflow to publish the GitHub Release before running either
updater. Verify the release contains `sin-agent.zip`,
`FS25_SiN_Server.zip`, `build-manifest.json`, `SHA256SUMS.txt`,
`Update-SiN.ps1`, `Update-SiN-Client.ps1`, `Restart-SiN-Agent.ps1`, and
`Publish-SiN-Modpack.ps1`.

## 3. Deploy Central and JiN

On the Central host, run the API and JiN as separate processes. The normal
environment must already provide `MONGODB_URI` and credentials:

```powershell
$env:MONGODB_DATABASE = "fs25_network"
$env:SIN_API_HOST = "0.0.0.0"
$env:SIN_API_PORT = "8787"
python -m fs25_network_core.server_api
```

In another Central-host window, run the supervised JiN process (not a second
unsupervised `bot_frontend` instance):

```powershell
$env:MONGODB_DATABASE = "fs25_network"
python -m fs25_network_core.bot_supervisor
```

JiN's supervisor writes `health.json`, `bot.log`, and `supervisor.log` under
`$env:LOCALAPPDATA\SiN\JiN` by default. Set `FS25_JIN_RUNTIME_DIR` before
launch if those files belong elsewhere. The health marker requires the Discord
gateway to be ready and the activity, bank, and status publishers to complete
their scan loops. The supervisor checks it every 15 seconds, allows three
minutes for startup or a transient outage, then restarts only its own child
process if progress stops. It caps recovery at five restarts in 30 minutes;
investigate `bot.log` and `supervisor.log` if that limit is reached. A process
lock prevents two new JiN instances using the same runtime directory.

For unattended operation, configure **one** Windows Scheduled Task to run
`python -m fs25_network_core.bot_supervisor` from the Central checkout at
machine startup under the Central service account. Use the full path to that
account's Python interpreter, set the task's working directory to the checkout,
select "Do not start a new instance" and restart the task on failure. The
task must have access to the same private `.env`/Mongo credentials as JiN.
Stop the old manually launched bot before enabling the task; an older build
does not hold the new single-instance lock. Scheduled Task failure recovery
protects the supervisor itself; JiN's health marker handles a bot that is
still running but no longer publishing.

Read-only health check on the Central host:

```powershell
Get-Content -LiteralPath "$env:LOCALAPPDATA\SiN\JiN\health.json" -Raw | ConvertFrom-Json |
    Select-Object pid, healthy, updated_at, last_healthy_at, reasons
Get-Content -LiteralPath "$env:LOCALAPPDATA\SiN\JiN\supervisor.log" -Tail 30
```

Do not treat a process listing or Discord's connection state alone as proof
that publications are flowing. A restart replays the durable activity outbox;
it cannot fix permanent Discord permission errors or a failed outbox record.

If the FS25 money bridge is intentionally enabled for this server, configure it
once from the Central repository environment:

```powershell
python -m fs25_network_core.money_bridge_config --server sin-fs25-01 --enable
```

Do not disable it as part of ordinary mod or Agent deployment.

## 4. Deploy the dedicated-server Agent and mod

Stop the FS25 dedicated server through its normal server control before replacing
the ZIP or migrating mailbox files. The updater stops/restarts only the SiN
Agent; it never stops FS25.

If the VM has no current updater, bootstrap it from the published release:

```powershell
New-Item -ItemType Directory -Force C:\SiN\Deploy | Out-Null
Invoke-WebRequest `
  -Uri "https://github.com/DanHouston/FS25_Sin_Core/releases/download/$Version/Update-SiN.ps1" `
  -OutFile "C:\SiN\Deploy\Update-SiN.ps1"
```

Run the normal server deployment on `sin-fs25-01`:

```powershell
$env:SIN_FS25_MODS_DIR = $ServerMods
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "C:\SiN\Deploy\Update-SiN.ps1" `
  -Version $Version `
  -ApiUrl $CentralUrl `
  -MailboxDir $Mailbox `
  -ModsPath $ServerMods
```

The updater backs up the prior Agent/mod/metadata, validates release hashes,
uses the canonical mailbox, restarts the Agent, and writes
`C:\SiN\deployment.json`. Start/reload the FS25 dedicated server manually when
the updater reports `FS25 RESTART REQUIRED`.

For an Agent-only restart, without changing the FS25 ZIP:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "C:\SiN\Deploy\Restart-SiN-Agent.ps1"
```

If that script is missing, run the full updater first; it is included in every
release and copied into `C:\SiN\Deploy`.

Verify the installed server artifact:

```powershell
$Deployment = Get-Content "C:\SiN\deployment.json" -Raw | ConvertFrom-Json
$ServerZip = [string]$Deployment.server_path
$ActualServerHash = (Get-FileHash -LiteralPath $ServerZip -Algorithm SHA256).Hash.ToLowerInvariant()

$Deployment | Select-Object version,git_commit,server_path,server_sha256
"Actual SHA256:   $ActualServerHash"
"Hash matches:    $($ActualServerHash -eq $Deployment.server_sha256.ToLowerInvariant())"
```

The canonical FS25 mailbox must be:

```text
C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\modSettings\FS25_SiN_Server
```

Do not print or copy `serverBinding.xml` credentials.

## 5. Update the validation client

After the GitHub Release exists, run this on the client. Using the repository
script avoids depending on a possibly missing `C:\SiN\Deploy` client folder:

```powershell
Set-Location $Repo
& powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File (Join-Path $Repo "scripts\Update-SiN-Client.ps1") `
  -Version $Version `
  -ModsPath $ClientMods
```

The updater downloads and checksum-validates the same public ZIP used by the
server, removes the legacy `FS25_SiN_NetworkLocal.zip` if present, and reports
that FS25 reload is required. Verify the client hash:

```powershell
$ClientZip = Join-Path $ClientMods "FS25_SiN_Server.zip"
Get-FileHash -LiteralPath $ClientZip -Algorithm SHA256
```

The client and server SHA256 values must be identical before multiplayer load.

## 6. Publish the server modpack

The modpack command adds new ZIPs and replaces only same-named ZIPs whose
contents changed. It retains destination-only ZIPs, then rebuilds the manifest
and pack from the merged destination mods folder. Remove obsolete mods from the
destination `mods` folder manually when retiring them. There is no separate
approval list or versioned publication tree. Run after placing new/updated ZIPs
in the server's mod folder:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "C:\SiN\Deploy\Publish-SiN-Modpack.ps1" `
  -SourceMods $ServerMods `
  -Destination $PublicationRoot `
  -ServerKey $ServerKey `
  -ServerName $ServerName
```

On success, inspect `mod_count`, `added_mod_count`, `updated_mod_count`,
`manifest_sha256`, and `modpack_sha256`. The destination contains `mods\`, `manifest.json`, and one
`SiN Test Server 01-Modpack.zip`. Google Drive is used as a mounted filesystem;
no Google API is involved.

## 7. Post-deployment checks

On the server, confirm the runtime mailbox and mod hash:

```powershell
Test-Path -LiteralPath $Mailbox
Get-ChildItem -LiteralPath $Mailbox -Force | Select-Object Name,Length,LastWriteTime
Get-FileHash -LiteralPath (Join-Path $ServerMods "FS25_SiN_Server.zip") -Algorithm SHA256
```

In JiN, perform the minimum current-world checks:

```text
/server-status
/farm_status
/balance
```

`/balance` should show `Game balance: $...` after the refreshed server mod has
produced at least one current snapshot. If it still says unavailable, inspect
the authoritative snapshot and confirm that the farm record includes a
`balance` value and that the user has an active, mod-confirmed farm-manager
mapping. Do not infer a game balance from the SiN wallet or pending requests.

For an intentional money smoke test only, use a small amount and verify the
authoritative game-side receipt before repeating:

```text
/deposit amount:1
/balance
/withdraw amount:1
/balance
```

Verify both chat directions in the configured Activity channel and FS25 chat,
then verify that the same message is not echoed or duplicated. Confirm the
server and client ZIP hashes again if FS25 reports a mod mismatch.

For read-only Central state inspection, use the repository's configured Python
Mongo connection; do not use `mongosh` or edit MongoDB manually:

```powershell
python -c "from fs25_network_core.database import Database; d=Database().db; print('SERVERS'); print(d.sin_servers.find_one({'server_key':'sin-fs25-01'})); print('ACTIVE WORLD'); print(d.world_generations.find_one({'server_key':'sin-fs25-01','save_key':'sin-fs25-hobo-v1','state':'active'})); print('LATEST SNAPSHOT'); s=d.server_snapshots.find_one({'server_key':'sin-fs25-01','save_key':'sin-fs25-hobo-v1'}, sort=[('received_at',-1)]); print({'received_at':s.get('received_at'),'world_id':s.get('world_id'),'farms':s.get('farms'),'farm_balances':s.get('farm_balances') if s else None}); print('ACTIVE MANAGER'); print(d.memberships.find_one({'discord_id':'<DISCORD_ID>','server_id':'sin-fs25-01','save_id':'sin-fs25-hobo-v1','state':'active','desired_role':'farm_manager','applied_role':'farm_manager'}))"
```

## 8. Rollback

Stop FS25 before restoring the prior server Agent/mod release:

```powershell
$env:SIN_FS25_MODS_DIR = $ServerMods
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "C:\SiN\Deploy\Update-SiN.ps1" `
  -Rollback `
  -ApiUrl $CentralUrl `
  -MailboxDir $Mailbox `
  -ModsPath $ServerMods
```

Rollback restores the latest backup and restarts only the Agent. It does not
rewrite server bindings, MongoDB state, world history, or FS25 save data.
## Crop settings mod

Releases also contain `SiN_FS25_Crop_Settings.zip`. It is an independent
server/client mod and must be included in the approved modpack with identical
bytes on every participant. The current probe policy changes only Sorghum
(plant April-May, harvest August-November); changing `config/fruit-policy.xml`
requires a new release and a map/save reload. Verify the ZIP with the release
`SHA256SUMS.txt` before copying it into the modpack source directory.
