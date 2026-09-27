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
$PublicationRoot = "H:\My Drive\SiN Mods"
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
`Update-SiN.ps1`, `Update-SiN-Client.ps1`, and `Restart-SiN-Agent.ps1`.

## 3. Deploy Central and JiN

On the Central host, run the API and JiN as separate processes. The normal
environment must already provide `MONGODB_URI` and credentials:

```powershell
$env:MONGODB_DATABASE = "fs25_network"
$env:SIN_API_HOST = "0.0.0.0"
$env:SIN_API_PORT = "8787"
python -m fs25_network_core.server_api
```

In another Central-host window:

```powershell
$env:MONGODB_DATABASE = "fs25_network"
python -m fs25_network_core.bot_frontend
```

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

## 6. Refresh and publish the approved modpack

When the approved ZIP list is unchanged, install the new SiN ZIP first, then
refresh/publish from the existing approval manifest:

```powershell
Set-Location $Repo
& powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File (Join-Path $Repo "scripts\Update-SiN-Client.ps1") `
  -Version $Version `
  -ModsPath $ClientMods `
  -PublishModpack -RefreshApprovedModpack `
  -ModpackRepositoryRoot $Repo `
  -ModpackPublicationRoot $PublicationRoot `
  -ModpackServerKey $ServerKey `
  -ModpackServerName $ServerName `
  -ModpackVersion $Version
```

This reads the existing approved filenames, fails closed if one is missing, and
does not approve newly appearing ZIPs. Use `-ApproveAllSourceMods` only when the
source directory itself is deliberately the complete approved set; never combine
it with `-RefreshApprovedModpack`.

Validate the published pack and optionally synchronize another client:

```powershell
$env:PYTHONPATH = $Repo
python -m fs25_network_core.modpack validate `
  --source-dir $ClientMods `
  --publication-root $PublicationRoot `
  --server-key $ServerKey `
  --server-name $ServerName

python -m fs25_network_core.modpack sync `
  --source-dir $ClientMods `
  --publication-root $PublicationRoot `
  --client-dir "C:\path\to\another\FS25\mods" `
  --server-key $ServerKey `
  --server-name $ServerName
```

The publication root is first modified when a validated versioned release is
captured. `Current` is swapped only after the complete pack and hashes validate.
Google Drive is used as a mounted filesystem; no Google API is involved.

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
python -c "from fs25_network_core.database import Database; d=Database().db; print('SERVERS'); print(d.sin_servers.find_one({'server_key':'sin-fs25-01'})); print('ACTIVE WORLD'); print(d.world_generations.find_one({'server_key':'sin-fs25-01','save_key':'sin-fs25-hobo-v1','state':'active'})); print('LATEST SNAPSHOT'); print(d.server_snapshots.find_one({'server_key':'sin-fs25-01','save_key':'sin-fs25-hobo-v1'}, sort=[('received_at',-1)]))"
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
