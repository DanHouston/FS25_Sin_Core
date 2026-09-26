# FS25 Network Core

All project mods use `FS25_SiN_<Purpose>` for their folder and ZIP names:
`FS25` identifies the game, `SiN` identifies SimNet, and the suffix identifies
the mod's purpose. The server runtime mod is `FS25_SiN_Server`.

Start local game integration with the [local testing guide](docs/local-testing.md).
The first custom mod exports telemetry from a disposable save; its offline harness
requires neither Discord nor MongoDB.

See [Discord channels and authorization](docs/discord-and-authorization.md) for
the channel layout, account linking, farm role workflow, and custom mod contract.
See [deterministic FS25 modpack distribution](docs/modpack-distribution.md) for
the explicit capture, validation, publication, and client synchronization flow.
See [validation architecture](docs/validation-architecture.md) for the
fail-first source, contract, scenario, packaged-artifact, and live validation
boundaries.
The new authorization workflow supersedes the legacy manual `farm_links` helper
for banking: only mod-confirmed active farm managers can transact with a farm.

Initial Python backend for a network of FS25 servers. Includes MongoDB identity
links, an atomic central wallet and withdrawal queue, Discord commands, local
farm snapshot parsing, and economy speed calculations. It does **not yet deliver
money or change production speeds in a running game**.

## Identity and onboarding

Wallets belong to Discord IDs, stored as strings. Farm links use
`(server_id, save_id, discord_id)` and carry the in-game farm ID. Change `save_id`
whenever a save is reset or replaced: farm IDs are not permanent across saves.
The authorization workflow allows one desired manager per farm, multiple
workers/visitors, and one farm per user per save.
Farm migration needs an operator workflow; never silently reuse old mappings.

Players submit `/farm_request server:<server>` and choose a numbered field from
the current-world map; the approved application supplies the farm name.
An operator creates the farm and assigns starting land in game, then uses
`/farm_approve` to associate the requester with a player identity observed by
the mod. Staff confirms that identity and land allocation before approving.
`/farm_assign` handles later role changes. Player self-linking is disabled.
The mod must acknowledge the applied assignment before
banking is authorized. Matching farm ID/name verifies the snapshot, not player
identity or current permissions. `AdminManager.link(...)` is a legacy helper
and its records do not authorize banking. No unverified `dedicatedServer.xml`
setting is written; verify the farm-creation restriction on your actual server.

## Banking

Amounts are whole game-currency units. A deposit requires a unique, immutable
source transfer ID and trusted evidence identifying sender, amount, bank
destination, and successful completion. An admin balance delta alone is not
enough. `credit_verified_transfer` is an operator/adapter interface, not a public
endpoint. Its evidence argument records an attestation; it does not validate
an Advanced Bank System transfer automatically. Shared-farm personal attribution
requires a mod event that identifies the player; this version uses the approved
single manager mapping.

`/deposit amount` explains the verification workflow. `/balance` displays funds.
`/withdraw server amount` atomically reserves funds and creates a durable queue
record using the Discord interaction ID. Delivery is not reported as successful
until a trusted operator/adapter calls `settle_withdrawal` with a durable receipt.
A definitively failed delivery refunds exactly once. An ambiguous timeout must
remain pending until reconciled; never automatically refund or resend it.

MongoDB transactions make reservation, crediting, and settlement atomic. Unique
source-transfer and request IDs prevent replay. A local MongoDB **replica set**
(including a single-node replica set) is required; a standalone instance is not
sufficient. MongoDB stores the wallet, not a JSON wallet file.

## Live game adapter still required

There is no implemented or verified GPORTAL command API here, and
`focNetworkAddMoney` is not assumed to be a built-in command. Before enabling
withdrawals, implement and test a server-side integration that:

- Authenticates the coordinator and validates server/save/farm, integer amount,
  and current farm ownership before delivery.
- Accepts a stable operation ID, persists an execution receipt, and provides
  reconciliation after disconnects and restarts. Merely deduplicating in Python
  cannot prevent duplicate in-game money after a network timeout.
- Exposes verified bank transfer events and trustworthy player/factory snapshots.
- Applies production multipliers authoritatively and restores 1.0 on stale input.

Keep withdrawals disabled until this contract is implemented. Do not edit a live
save file to deliver cash. No worker consumes the withdrawal queue in this version.

## Economy

For each factory category, the proposed policy is
`clamp(active_players * target_factories_per_player / factory_count, 1, 3)`.
The default target density is 0.25 factories per active player; 24 active players
and two bakeries yield 3x, four yield 1.5x, and 24 yield 1x. Empty populations or
categories yield 1x. This is a configurable starting policy, not a game mechanic.

Define active players using a rolling window of verified registered identities,
deduplicated across servers if using a network-wide market. Count placed factories
by canonical category. Collect observations at a consistent scope and timestamp;
the calculator itself does not collect or validate telemetry freshness. Add
smoothing and a maximum change rate before wiring it to live production.
Production speed does not itself change FS25 sale prices; price adjustment needs
a separate policy and game integration.

## Local setup

1. Use Python 3.11+ and create a virtual environment; install `requirements.txt`.
2. Put `MONGODB_URI=your-full-Atlas-connection-string` in `atlas-credentials.env`
   in the repository root. Optionally set `MONGODB_DATABASE=fs25_network` there.
   For a local replica set, explicitly use
   `MONGODB_URI=mongodb://localhost:27017/?replicaSet=rs0` instead. There is no
   automatic localhost fallback.
3. `servers.json` contains `local-dev`, configured for test save slot 1. Its
   onboarding records use a separate `fs25_network_local_test` database. When ready,
   add production entries using `servers.example.json` as a template,
   configure real snapshot paths and unique save IDs.  Keep
   `fs25_money_bridge_enabled` false until both receipt-gated native wallet
   directions have passed live validation; the legacy `withdrawals_enabled`
   flag is not sufficient.
4. Set `DISCORD_TOKEN` in the process environment or in the root `.env` file as
   `DISCORD_TOKEN=your-bot-token`. `discord.json` configures guild
   `1547411827539837081`; `DISCORD_GUILD_ID` can override it. Invite
   the application with the bot and application-command scopes to that guild.
5. Run `python -m fs25_network_core.bot_frontend`.

`discord.json` configures Network Admin role `1547417095589994576` for
`/farm_assign`. `DISCORD_OPERATOR_ROLE_IDS` overrides that list when set (an empty
value denies all operators). The local adapter only reads observations; live
permission application and acknowledgments still require implementation. See the authorization guide for Discord
command visibility configuration and the proposed permission policies.

The bot registers guild commands and takes user identity from Discord interactions.
No privileged member/message intents are required. MongoDB and operator Python
interfaces must remain private. Tokens and database credentials belong in the
environment or the ignored private environment files, not configuration committed
to source control. Startup loads `atlas-credentials.env` then `.env` from the
repository root. Existing process variables take precedence, and secret values
are not interpolated. Both private files are covered by `.gitignore`.

After installing requirements, run the economy, configuration, Discord command,
and mocked authorization checks with
`python -m unittest discover -s tests`. The authorization tests verify service
decisions and database query boundaries, not MongoDB transaction isolation.
MongoDB concurrency, crash recovery, Discord, real save schemas, and live game
integration must be validated against your services before production use.
