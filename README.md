# FS25 Network Core

Central services and the JiN Discord bot for SiN's FS25 servers, with a Windows
Agent and the `FS25_SiN_Server` game runtime. Standalone crop, contract and
economy policy mods are packaged alongside the runtime in `dist`.

Start with the [complete JiN command guide](docs/bot-commands.md) for all user
and admin commands, arguments, permissions, channels and examples. Use the
[local testing guide](docs/local-testing.md) for disposable-save development.

See [Discord channels and authorization](docs/discord-and-authorization.md) for
the channel layout, account linking, farm role workflow, and custom mod contract.
See the [MongoDB structure guide](docs/database-structure.md) for the collection
catalog, identity scopes, and retention behavior.
See [deterministic FS25 modpack distribution](docs/modpack-distribution.md) for
the explicit capture, validation, publication, and client synchronization flow.
See the [deployment runbook](docs/deploy-runbook.md) for copy/paste release,
server, Agent, client, modpack, hash, rollback, and live-validation commands.
See [validation architecture](docs/validation-architecture.md) for the
fail-first source, contract, scenario, packaged-artifact, and live validation
boundaries.
The new authorization workflow supersedes the legacy manual `farm_links` helper
for banking: only mod-confirmed active farm managers can transact with a farm.

The current runtime includes community onboarding, verified farm authority,
receipt-gated banking and vehicle transfers, finance/activity reports and a
read-only farm closure assessment. Component availability and live validation
status are documented in the feature guides; a queued operation is not a
confirmed game mutation.

## Identity and onboarding

Wallets belong to Discord IDs, stored as strings. Game identity links are
server/save scoped; farms, authority and operations also identify the active
world generation. Farm numbers and session user numbers are not permanent
identities. See the [database structure](docs/database-structure.md) and
[world continuity guide](docs/world-continuity-migration.md).

Players apply for community membership, obtain staff approval and link their
observed game identity when prompted with a registration code. They then use
`/farm_request server:<server>` and choose a numbered field from the current-world
map. Submission reserves its farmland; the approved application supplies the
farm name. Staff `/farm_approve` queues native farm provisioning and the selected
land assignment. After FS25 confirms the requested owner, manager authority is
queued separately. Staff do not manually create the farm or buy starting land
for this workflow. Financial provisioning has a separate capability state.
`/farm_assign` handles later roles for an already verified identity. Banking
requires mod-confirmed manager authority. Legacy `farm_links` helpers do not
authorize access. See [onboarding](docs/discord-and-authorization.md).

## Banking

Amounts are whole game-currency units. `/deposit amount:<integer>` moves verified
farm cash into SiN checking; `/withdraw amount:<integer>` reserves SiN funds for
delivery to the verified farm. Both require the enabled money bridge and an
authenticated native receipt before final settlement. `/balance` shows funds,
pending amounts and recent outcomes. Native FS25 finance entries use **Misc**;
separate custom finance rows remain future work.

The command's private response in the invoking channel updates on confirmation
within its interaction window. No deposit/withdrawal DM is sent. Use `/balance`
for later results or after a bot restart. A definitively failed withdrawal
refunds once; an uncertain operation remains pending for reconciliation.
`/pay` and invoices move SiN wallet funds without changing native farm cash.
See [community banking](docs/community-workflows.md#banking) and
[bank finance validation](docs/bank-finance-validation.md).

MongoDB transactions make reservation, crediting, and settlement atomic. Unique
source-transfer and request IDs prevent replay. A local MongoDB **replica set**
(including a single-node replica set) is required; a standalone instance is not
sufficient. MongoDB stores the wallet, not a JSON wallet file.

## Game integration

The authenticated Central/Agent mailbox carries snapshots, commands and durable
game receipts. The server mod validates scope and native state, applies supported
operations and reads back their results. Unsupported or uncertain outcomes remain
pending validation/reconciliation. Native money operations are configuration-gated.
This does not use an assumed hosting-provider console API or live save-file edits.

Vehicle transfers use permanent SiN codes, validate source ownership, relocate to
the native shop and confirm the destination owner. Cross-server movement is
unavailable. An admin-only [personal garage](docs/vehicle-garage.md) is prepared
for validation, with native archives and live/save checkpoint requirements.
See [vehicle integration](docs/vehicle-inventory-and-garage.md).
`/farm_assess_admin` reports a proposed closure credit; it does not execute a
settlement or shut down the farm. See [closure assessment](docs/farm-closure-assessment.md).

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
3. Production server/save discovery comes from Central's registry. `servers.json`
   is an explicit local-development adapter, loaded only with
   `FS25_SERVERS_FILE=servers.json`. JiN uses the configured central database;
   this file does not redirect onboarding into a separate database. Register
   production servers with `/server_register`, then pair/configure the Agent and
   active save using the [deployment runbook](docs/deploy-runbook.md). Enable
   `fs25_money_bridge_enabled` only after the native money directions have been
   validated on that deployment; `withdrawals_enabled` alone is insufficient.
4. Set `DISCORD_TOKEN` in the process environment or in the root `.env` file as
   `DISCORD_TOKEN=your-bot-token`. `discord.json` configures guild
   `1547411827539837081`; `DISCORD_GUILD_ID` can override it. Invite
   the application with the bot and application-command scopes to that guild.
5. Run `python -m fs25_network_core.bot_frontend`.

`discord.json` configures Network Admin role `1547417095589994576` for
staff commands. `DISCORD_OPERATOR_ROLE_IDS` overrides that list when set (an empty
value denies all operators). Discord command visibility and the backend role
check are separate. See the [command guide](docs/bot-commands.md#permissions-and-channels)
for the exact channel and default command permissions.

The bot registers guild commands and takes user identity from Discord interactions.
The current bot enables Message Content intent for the configured Activity-channel
chat bridge; enable it in the Discord application settings. Slash commands take
identity from their interactions. MongoDB and operator Python interfaces must
remain private. Tokens and database credentials belong in the
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
