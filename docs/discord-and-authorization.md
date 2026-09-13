# Admin-controlled farm onboarding

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
them. Role and channel IDs live in `discord.json`.

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

1. Close FS25 and install `dist/FS25_SiN_NetworkLocal.zip` version **0.2.0.0**.
   Remove the old `FS25_NetworkLocal.zip` and enable the renamed mod in the test
   save. This update adds player observations; previous exports have no roster.
2. Restart the bot. `servers.json` now registers **local-dev**, save slot **1**,
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
5. Staff reviews the field choice and creates the named farm in game.
6. With the save running, staff runs `/farm_roster server:local-dev`. It lists
   actual farm IDs and stable game player IDs from the mod. Confirm which game
   player corresponds to the Discord requester; a matching display name alone
   is not proof. Coordinate directly with the player if necessary.
7. Staff runs `/farm_approve server:local-dev`, then selects the requesting
   requester from the pending-request picker, the named farm, and the observed
   player from the command's pickers. The requester picker contains only
   pending requests, so bot accounts cannot appear. Player choices display
   `Nickname: <nickname> | FS25 player ID: <stable ID>`. Confirm
   `identity_and_land_confirmed:true` only after verification. The selected
   player must exist in the fresh roster, the farm must be named, and its name
   must match the request.
8. `/farm_status server:local-dev` shows the land-pending association. The mod
   must acknowledge the exact land operation before authorization becomes active.

To decline a request, staff uses `/farm_reject server member reason`. The
requester can read its state and rejection reason using /farm_status.
Repeated requests preserve the original record; editing/resubmitting after
rejection needs a future explicit workflow. Staff role changes after association
use `/farm_assign`; it cannot create identities or bypass onboarding.

## Snapshot configuration and trust

The local adapter reads the game's redirected Documents directory at
`My Games/FarmingSimulator2025/modSettings/FS25_SiN_NetworkLocal/snapshot.xml`.
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
`operation_type=assign_farmland` command into the existing permission mailbox.
The trusted mod validates the live farm and farmland state, calls the verified
`g_farmlandManager:setLandOwnership(farmlandId, farmId, false)` API only for
unowned land, verifies the resulting owner, and writes a matching receipt.
Already-owned target land is acknowledged idempotently; land owned by another
farm is rejected and never transferred. Authorization remains pending until
the authoritative receipt is processed.

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
