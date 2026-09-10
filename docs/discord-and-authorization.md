# Admin-controlled farm onboarding

Players request a farm. Only Network Admins associate Discord identities with
observed game players and farms. `/link` is removed on the next successful guild
command sync; old code-issuance and verification methods now reject all calls.

## Channels

| Channel | ID | Commands / purpose |
| --- | --- | --- |
| #link-account | 1547411943311024240 | /farm_request, /farm_status; private responses |
| #bank | 1547412275462406205 | /balance, /deposit, /withdraw; private responses |
| #farm-approvals | 1547412673069584444 | Staff review, roster, approval, rejection, role changes |
| #audit-log | 1547412783564324956 | Reserved for future audit publishing |
| #bridge-alerts | 1547412813390291044 | Reserved for future bridge alerts |

Keep #farm-approvals, #audit-log, and #bridge-alerts private to staff. Other useful
channels remain #start-here, #help, #announcements, #market, #server-status, and
per-server chat/voice. No automatic channel creation or background posting occurs.
The bot responds privately to commands. Staff inspects the request queue with
/farm_requests; requests are not automatically posted to a channel.

The existing #link-account channel can keep its name; its purpose is now farm
requests and status. Role and channel IDs live in `discord.json`.

## Authorization boundaries

Network Admin role `1547417095589994576` is authorized in guild
`1547411827539837081`. `DISCORD_OPERATOR_ROLE_IDS`, when set, overrides the role
list; an empty value denies all operators. Every staff command verifies the role
and guild at execution. Every command enforces its configured channel ID.

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
   **fs25_network_local_test**, separate from the central wallet database.
   The Atlas database user must have access to this test database.
3. In #link-account, submit:

   `/farm_request server:local-dev farm_name:My farm starting_field:<your field>`

   Use the exact created farm name for this test. The requester comes from the
   Discord interaction; players cannot submit another Discord identity, a game
   identity, or a granted role. A request grants no permissions.
4. Staff opens #farm-approvals and runs `/farm_requests server:local-dev`.
   It lists the first 15 pending requests, including requester ID, farm name,
   starting field, and request ID. Requests persist in MongoDB.
5. Staff reviews the field choice, creates the named farm, and assigns the
   starting land in game. For the existing disposable save, inspect farm 1 and
   its land instead of creating an unnecessary second farm.
6. With the save running, staff runs `/farm_roster server:local-dev`. It lists
   actual farm IDs and stable game player IDs from the mod. Confirm which game
   player corresponds to the Discord requester; a matching display name alone
   is not proof. Coordinate directly with the player if necessary.
7. Staff runs `/farm_approve server:local-dev request_id:<request> farm_id:1
   player_id:<observed ID> identity_and_land_confirmed:true` only after that
   verification. The selected player must exist in the fresh roster, the farm
   must be named, and its name must match the request.
8. `/farm_status server:local-dev` shows the approved association and pending
   permission state. **Approval does not yet change game permissions.** The
   delivery/acknowledgment adapter is the next implementation milestone.

To decline a request, staff uses `/farm_reject server request_id reason`. The
requester can read its state and rejection reason using /farm_status.
Repeated requests preserve the original record; editing/resubmitting after
rejection needs a future explicit workflow. Staff role changes after association
use `/farm_assign`; it cannot create identities or bypass onboarding.

## Snapshot configuration and trust

The local adapter reads the game's redirected Documents directory at
`My Games/FarmingSimulator2025/modSettings/FS25SiNNetworkLocal/snapshot.xml`.
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

Starting-field ownership is confirmed manually by staff; the backend records the
confirmation but does not purchase, assign, or validate land through telemetry.
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

Build a server-authoritative adapter that validates current save, identity and
farm, applies absolute role permissions, persists operation/revision receipts,
and acknowledges successful application. Unknown outcomes stay pending. Restart
and duplicate handling must be tested before enabling live delivery.

Manager/worker/visitor/revoked are policy labels, not engine permission flags.
Map and test those policies against FS25; never grant dedicated-server admin
rights from a farm role. Local file telemetry does not itself enforce any role.
No player code-entry menu is needed in this admin-controlled design.
