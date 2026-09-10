# Local test: first mod milestone

This build exports farm telemetry every five seconds while the game simulation
runs. It does not link identities, change permissions, move money, or connect to
Atlas. The Python inspector also never connects to the database. The simulator
exercises the same telemetry format and labels its output as simulated.

## Without opening FS25

From the repository root:

```powershell
python -m fs25_network_core.local_test simulate
python -m fs25_network_core.local_test build-mod
```

The simulator creates `local-test/simulated-snapshot.xml` exclusively; for another
run use `--output local-test/another-snapshot.xml`. It never overwrites a snapshot.
The build creates `dist/FS25_SiN_NetworkLocal.zip` with modDesc.xml at the ZIP root.
No credentials, Python dependencies, or private configuration enter the ZIP.

## In FS25

The mod has been renamed to follow the `FS25_SiN_<Purpose>` convention. When
upgrading from the original package, remove the old `FS25_NetworkLocal.zip` from
your game mods folder while FS25 is closed, then install the renamed package.
Enable the new mod entry in your disposable save; FS25 treats it as a different
mod. Its telemetry now lives under `modSettings/FS25SiNNetworkLocal/`.

1. Copy `dist/FS25_SiN_NetworkLocal.zip` into your actual FS25 mods directory, normally
   `Documents/My Games/FarmingSimulator2025/mods`. OneDrive or a custom mods
   directory can change this location. Only install the ZIP, not a duplicate folder.
2. Launch FS25 and create a **new disposable save**. Select **SiN (SimNet) Network Local
   Test** in the mod selection list. Singleplayer is sufficient for this probe.
3. Enter the save and let the simulation run for at least ten seconds, outside
   pause menus. Open another PowerShell window and run:

```powershell
python -m fs25_network_core.local_test inspect "$([Environment]::GetFolderPath('MyDocuments'))/My Games/FarmingSimulator2025/modSettings/FS25SiNNetworkLocal/snapshot.xml"
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
