# MongoDB structure

Virtual garage additions: `vehicle_garage` stores account ownership, original
server/save/world/native vehicle identity, custody state and operation/audit
references. `vehicle_garage_archives` stores the immutable native XML bytes and
SHA-256 for each custody cycle. Both have no TTL and are retained across farm
decommission and world replacement; include them in backups. A vehicle-code
record's `garage_lock_id` prevents concurrent custody and transfers. Historical
retrieved archives cannot be reused. See [garage protocol](vehicle-garage.md).

The application stores data in MongoDB collections (the closest equivalent to
SQL tables). `MONGODB_DATABASE` selects the database; it defaults to
`fs25_network`. `community_db` in the authorization code is an alias for the
same selected database, not a separate database. The server requires a MongoDB
replica set because money, authorization, and world replacement use transactions.

MongoDB documents are intentionally flexible rather than defined by a single
SQL-style schema. The descriptions below explain each collection's purpose and
its main identity boundaries. Code validation and indexes in
[`database.py`](../fs25_network_core/database.py) are authoritative when this
reference and implementation differ. Most server and farm data is scoped by
`server_key`/`save_key`; observations and other world-specific records also use
`world_id`. Discord IDs and game identity IDs are stored as strings.

## Servers and world identity

| Collection | Purpose |
| --- | --- |
| `sin_servers` | Registered game servers, display/configuration data, credential hash, and current online/runtime status. |
| `sin_saves` | Maps a server's FS25 save slot to SiN's stable `save_key`. |
| `server_snapshots` | Latest authenticated game snapshot for a server, save, and world, including farm, player, and vehicle observations. It is a current-state document, not a history of every poll. |
| `sin_maps` | Validated map geometry and metadata used by field/farmland selection and map reports. |
| `world_generations` | Active and retired world-generation markers for a server/save; separates replacement saves that reuse the same save slot. |
| `world_continuity_migrations` | Auditable, operator-directed migration between world generations. |
| `processed_server_events` | Idempotency markers for authenticated events; records expire after seven days. |

## Farms, identities, and permissions

| Collection | Purpose |
| --- | --- |
| `community_applications` | Farm/community applications and their review state. Despite the code's `community_db` alias, this lives in the selected application database. |
| `link_codes` | Expiring one-time codes for the legacy account/farm linking flow. |
| `game_identities` | Approved links between Discord accounts and FS25 player identities for a server/save. |
| `observed_fs25_identities` | World-scoped identities observed by the game mod before or during registration. |
| `registration_codes` | Pending/used identity verification codes and token hashes; expired codes are removed by TTL. |
| `memberships` | Desired and applied farm roles, including farm manager and contractor relationships. |
| `permission_jobs` | Pending permission changes delivered to the FS25 server and awaiting receipts. |
| `farm_requests` | Member farm creation requests and their approval/provisioning state. |
| `land_operations` | Farmland assignment workflow and receipt evidence for a farm request. |
| `farm_field_reservations` | Prevents two pending farm requests from reserving the same farmland in one world. |
| `sin_farms` | SiN's canonical farm mappings, owner, type, lifecycle state, and observed FS25 farm ID. |
| `farm_links` | Legacy Discord-to-farm link records. These are not sufficient to authorize current banking. |

The read-only farm closure assessment stores its observed cash, loan and
itemized asset/stock valuations inside the existing current-world
`server_snapshots.farm_assessments` field. Source freshness is carried by
`farm_assessment_observed_at`; no closure or wallet-credit record is created
by requesting an assessment. Recipient ownership comes from `sin_farms`.
Schema 2 carries `stock_scope=building_storage_only` and a separate `livestock`
asset category (animal count, per-animal sale price and total); older snapshots
cannot produce a closure credit under this policy.

## Money and farm operations

| Collection | Purpose |
| --- | --- |
| `wallets` | Current Discord user's SiN wallet balance. |
| `ledger_entries` | Immutable wallet balance changes and their transaction references. |
| `wallet_transfers` | Internal wallet-to-wallet transfer records. |
| `admin_payments` | Operator-issued wallet credits/debits and references. |
| `deposit_requests` | Deposit requests and their verification/settlement state. |
| `withdrawals` | Withdrawal requests, reserved funds, delivery state, and settlement evidence. |
| `transfers` | Verified external bank transfer evidence, such as deposits, with replay protection. |
| `farm_operations` | Durable game-side operation queue and receipts, including money, farm provisioning, and vehicle/product transfers. |
| `fs25_money_operations` | FS25-native money mutation state and reconciliation evidence. |
| `bank_bridge_operations` | Operations crossing the bank/FS25 bridge and their reconciliation state. |
| `farm_financial_provisioning` | Provisioning state for farm financial capabilities and accounts. |

## Telemetry, reporting, and vehicle identity

| Collection | Purpose |
| --- | --- |
| `farm_finance_changes` | Observed farm balance changes used for financial and market reporting. |
| `farm_operational_events` | Farm sales, work, contract attribution, and operational observations. Routine storage movements are summarized separately; raw storage rows expire after 14 days and other operational rows after 35 days. |
| `farm_storage_hourly` | Hourly farm storage totals used by reports; records expire 14 days after the last observation. |
| `vehicle_codes` | Stable human-readable vehicle codes mapped to native vehicle IDs within a server/save/world. Codes are globally unique and retained after a vehicle leaves the live inventory. |
| `native_contracts` | Current known lifecycle and metadata for native FS25 contracts. |
| `native_contract_lifecycle_events` | Observed native contract lifecycle transitions, such as available, accepted, completed, or cancelled. |
| `contracts` | SiN-managed contract records used by the older/general contract workflow. |
| `player_activity_minutes` | Deduplicated minute-level player activity observations. |
| `player_activity_aggregates` | Per-player activity totals maintained for the current world. |
| `player_activity_sessions` | Connection/session boundaries and session summaries. |
| `activity_outbox` | Durable messages waiting to be published to the community activity channel. |
| `chat_messages` | Captured in-game chat messages and delivery metadata. |
| `server_status_cards` | Cached status-card configuration/state for a server. |
| `market_daily_finance` | Daily aggregated farm finance used by the market reporting workflow. |

## Community and billing

| Collection | Purpose |
| --- | --- |
| `invoices` | Community invoices, recipients, and payment status. |
| `community_events` | Scheduled community events and their publication state. |

## Indexes, retention, and world replacement

`Database.initialize()` creates the indexes used for uniqueness, query scope,
and expiration. Important protections include unique identity and operation
keys, server/save/world-scoped snapshots and maps, and unique vehicle code
assignments. TTL indexes remove registration/link codes, processed-event
markers, and aged telemetry; MongoDB's TTL monitor performs deletion
asynchronously. Do not treat TTL cleanup as an exact timer.

Replacing a save's world retires the previous `world_id` and archives or
transitions its world-scoped data through the world-generation workflow. Avoid
manual deletion or changing `world_id` values to repair a mapping; use the
world continuity migration workflow when data must move between generations.

For a live database, inspect names and indexes with `show collections` and
`db.<collection>.getIndexes()` in `mongosh`. Never paste credentials or
unredacted player identifiers into an issue or shared log. This document is a
collection catalog, not a production backup/restore procedure; see
[`hobo-save-backup-restore.md`](hobo-save-backup-restore.md) for recovery steps.
