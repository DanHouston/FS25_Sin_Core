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

FS25_SiN_Server uses the FS25 profile-level writable path returned by
`getUserProfileAppPath()`, specifically
`modSettings/FS25_SiN_Server/serverBindingDiagnostic.xml`. On startup it
creates a diagnostic ID if absent and logs `[SiN Server Binding] persistence
diagnostic created`; later startups load and log the same ID. This is only a
storage proof, not server pairing or credential storage. Test it by stopping
and restarting the server, reloading the save, changing saves with the same
profile, and replacing the mod ZIP; compare the logged ID each time. A real
hosted/GPORTAL test is still required to confirm profile persistence there.

```powershell
python -m fs25_network_core.local_test simulate
python -m fs25_network_core.local_test build-mod
python scripts/run_integration_campaign.py
```

The simulator creates `local-test/simulated-snapshot.xml` exclusively; for another
run use `--output local-test/another-snapshot.xml`. It never overwrites a snapshot.
The build creates `dist/FS25_SiN_Server.zip` with modDesc.xml at the ZIP root.
No credentials, Python dependencies, or private configuration enter the ZIP.
Whenever `mods/FS25_SiN_Server/` changes, rebuild this ZIP before deployment
with `python -m fs25_network_core.local_test build-mod`; FS25 should receive the
ZIP from `dist`, not the source directory.

The offline integration campaign writes `local-test/integration-campaign.json`.
It builds the mod, validates a simulated snapshot, delivers a representative
central operation through the Agent mailbox, consumes it with the protocol
harness, acknowledges its receipt, and verifies that activity minutes are
forwarded before a watermarked disconnect. The report contains no credentials.

### Named integration scenarios

The campaign is backed by the reusable `ScenarioRegistry` in
`fs25_network_core.integration_campaign`. Built-in scenarios are deterministic,
offline, and safe to run in CI:

```powershell
python scripts/run_integration_campaign.py --list-scenarios
python scripts/run_integration_campaign.py --scenario artifact-and-snapshot --output local-test/artifact.json
python scripts/run_integration_campaign.py --scenario mailbox-roundtrip --output local-test/mailbox.json
python scripts/run_integration_campaign.py --scenario event-ordering --output local-test/events.json
python scripts/run_integration_campaign.py --scenario farmland-ownership --output local-test/farmland-ownership.json
```

The six authoritative scenario names are `registration`,
`control_plane_backlog`, `activity_disconnect`, `map_contract`,
`contract_scope`, and `authority_regression`. They are the acceptance names;
the semantic and boundary names remain available for focused diagnostics.

`control_plane_backlog` creates and drains a measured 10,000-entry mailbox
through the real Agent control plane and reports `backlog_size`,
`processed_events`, `batches`, and `elapsed_ms`. `activity_disconnect` routes
the ordered events through `CentralEventProcessor`; `map_contract` continues
through central map validation and deterministic rendering.

`farmland-ownership` is a focused Central → Agent mailbox → reference executor
→ receipt → Central reconciliation scenario. Its reference executor keeps an
independent mutable owner map so a requested farm ID is not automatically an
observed owner. It proves receipt-gated idempotency and scope handling only;
run the real-FS25 procedure in [farmland-ownership.md](farmland-ownership.md)
before claiming GIANTS ownership mutation validation.

`offline-full` is the default and produces the complete campaign report. A
consumer can register a `NamedScenario(name, description, execute)` with the
registry and use `run_named_scenario` without duplicating temporary-directory,
report-writing, or secret-handling code. Scenario reports include the selected
scenario name, a passed status, and only stable identifiers and hashes.

## In FS25

The mod has been renamed to follow the `FS25_SiN_<Purpose>` convention. When
upgrading from the original package, remove the old `FS25_NetworkLocal.zip` from
your game mods folder while FS25 is closed, then install the renamed package.
Enable the new mod entry in your disposable save; FS25 treats it as a different
mod. Its telemetry now lives under `modSettings/FS25_SiN_Server/`.

1. Copy `dist/FS25_SiN_Server.zip` into your actual FS25 mods directory, normally
   `Documents/My Games/FarmingSimulator2025/mods`. OneDrive or a custom mods
   directory can change this location. Only install the ZIP, not a duplicate folder.
2. Launch FS25 and create a **new disposable save**. Select **SiN (SimNet) Server**
   in the mod selection list. Singleplayer is sufficient for this probe.
3. Enter the save and let the simulation run for at least ten seconds, outside
   pause menus. Open another PowerShell window and run:

```powershell
python -m fs25_network_core.local_test inspect "$([Environment]::GetFolderPath('MyDocuments'))/My Games/FarmingSimulator2025/modSettings/FS25_SiN_Server/snapshot.xml"
```

Adjust that path if your game profile is elsewhere. Expected output includes
`"source": "game"`, the save slot, persisted runtime generation, increasing
sequence, and actual farm IDs/names.
Leave the save running: snapshots older than thirty seconds are rejected.

Version 0.2.0.0 also includes a `players` mapping keyed by stable game identity.
The game may export an unnamed farm alongside your named farm. The inspector
preserves these records and lists their IDs in `unnamed_farm_ids`; an empty name
does not establish whether a farm is a player or system farm. Duplicate IDs and
missing name attributes are still rejected. This telemetry does not authorize
farm assignments or banking.

If no file appears, look in the game profile's `log.txt` for `[SiN (SimNet) Server]`
or an error mentioning `FS25_SiN_Server`. The Lua code and descriptor must still
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
No local test server is added to the production registry by default.  The
Discord bot discovers production choices from Mongo-backed `sin_servers` and
`sin_saves`.  For the legacy local-development adapter only, start the bot
with `FS25_SERVERS_FILE=servers.json` explicitly.

Version 0.2.0.0 also exports stable player IDs and display names from the game's
user manager. There is no player code-entry menu: staff controls associations.
When that explicit local adapter is enabled, the bot reads this local roster
for `local-dev`, using the separate
`fs25_network_local_test` database. See the admin workflow in
[Discord and authorization](discord-and-authorization.md).
Actual permission application and acknowledgments remain unimplemented. Keep
withdrawals disabled. This local file adapter trusts the operator's PC and is
not a remote server authentication mechanism.

API reference used for XML create/set/save lifecycle:
[GIANTS FS25 FieldManager](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=17&class=183&version=script).

### Continuous local bridge

Run python -m fs25_network_core.local_permission_bridge <modSettings-path> --server local-dev --save local-dev-save-001 --watch only for legacy local permission/event development. It does not deliver farmland ownership operations: those must use the production-shaped Agent and Central `FarmLifecycle` receipt path. Authenticated server events are consumed from the mod events directory and processed once; malformed events are quarantined as .failed. Build dist/FS25_SiN_Server.zip after any source change under mods/FS25_SiN_Server.

The bridge writes authenticated, normalized activity records to Mongo `activity_outbox`. JiN starts one background publisher on Discord readiness and routes records through `sin_servers.discord_activity_channel_id`. Server Offline announcements remain deferred.

### Standalone Agent

The standalone Agent handles pairing plus authenticated snapshot, event, command,
and receipt transport. It does not read MongoDB. Central owns pairing, durable
operation state, and receipt reconciliation; farmland ownership specifically
uses the scoped `FarmLifecycle` path documented in
[farmland-ownership.md](farmland-ownership.md).

Central machine (with `MONGODB_URI` and the normal central Python dependencies):

```powershell
$env:SIN_API_HOST = "0.0.0.0" # use 127.0.0.1 for same-machine development
$env:SIN_API_PORT = "8787"
python -m fs25_network_core.server_api
```

On the dedicated-server VM, install the Agent code under `C:\SiN\Agent\` and
point it at the FS25 profile mailbox. The Agent requires no `MONGODB_URI` or
`MONGODB_DATABASE`:

```powershell
$env:SIN_BACKEND_URL = "https://central.example"
$env:SIN_MAILBOX_DIR = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\modSettings\FS25_SiN_Server"
$env:SIN_POLL_INTERVAL = "2"

# Preferred headless-server bootstrap; no GIANTS interactive console is needed.
python -m fs25_network_core.agent --pair CODE

# After pairing, keep the Agent running for mailbox-driven pairing retries.
python -m fs25_network_core.agent --watch
```

For local development, `SIN_BACKEND_URL` may use `http://127.0.0.1:8787`.
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
$env:SIN_API_PORT = "8787"
python -m fs25_network_core.server_api
```

On the persistent `SiN-FS25-01` VM:

```powershell
$env:SIN_BACKEND_URL = "http://192.168.1.185:8787"
$env:SIN_MAILBOX_DIR = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\modSettings\FS25_SiN_Server"
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

### Minute player activity telemetry

Player activity telemetry is independent from registration, farm membership, and
manager authority. FS25_SiN_Server samples each connected real player's horizontal
position at one-minute boundaries. A movement distance greater than **0.5 m**
since the previous successful sample is qualifying activity and records one
`active` minute. Smaller movement is treated as no activity: inactive minutes
1-10 are `idle`, and minute 11 onward is `afk`. The first position sample only
establishes a baseline; it does not manufacture a completed minute. If a
position cannot be observed, that interval is unobserved rather than counted.

Each completed interval is emitted as a `player_activity_minute` event under the
existing `events\` mailbox. The Agent forwards it through the authenticated
`POST /api/server/events` path. Central stores one interval record and updates
the cumulative `player_activity_aggregates` document idempotently. Retries use
the deterministic `(server, save, uniqueUserId, session, minute_sequence)` key;
no coordinates are stored centrally. A disconnect ends the session and discards
any partial interval. Reconnect starts a new inactivity streak, even if the
stable `uniqueUserId` is unchanged.

To test manually, leave a registered or unregistered real player stationary for
10 completed samples, verify idle totals, then observe the 11th sample become
AFK. Move the player beyond the tolerance and verify the next completed sample
is active and the inactivity streak resets. Repeat with the Agent temporarily
stopped; event XML should remain in `events\` and be delivered once after the
Agent/API returns. The dedicated-server pseudo-user is excluded.

### Remote player registration

Player identity registration is a separate durable link from Discord user ID to
the stable FS25 `uniqueUserId`. It does not approve an application, create a
farm, assign land, or grant manager authority. The authoritative flow is:

```text
FS25_SiN_Server registration-request XML
  -> Mongo-free Agent
  -> authenticated POST /api/server/registration/request
  -> registration_codes/game_identities in fs25_network
  -> registration-response XML
  -> FS25_SiN_Server prompt and quarantine state
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
same generic FS25_SiN_Server ZIP must be installed on server and clients.

Live process commands:

```powershell
# central host
$env:MONGODB_DATABASE = "fs25_network"
$env:SIN_API_HOST = "0.0.0.0"
$env:SIN_API_PORT = "8787"
python -m fs25_network_core.server_api

# dedicated-server VM
$env:SIN_BACKEND_URL = "http://192.168.1.185:8787"
$env:SIN_MAILBOX_DIR = "C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025\modSettings\FS25_SiN_Server"
python -m fs25_network_core.agent --watch
```

### Persistent-world clock policy

#### Physical save mapping

Each registered server normally exposes one active logical save to Discord,
while Central may retain mappings for more than one physical FS25 save.  The
most recent valid snapshot selects the active mapping.  A first-time physical
save ID must be associated explicitly; it is never guessed from a map name.

Inspect mappings without changing MongoDB:

```powershell
python -m fs25_network_core.save_mapping `
  --server sin-fs25-01 `
  --show
```

Associate a new save ID as a second logical save:

```powershell
python -m fs25_network_core.save_mapping `
  --server sin-fs25-01 `
  --save sin-fs25-hobo `
  --fs25-save-id 3
```

For an intentional physical-ID remap of the existing logical save, require
the old value so an unexpected mapping cannot be overwritten:

```powershell
python -m fs25_network_core.save_mapping `
  --server sin-fs25-01 `
  --save sin-fs25-main `
  --expected-current-fs25-save-id 1 `
  --fs25-save-id 3
```

The Agent needs no save selector and no re-pairing.  Central uses the latest
valid runtime snapshot for normal Discord commands; delayed snapshots from an
older runtime are rejected using the persisted runtime-generation token.

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
atomic `clock-policy.xml` in the mailbox. FS25_SiN_Server reads the concrete
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
## Remote farm lifecycle smoke test

After pairing and configuring `sin-fs25-01` / `sin-fs25-main`, keep the central
API, Agent, and dedicated server running. The first authenticated heartbeat
queues the idempotent `SiN Harvest` system-farm ensure operation. Confirm the
Agent creates a command under `permission-commands/`, FS25_SiN_Server returns a
receipt, and the central `sin_farms` mapping records the actual FS25 farm ID.
Pairing remains valid if this operation is pending while FS25 is offline.

An approved member then runs `/farm_request server:<server>`, reviews the
ephemeral current-world map, selects an available numbered field, and submits
the request. Staff reviews it in #staff with `/farm_approve`.
Confirm the Agent delivers `provision_farm`, FS25_SiN_Server creates/adopts the
named farm, then Central automatically delivers `assign_farmland` for the
field stored by `/farm_request`. The ownership receipt must prove read-back
owner; an already-owned foreign field is refused. The requester remains
non-manager until the ownership and separate manager-permission receipts are
applied. `/farm_status` should progress from `provisioning` through pending
ownership to `awaiting_manager` and `active`; registration, approval, farm
creation, field ownership, and manager authority remain separate records.

If any game mutation may have occurred without a trustworthy receipt, stop
retries and inspect the central operation as `reconciliation_required`. Do not
delete or recreate the farm manually just to clear that state.
