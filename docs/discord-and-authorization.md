# Admin-controlled farm onboarding

## Durable farm lifecycle

Pairing establishes server trust; it does not create a farm. Once an authenticated
server heartbeat reaches the central API, SiN queues an idempotent bootstrap
operation for the shared system farm **SiN Harvest**. The Agent delivers that
operation through `permission-commands/`; the authoritative FS25_SiN_Server runtime
calls the verified `FarmManager:createFarm(name, colorIndex, password, farmId)`
API using a valid built-in color index and an empty password, re-enumerates farms
to learn the actual ID, and returns a durable receipt. A temporary FS25 outage
leaves the operation pending. If FS25 already contains exactly one `SiN Harvest`
farm, the next authenticated snapshot/reconcile adopts that actual ID instead of
creating another farm; duplicates fail closed as `reconciliation_required`.

Member farm requests use the approved application's farm name and a numeric
starting farmland ID. `/farm_request` stores the friendly server selection and
field choice; it never creates a farm or grants authority. Staff uses
`/farm_approve` to queue a `provision_farm` operation. FS25_SiN_Server creates or
adopts the exact named farm, verifies that the requested field is unowned, and
calls `g_farmlandManager:setLandOwnership(farmlandId, farmId)` only when safe.
It then broadcasts the supported `FarmlandStateEvent` so connected clients
receive the same ownership state used by the authoritative server snapshot.
The receipt must prove the actual farm and field owner before SiN queues the
requester's manager permission. The persisted pending manager authorization is
delivered to FS25_SiN_Server before its permission receipt is acknowledged; until
that receipt is applied, the request remains `awaiting_manager`, and only then
does it become `active`.

`/farm_status` takes no server argument and resolves the caller's latest request.
`/server_reconcile server:<friendly selection>` safely re-queues missing system
resources for an already paired server. A failed or uncertain game mutation is
recorded as `reconciliation_required`; operators must reconcile it before retrying.
Ordinary members are never made managers of SiN Harvest, and registration alone
still grants no farm, land, or manager authority.

Farm membership and manager authority are separate. FS25's normal farm-selection
flow removes the player from the old farm, sets the player's `farmId`, and adds
the user to the selected farm with default permissions. SiN does not force an
approved member into SiN Harvest and does not prevent a registered member from
switching farms. The FS25_SiN_Server authority loop only demotes a manager when no
matching persisted SiN manager authorization exists; joining a farm never grants
manager status. There is no SiN-specific contractor role in this path.

Players request a farm. Only Network Admins associate Discord identities with
observed game players and farms. `/link` is removed on the next successful guild
command sync; old code-issuance and verification methods now reject all calls.

## Channels

## Community membership before farms

Players link their stable FS25 identity with `/register code:<CODE>` after the
server-side SiN registration prompt. This identity link is independent of
community application approval and farm authorization; it grants neither a
farm nor manager permissions. The server key is internal metadata and is not a
Discord command argument.

New Discord users first use `/apply nickname:<name> farm_name:<farm>` in any
channel in the configured guild. This creates only a community application; it grants no game,
banking, farm, or wallet access. Network Admins review it in `#sin-applications`
with `/application_pending`, `/application_approve`, or `/application_deny`.
Approval sets the server nickname to `nickname | farm name` and grants the SiN
Member role. Members must not have Change Nickname. SiN JiN needs Manage
Nicknames and Manage Roles, with its role above SiN Member; do not grant
Administrator. Discord channel/role overwrites control visibility; the bot never
creates or dynamically hides channels or roles. Only after community approval may
a member optionally use the separate `/farm_request` workflow below.

| Channel | ID | Commands / purpose |
| --- | --- | --- |
| #link-account | 1547411943311024240 | Legacy organization channel; member responses are private |
| #bank | 1547412275462406205 | Legacy organization channel; member responses are private |
| #farm-approvals | 1547412673069584444 | Staff review, roster, approval, rejection, role changes |
| #audit-log | 1547412783564324956 | Reserved for future audit publishing |
| #bridge-alerts | 1547412813390291044 | Reserved for future bridge alerts |

Keep #farm-approvals, #audit-log, and #bridge-alerts private to staff. Other useful
channels remain #start-here, #help, #announcements, #market, #server-status, and
per-server chat/voice. No automatic channel creation or background posting occurs.
The bot responds privately to commands. Staff inspects the request queue with
/farm_requests; requests are not automatically posted to a channel.

The existing #link-account and #bank channels can keep their names for
organization, but member self-service commands are no longer restricted to
them. Role and channel IDs live in `discord.json`. Optional `channels.jobs` and
`channels.events` values enable persistent contract and community-event cards;
the bot does not guess or create those channels. Set `DISCORD_TIMEZONE` to an
IANA timezone name when event creators should be able to enter local times.

## Authorization boundaries

Network Admin role `1547417095589994576` is authorized in guild
`1547411827539837081`. `DISCORD_OPERATOR_ROLE_IDS`, when set, overrides the role
list; an empty value denies all operators. Every staff command verifies the role
and guild at execution. Staff commands enforce their configured channel ID;
member self-service commands only require the configured guild.

Under Discord Server Settings → Integrations → FS25 Network, allow the Network
Admin role to use all `farm_*` staff commands. Their default visibility requires
Administrator, but the backend additionally requires the configured role. Do not
grant the bot Administrator just to expose commands. Channel overwrites and
command visibility must be configured in Discord; this code does not change them.
See [Discord application commands](https://docs.discord.com/developers/interactions/application-commands).

## Local test procedure

1. Close FS25 and install `dist/FS25_SiN_Server.zip` version **0.2.0.0**.
   Remove the old `FS25_NetworkLocal.zip` and enable the renamed mod in the test
   save. This update adds player observations; previous exports have no roster.
2. For this legacy local adapter, set `FS25_SERVERS_FILE=servers.json` before
   starting the bot, then restart it. This explicitly registers **local-dev**,
   save slot **1**,
   logical save ID **local-dev-save-001**. All its onboarding records go to
   the configured central `MONGODB_DATABASE` (normally **fs25_network**). The
   legacy local mailbox bridge is the only component that may explicitly use
   **fs25_network_local_test** for backward-compatible local testing.
   The Atlas database user must have access to this test database.
3. In any channel in the configured guild, submit:

   `/farm_request server:local-dev starting_field:<your field>`

   Use the exact created farm name for this test. The requester comes from the
   Discord interaction; players cannot submit another Discord identity, a game
   identity, or a granted role. A request grants no permissions.
4. Staff opens #farm-approvals and runs `/farm_requests server:local-dev`.
   It lists the first 15 pending requests, including requester, farm name, and
   starting field. Requests persist in MongoDB.
5. Staff reviews the field choice and runs `/farm_approve server:local-dev`.
   Select the requester from the pending picker. The backend queues a durable
   `provision_farm` operation; staff does not create the farm or guess its ID.
6. Keep the save and Agent running. FS25_SiN_Server creates/adopts the exact
   approved farm name, discovers the actual FS25 farm ID, checks the requested
   field is still unowned, and returns a receipt. A field conflict becomes a
   recoverable reconciliation state and never steals land.
7. The central service then queues the separate manager permission operation.
   Manager authority is not active until FS25_SiN_Server confirms that operation.
8. `/farm_status` takes no server argument and reports provisioning,
   awaiting-manager, active, rejected, or reconciliation-required state.

To decline a request, staff uses `/farm_reject server member reason`. The
requester can read its state and rejection reason using /farm_status.
Repeated requests preserve the original record; editing/resubmitting after
rejection needs a future explicit workflow. Staff role changes after association
use `/farm_assign`; it cannot create identities or bypass onboarding.

## Snapshot configuration and trust

The local adapter reads the game's redirected Documents directory at
`My Games/FarmingSimulator2025/modSettings/FS25_SiN_Server/snapshot.xml`.
Set `FS25_LOCAL_SNAPSHOT` in `.env` to the exact path if using a custom profile.
Snapshots must be from the game, be less than thirty seconds old, and match the
configured save slot. Keep the game simulation running during approval.

This adapter is limited to `development: true` servers and trusts the operator's
PC filesystem. It must not be used for untrusted remote servers. Save slots can
be reused: on resetting/replacing the test save, change `save_id` and reconcile
old jobs before doing more approvals. Timestamp/slot checks do not authenticate
save content. Production needs authenticated, server-scoped transport.

The roster uses the engine's stable unique user ID, not a transient session ID.
The implementation follows [GIANTS FS25 user-manager usage](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=1&class=113&version=script).
Its behavior still needs validation in singleplayer and multiplayer on your game.

## Read-only farmland API diagnostic

The local mod includes a disabled-by-default, read-only farmland diagnostic.
From the authoritative FS25 server console, run `sinFarmlandDiagnostic`, then
search the server log for `[SiN Farmland Diagnostic]`. The probe reports only
objects, bounded field and metatable names, and readable
land/farm/owner/purchase metadata; it does not invoke discovered methods or
change the save.

After `/farm_approve`, the backend writes an idempotent
`operation_type=provision_farm` command into the existing permission mailbox.
The trusted mod validates or creates the exact named farm and farmland state, calls the verified
`g_farmlandManager:setLandOwnership(farmlandId, farmId)` API only for
unowned land, broadcasts `FarmlandStateEvent` to replicate the change, verifies
the resulting authoritative owner, and writes a matching receipt.
Already-owned target land is acknowledged idempotently; land owned by another
farm is rejected and never transferred. The older `assign_farmland` operation
remains supported for compatible existing records. Authorization remains
pending until the authoritative farm and manager receipts are processed.

## Records and transaction behavior

- `farm_requests`: requester, requested server/save, farm name, starting field,
  state, reviewer, decision time, and approval operation ID.
- `game_identities`: unique Discord and stable game identity per server/save,
  approving staff member, approval time, observed session and sequence.
- `memberships`: desired versus applied role, farm and player, revision and state.
- `permission_jobs`: operation ID, desired role, approver and durable acknowledgment.

Approval writes the identity, membership, permission job, and reviewed request
in a single MongoDB transaction. Existing conflicting identities are rejected;
the same game identity cannot be claimed twice in a server/save. Old self-linked
identities without staff approval cannot be used for assignments.

Starting-field ownership is assigned by a durable server-authoritative operation
and remains pending until the mod acknowledges the exact server, save, farm,
field, and operation ID. Staff do not join the farm, add money, or purchase the
starting field manually.

Manager authorization is also enforced immediately from FS25's
`MessageType.PLAYER_FARM_CHANGED` message, after the engine publishes the farm
transition. The mod resolves the affected user by `uniqueUserId`, demotes
unauthorized managers, and promotes only persisted SiN-authorized managers.
The existing periodic reconciliation remains a backstop. FS25 does not expose a
pre-transition hook here, so the engine may still establish its initial manager
state before this post-transition callback runs.
Requests never establish land reservations. Managers can later change game assets.

Only active, mod-confirmed farm managers can use farm banking. Pending permission
changes block that access. Local development withdrawals are disabled, and its
test identities do not authorize central wallets. /balance still shows the central
Discord wallet, not a test wallet. No money is moved by onboarding.

One desired manager per farm and one farm per user per save are supported. Other
members may be workers or visitors. Only one pending permission operation per
membership is allowed. Farm moves, identity corrections, manager handover,
rejected-job recovery and pending-job supersession need further workflows.

## Remaining game integration

## SiN JiN services farm

`local-dev` reserves Farm 2, **SiN JiN | SiNful Harvest**, as its system farm.
It owns no land and is not a player home farm. It may own shared equipment.
The system-farm ID and exact name are configured per server; player farm approval
and manager restoration must never assign or promote a player as its manager.
Contractor access for shared equipment is a separate engine-permission policy.

Build a server-authoritative adapter that validates current save, identity and
farm, applies absolute role permissions, persists operation/revision receipts,
and acknowledges successful application. Unknown outcomes stay pending. Restart
and duplicate handling must be tested before enabling live delivery.

Manager/worker/visitor/revoked are policy labels, not engine permission flags.
Map and test those policies against FS25; never grant dedicated-server admin
rights from a farm role. Local file telemetry does not itself enforce any role.
No player code-entry menu is needed in this admin-controlled design.
