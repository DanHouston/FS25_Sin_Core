# Local test: first mod milestone

This build exports farm telemetry every five seconds while the game simulation
runs. It does not link identities, change permissions, move money, or connect to
Atlas. The Python inspector also never connects to the database. The simulator
exercises the same telemetry format and labels its output as simulated.

## Without opening FS25

From the repository root:

Central JiN and the central pairing API use the shared `MONGODB_DATABASE`
environment setting, defaulting to `fs25_network`. The legacy local mailbox bridge
explicitly uses `fs25_network_local_test` when run through its CLI for backward-
compatible local testing; that exception does not affect central services.

## Server binding persistence proof

NetworkLocal uses the FS25 profile-level writable path returned by
`getUserProfileAppPath()`, specifically
`modSettings/FS25_SiN_NetworkLocal/serverBindingDiagnostic.xml`. On startup it
creates a diagnostic ID if absent and logs `[SiN Server Binding] persistence
diagnostic created`; later startups load and log the same ID. This is only a
storage proof, not server pairing or credential storage. Test it by stopping
and restarting the server, reloading the save, changing saves with the same
profile, and replacing the mod ZIP; compare the logged ID each time. A real
hosted/GPORTAL test is still required to confirm profile persistence there.

```powershell
python -m fs25_network_core.local_test simulate
python -m fs25_network_core.local_test build-mod
```

The simulator creates `local-test/simulated-snapshot.xml` exclusively; for another
run use `--output local-test/another-snapshot.xml`. It never overwrites a snapshot.
The build creates `dist/FS25_SiN_NetworkLocal.zip` with modDesc.xml at the ZIP root.
No credentials, Python dependencies, or private configuration enter the ZIP.
Whenever `mods/FS25_SiN_NetworkLocal/` changes, rebuild this ZIP before deployment
with `python -m fs25_network_core.local_test build-mod`; FS25 should receive the
ZIP from `dist`, not the source directory.

## In FS25

The mod has been renamed to follow the `FS25_SiN_<Purpose>` convention. When
upgrading from the original package, remove the old `FS25_NetworkLocal.zip` from
your game mods folder while FS25 is closed, then install the renamed package.
Enable the new mod entry in your disposable save; FS25 treats it as a different
mod. Its telemetry now lives under `modSettings/FS25_SiN_NetworkLocal/`.

1. Copy `dist/FS25_SiN_NetworkLocal.zip` into your actual FS25 mods directory, normally
   `Documents/My Games/FarmingSimulator2025/mods`. OneDrive or a custom mods
   directory can change this location. Only install the ZIP, not a duplicate folder.
2. Launch FS25 and create a **new disposable save**. Select **SiN (SimNet) Network Local
   Test** in the mod selection list. Singleplayer is sufficient for this probe.
3. Enter the save and let the simulation run for at least ten seconds, outside
   pause menus. Open another PowerShell window and run:

```powershell
python -m fs25_network_core.local_test inspect "$([Environment]::GetFolderPath('MyDocuments'))/My Games/FarmingSimulator2025/modSettings/FS25_SiN_NetworkLocal/snapshot.xml"
```

Adjust that path if your game profile is elsewhere. Expected output includes
`"source": "game"`, the save slot, increasing sequence, and actual farm IDs/names.
Leave the save running: snapshots older than thirty seconds are rejected.

Version 0.2.0.0 also includes a `players` mapping keyed by stable game identity.
The game may export an unnamed farm alongside your named farm. The inspector
preserves these records and lists their IDs in `unnamed_farm_ids`; an empty name
does not establish whether a farm is a player or system farm. Duplicate IDs and
missing name attributes are still rejected. This telemetry does not authorize
farm assignments or banking.

If no file appears, look in the game profile's `log.txt` for `[SiN (SimNet) Network Local]`
or an error mentioning `FS25_SiN_NetworkLocal`. The Lua code and descriptor must still
be validated in your installed game. No game engine was available during build.
The descriptor uses the FS25 baseline version 92; report any loading/schema error.

## Scope and next milestone

Only the host/server exports. Local multiplayer via Create Game can be tested
after the singleplayer probe works. This initial exporter uses a single profile
output path and should run against only one local game instance. Session/save-slot
metadata is diagnostic, not a permanent authenticated identity or save ID.

The XML can briefly be incomplete while the game writes it; the inspector reports
a parse error and can be rerun on the next heartbeat. Files are local, untrusted
telemetry: they must never authorize wallet credits or permission acknowledgments.
No local test server is added to the production `servers.json` yet.

Version 0.2.0.0 also exports stable player IDs and display names from the game's
user manager. There is no player code-entry menu: staff controls associations.
The bot reads this local roster for `local-dev`, using the separate
`fs25_network_local_test` database. See the admin workflow in
[Discord and authorization](discord-and-authorization.md).
Actual permission application and acknowledgments remain unimplemented. Keep
withdrawals disabled. This local file adapter trusts the operator's PC and is
not a remote server authentication mechanism.

API reference used for XML create/set/save lifecycle:
[GIANTS FS25 FieldManager](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=17&class=183&version=script).

### Continuous local bridge

Run python -m fs25_network_core.local_permission_bridge <modSettings-path> --server local-dev --save local-dev-save-001 --watch to continuously poll the existing mailbox. Authenticated server events are consumed from the mod events directory and processed once; malformed events are quarantined as .failed. Build dist/FS25_SiN_NetworkLocal.zip after any source change under mods/FS25_SiN_NetworkLocal.

The bridge writes authenticated, normalized activity records to Mongo `activity_outbox`. JiN starts one background publisher on Discord readiness and routes records through `sin_servers.discord_activity_channel_id`. Server Offline announcements remain deferred.

### Pairing-only standalone Agent

The first production-shaped Agent slice handles only server pairing. It does not
read MongoDB and does not process events, commands, land operations, or receipts.
The central pairing API owns the existing `ServerRegistry.pair_code()` logic.

Central machine (with `MONGODB_URI` and the normal central Python dependencies):

```powershell
$env:SIN_API_HOST = "0.0.0.0" # use 127.0.0.1 for same-machine development
$env:SIN_API_PORT = "8080"
python -m fs25_network_core.server_api
```

On the dedicated-server VM, install the Agent code under `C:\SiN\Agent\` and
point it at the FS25 profile mailbox. The Agent requires no `MONGODB_URI` or
`MONGODB_DATABASE`:

```powershell
$env:SIN_BACKEND_URL = "https://central.example"
$env:SIN_MAILBOX_DIR = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\modSettings\FS25_SiN_NetworkLocal"
$env:SIN_POLL_INTERVAL = "2"

# Preferred headless-server bootstrap; no GIANTS interactive console is needed.
python -m fs25_network_core.agent --pair CODE

# After pairing, keep the Agent running for mailbox-driven pairing retries.
python -m fs25_network_core.agent --watch
```

For local development, `SIN_BACKEND_URL` may use `http://127.0.0.1:8080`.
HTTPS certificate validation is not disabled by the Agent. The manual flow is:

1. Start the central API and the Agent.
2. Run `/server_register` in Discord and copy the one-time code.
3. Run `python -m fs25_network_core.agent --pair CODE` on the VM.
4. Confirm that `permission-commands/server-pairing-response.xml` is consumed
   and `serverBinding.xml` contains the assigned server key and credential.
5. Restart FS25 and confirm the binding is loaded again.

The endpoint is `POST /api/server/pair` with JSON `{"pairing_code":"CODE"}`.
Only the successful response contains the plaintext credential; MongoDB stores
only its hash.

### Live heartbeat and player activity test

On the central host, start JiN and the API as separate processes with
`MONGODB_DATABASE=fs25_network`:

```powershell
$env:MONGODB_DATABASE = "fs25_network"
python -m fs25_network_core.bot_frontend
```

In a separate central-host window:

```powershell
$env:SIN_API_HOST = "0.0.0.0"
$env:SIN_API_PORT = "8080"
python -m fs25_network_core.server_api
```

On the persistent `SiN-FS25-01` VM:

```powershell
$env:SIN_BACKEND_URL = "http://192.168.1.185:8080"
$env:SIN_MAILBOX_DIR = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\modSettings\FS25_SiN_NetworkLocal"
$env:SIN_POLL_INTERVAL = "2"
python -m fs25_network_core.agent --watch
```

Live verification:

1. Keep the dedicated server and Agent running; confirm heartbeat activity in
   the central API/JiN logs and the configured Discord activity channel.
2. Join `sin-fs25-01` from a separate FS25 client and verify a
   `player_connected` event and the expected JiN activity message.
3. Disconnect the client while the dedicated server remains running and verify
   the corresponding `player_disconnected` activity.
4. Reconnect and confirm that the same event files are not replayed as duplicate
   activity.

To inspect failures, list `events\*.failed` under the mailbox and inspect only
   event type/ID metadata; do not copy or print the XML credential attribute.
Transient API failures leave the original `events\*.xml` file in place for retry.

### Remote player registration

Player identity registration is a separate durable link from Discord user ID to
the stable FS25 `uniqueUserId`. It does not approve an application, create a
farm, assign land, or grant manager authority. The authoritative flow is:

```text
NetworkLocal registration-request XML
  -> Mongo-free Agent
  -> authenticated POST /api/server/registration/request
  -> registration_codes/game_identities in fs25_network
  -> registration-response XML
  -> NetworkLocal prompt and quarantine state
```

An unregistered player receives a code targeted to that player and remains out
of a farm while the request is pending. In Discord use `/register code:<CODE>`;
the server key is internal and is not a visible command argument. Codes are
single-use, expiring, hash-backed, and are reusable for the same active FS25
identity request. Registration alone never creates farm authorization.

The dedicated-server pseudo-user (`userId=1`, farm `0`, observed name
`Server`) is excluded from registration and player activity. Registration
request/response XML may contain the observed name and transient user ID for
routing, but `uniqueUserId` is the only durable game identity. The Agent keeps
requests on transient API failures and quarantines malformed or permanently
rejected mailbox files.

For the persistent VM, start the central API and Agent as usual, deploy the
updated mod ZIP if required, then join as an unregistered player. Confirm one
prompt and a registration request, run `/register code:<CODE>`, wait a few
seconds, and confirm the prompt stops and the player is released from the
registration quarantine. Reusing the code must fail. Disconnect/reconnect and
confirm the player is recognized without the registration-required activity
suffix. Inspect failed files in `registration-requests\*.failed` and the
registration response directory without opening or copying credential-bearing
files.

Player activity and registration handling are event-driven on the server. The
mod hooks `FSBaseMission.onClientConnected` for immediate joins and
`FarmManager.playerQuitGame` for immediate leaves. The 20-second heartbeat
performs a lightweight connected-user reconciliation as a fallback for missed
lifecycle callbacks; it must not duplicate transitions already handled by the
callbacks. Registration warnings use a targeted `Event` sent only to the
player's connection and are rendered client-side with the same
`showBlinkingWarning` primitive used by the SiN Stack Assist mod. The exact
same generic NetworkLocal ZIP must be installed on server and clients.

Live process commands:

```powershell
# central host
$env:MONGODB_DATABASE = "fs25_network"
$env:SIN_API_HOST = "0.0.0.0"
$env:SIN_API_PORT = "8080"
python -m fs25_network_core.server_api

# dedicated-server VM
$env:SIN_BACKEND_URL = "http://192.168.1.185:8080"
$env:SIN_MAILBOX_DIR = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\modSettings\FS25_SiN_NetworkLocal"
python -m fs25_network_core.agent --watch
```

### Persistent-world clock policy

Clock policy is stored on the canonical `sin_saves` document as
`clock_policy`. Configure the server/save mapping and policy from a central
host; this command intentionally does not infer mappings from VM traffic:

```powershell
$env:MONGODB_DATABASE = "fs25_network"
python -m fs25_network_core.clock_config `
  --server sin-fs25-01 `
  --save sin-fs25-main `
  --fs25-save-id 1 `
  --timezone America/New_York `
  --offset-minutes -360 `
  --normal-scale 1 `
  --catchup-scale 15 `
  --fast-catchup-threshold 60 `
  --fast-catchup-scale 360 `
  --ahead-scale 0 `
  --tolerance 2 `
  --hard-threshold 180 `
  --check-interval 60 `
  --enabled `
  --hard-resync
```

If the mapping already exists, omit `--fs25-save-id`. The API is authenticated
with the credential in `serverBinding.xml` and requires the runtime
`fs25_save_id` from `snapshot.xml`.

The Agent refreshes the policy during its existing watch process and writes an
atomic `clock-policy.xml` in the mailbox. NetworkLocal reads the concrete
target minute calculated centrally with the configured IANA timezone and offset.
Between refreshes it advances that target by elapsed real time; Lua does not
perform timezone or DST calculations. It uses
`g_currentMission.environment.dayTime` and
`g_currentMission:setTimeScale(...)` on the authoritative server. Positive
forward circular drift above 60 minutes enters 360x fast catch-up; drift above
the two-minute tolerance and at most 60 minutes uses 15x catch-up; synchronized
time restores 1x; an ahead-of-target policy pauses at 0x. Local evaluation is
about 1 second in fast catch-up, 3 seconds in normal catch-up, 2 seconds while
ahead, and 60 seconds when synchronized. Central policy refresh remains
separate and infrequent. No safe FS25 hard-forward API is verified here, so
large drift falls back to accelerated catch-up and logs one limitation message.

For clock correctness, put the game clock more than 60 minutes behind the
target and confirm 360x. As it reaches approximately 60 minutes behind, confirm
an automatic switch to 15x; at tolerance, confirm a return to 1x. Put the game
slightly ahead and confirm 0x until the target catches up, then 1x. Confirm
central policy refresh remains infrequent while local decisions are rapid and
that player/event transport continues during recovery. Test both sides of
midnight (for example 23:50/00:10), and confirm the target remains Eastern local
time minus six hours. Minute-of-day alone cannot distinguish arbitrary
displacements greater than twelve hours, so those cases use the documented
shortest-drift rule and should be manually reconciled.
