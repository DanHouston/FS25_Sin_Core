# JiN bot command reference

All **55 registered Discord slash commands**, reviewed against
`fs25_network_core/bot_frontend.py` and `channel_policy.py` on 2026-10-18.
This is the command reference; the linked feature guides explain the workflows
and implementation in more depth. FS25 console commands and deployment scripts
are covered by their feature guides, not this slash-command catalog.

## How to use this guide

- `<value>` is required; `[option:value]` is optional. Do not type the brackets.
- Examples show option names and example values. Enter the command in Discord,
  then fill its option controls. Names, messages and descriptions may contain spaces.
- Choose a server before selecting a farm, requester or native contract. Type
  part of a name and pick an autocomplete result. Display names are examples;
  the servers, farms and members available to you depend on the deployment.
- `<pick>` means select a matching result, not enter the literal word "pick".
  `<id>` means the durable record ID returned by JiN; autocomplete is available
  where noted. A vehicle code such as `PJT7H` is entered without square brackets.
- Commands run in the configured Discord server. They do not run in bot DMs.
  **Any channel** below means any accessible channel in that server.
- The invocation response is private to the caller in the invoking channel
  (ephemeral). A mod suggestion opens a form first. Some commands also create
  public forum posts, job/event cards or game chat messages, as described below.
- Amounts/rates are whole currency units from **1 to 1,000,000,000** unless
  otherwise stated. Available funds and workflow checks still apply.

## Permissions and channels

| Label | Meaning |
| --- | --- |
| Member/self-service | No Network Admin role check. Each workflow still checks its identity, ownership, participant or balance requirements. |
| Manager | An active, verified manager of the relevant farm is required for farm banking, vehicle inventory, farm reports and member vehicle transfers. |
| Staff | Requires a configured Network Admin/operator role in the configured guild. Discord Administrator alone does not satisfy this backend check. |
| Staff (A) | Also declares Administrator as its default Discord command permission. Server integration command permissions can grant visibility to the intended role; the backend role check still applies. |

Channel routing uses configured **channel IDs**, not names. This guide uses
`#sin-apply` for `channels.sin_apply`, `#staff` for `channels.staff`, and
`#operations` for `channels.operations`. Renaming a channel does not change
its routing. Staff commands require their configured channel. Most self-service
commands work in any channel; `/apply` is the exception.

## User commands

### Community membership and registration

| Command and arguments | Purpose and requirements | Example |
| --- | --- | --- |
| `/apply nickname:<text> farm_name:<text>` | In **#sin-apply**. Submit a community application. Both names are required, may not contain a pipe, and their combined `nickname \| farm name` display name must fit 32 characters. Approval is separate from game registration and farm authority. | `/apply nickname:Repton farm_name:Repton Does` |
| `/register code:<text>` | In any channel. Link your Discord account to the observed FS25 identity using the targeted registration code shown by the game. Requires approved community membership and a valid code. Does not grant a farm or manager rights. | `/register code:<code from game>` |
| `/suggest_mod` | In any channel. Open the Mod Name, Link, What does it add?, and Why should SiN add it? form. Submission publishes a credited forum post in the configured mod-suggestions forum. | `/suggest_mod` |

When an approved stable identity is already known on the same registered game
server, registration in another logical save may happen automatically. Use a
code when prompted; do not substitute a session user number or farm ID.

### Farm, finance and vehicle reports

All commands in this table work in any channel.

| Command and arguments | Purpose and requirements | Example |
| --- | --- | --- |
| `/farm_request server:<pick>` | Approved, registered player: open the current-world map and choose a starting field. Submitting reserves its associated farmland and creates a pending request. The approved application supplies the farm name. One personal farm/in-flight request per identity in the current world. | `/farm_request server:sin-fs25-01` |
| `/farm_status` | Show your latest farm request, assignment and permission synchronization state. No server option. | `/farm_status` |
| `/balance` | Show your SiN balance, pending deposits/withdrawals and recent bank outcomes. Native farm cash is shown only when verified manager context and authoritative observations are available; the SiN balance can still be shown without them. | `/balance` |
| `/equity` | Show SiN checking plus verified farm cash, land, building resale and equipment resale. The displayed total is before loans and excludes stock, livestock and pending bank transfers. This is different from the closure assessment. | `/equity` |
| `/farm_report [days:7] [server:<pick>]` | Manager: historical finance/activity report for your farm, including observed completed native contracts and measured player/AI work. `days` is 1-30 real-world days, default 7. Select a server if your identity context is ambiguous. | `/farm_report days:7 server:sin-fs25-01` |
| `/vehicles [server:<pick>] [page:1]` | Manager: list your farm's live owned vehicles/implements with permanent SiN codes and readable resale values. Ten vehicles per page; `page` is 1-500, default 1, and must exist. Select a server if needed. | `/vehicles server:sin-fs25-01 page:2` |
| `/activity_status [member:<Discord member>]` | Without `member`, show your own current-world connected/active/idle/AFK telemetry in any channel. **Supplying `member` requires Staff and #operations**, even when selecting yourself. Requires an identifiable game context. | `/activity_status` |

Opening the farm picker reserves nothing; submission rechecks the selected
field. Staff approval queues provisioning and land assignment. Manager authority
follows FS25's confirmed land ownership. Financial provisioning has a separate
capability state. See [farm onboarding](discord-and-authorization.md) and
[farm reports](farm-finance-telemetry.md).

### Money and vehicle transfers

All commands in this table work in any channel.

| Command and arguments | Purpose and requirements | Example |
| --- | --- | --- |
| `/deposit amount:<integer>` | Manager: transfer native farm money into your SiN account. Requires the enabled native money bridge and an unambiguous eligible identity context. The game debit must be confirmed before SiN credit. | `/deposit amount:100` |
| `/withdraw amount:<integer>` | Manager: reserve SiN funds for delivery to your verified farm. Requires available funds and the enabled bridge. Completion requires the game's confirmed credit. | `/withdraw amount:100` |
| `/pay player:<Discord member> amount:<integer> memo:<text>` | Transfer SiN account funds to another linked player. Memo is required; self-payment and bot recipients are rejected. The payer receives private confirmation; the recipient gets a best-effort DM receipt. Does not move native farm cash. | `/pay player:@Matt70 amount:500 memo:Field work` |
| `/vehicle_transfer destination_server:<pick> destination_farm:<pick> vehicle_code:<code>` | Verified source-farm manager: send an owned vehicle to another farm on its **current server/world**. Pick the destination by owner/farm name. No recipient approval is required. | `/vehicle_transfer destination_server:sin-fs25-01 destination_farm:<pick Matt's farm> vehicle_code:PJT7H` |

Bank commands update the same private channel response when confirmation arrives
within the interaction window (up to 15 minutes). They do not DM the caller.
After a restart or a later result, use `/balance`. A pending or uncertain bank
operation must be reconciled before repeating it.

Vehicle transfer confirmation updates the initiating private channel response
while its in-memory tracking is available (14 minutes, lost on bot restart).
JiN sends no transfer-result DM. For a later result, ask staff to inspect
`/transfer_list`. FS25 validates ownership, occupancy, AI use, attachments and
shop space; success requires destination ownership and relocation to the shop.
Detach the equipment and stop using it before transfer. A definitively failed
transfer needs a new request after the cause is corrected. If a completed
transfer went to the wrong farm, that farm must initiate a transfer back.
Cross-server transfers are unavailable. Garage commands are admin-only and
listed below; their implementation still needs in-game validation.

### SiN work contracts

These are community work agreements, distinct from native FS25 missions.
All commands work in any channel; they do not require a Network Admin role.

| Command and arguments | Purpose and requirements | Example |
| --- | --- | --- |
| `/contract_create work_type:<choice> fields:<text> compensation:<choice> rate:<integer> [description:<text>] [server:<pick>]` | Create a scoped work agreement. `fields` is comma-separated positive field numbers. Supply a description: Discord marks it optional, but the current service requires nonempty text. Server can be omitted only with one eligible identity context. Posts a job card when configured. | `/contract_create work_type:plowing fields:19,33 compensation:fixed rate:5000 description:Plow both fields server:sin-fs25-01` |
| `/contract_list` | List open SiN work contracts. | `/contract_list` |
| `/contract_view contract_id:<pick>` | View terms, state and recorded acceptance. Autocomplete lists open contracts; a known ID can be entered directly. | `/contract_view contract_id:<pick open job>` |
| `/contract_accept contract_id:<pick>` | Accept an open contract. The creator cannot accept their own. | `/contract_accept contract_id:<pick open job>` |
| `/contract_complete contract_id:<id> [note:<text>]` | Creator or acceptor: mark an accepted/in-progress agreement completed. Note is optional, up to 500 characters. Its autocomplete currently lists open contracts, so use the accepted contract's ID when it is absent from suggestions. Completion itself does not pay the other party. | `/contract_complete contract_id:<accepted job ID> note:Both fields finished` |

Work choices: `baling`, `cultivating`, `fertilizing`, `forestry`, `harvesting`,
`liming`, `mowing`, `planting`, `plowing`, `rolling`, `spraying`, `transport`.
Compensation choices are **Fixed price** (`fixed`) and **Hourly** (`hourly`);
`rate` is the total fixed price or hourly rate. Descriptions allow up to 1,000
characters. Arrange payment separately using `/pay` or an invoice.

Job cards have Accept and creator-controlled Cancel actions. Cancellation asks
for a reason and retains the history card. There is no `/contract_cancel` slash
command. See [community workflows](community-workflows.md).

### Invoices

All commands work in any channel. Invoice visibility and payment/cancellation
are restricted to the relevant parties, not every guild member.

| Command and arguments | Purpose and requirements | Example |
| --- | --- | --- |
| `/invoice_create recipient:<Discord member> amount:<integer> description:<text>` | Issue an invoice to someone else. Description is required, up to 1,000 characters. | `/invoice_create recipient:@Matt70 amount:500 description:Field work` |
| `/invoice_list` | List invoices issued by you or addressed to you. | `/invoice_list` |
| `/invoice_view invoice_id:<pick>` | Issuer or recipient: view amount, description and status. | `/invoice_view invoice_id:<pick invoice>` |
| `/invoice_pay invoice_id:<pick>` | Recipient: pay an issued invoice from available SiN funds. Payment credits the issuer through the ledger; repeating the same settled invoice does not pay twice. | `/invoice_pay invoice_id:<pick unpaid invoice>` |
| `/invoice_cancel invoice_id:<pick>` | Issuer: cancel an invoice that is still issued/unpaid. This slash command has no staff override option. | `/invoice_cancel invoice_id:<pick issued invoice>` |

Invoice ID autocomplete uses your issued/received invoices. Invoice payments
change SiN wallets, not native farm money.

### Community events

All user event commands work in any channel. Event creation and completion/
cancellation are staff commands listed below.

| Command and arguments | Purpose and requirements | Example |
| --- | --- | --- |
| `/event_list` | List community events, including their status and scheduled time. | `/event_list` |
| `/event_view event_id:<pick>` | View an event's description, time and participant count. | `/event_view event_id:<pick Harvest Night>` |
| `/event_join event_id:<pick>` | Join an event that is scheduled/active and has capacity. | `/event_join event_id:<pick Harvest Night>` |
| `/event_leave event_id:<pick>` | Remove yourself from an event's participant list. | `/event_leave event_id:<pick Harvest Night>` |

Event ID options have name/status autocomplete. When an events channel is
configured, the persistent board's Join/Leave controls use the same workflows.

## Admin commands

Every command below requires **Staff**. The `A` column identifies the commands
that additionally declare default Administrator visibility. Supplying `member`
to `/activity_status` also requires Staff, but that command has no default
Administrator decorator.

### Applications and server setup — #staff

| Command and arguments | A | Purpose | Example |
| --- | --- | --- | --- |
| `/application_pending` | No | List pending community applications. | `/application_pending` |
| `/application_approve member:<Discord member>` | No | Approve a pending applicant and attempt to synchronize their canonical Discord nickname and SiN Member role. Discord hierarchy failures are reported; they do not reverse the persisted approval. | `/application_approve member:@Matt70` |
| `/application_deny member:<Discord member> reason:<text>` | No | Deny a pending application with a reason. | `/application_deny member:@Matt70 reason:Please contact staff` |
| `/server_register server_key:<text> name:<text> activity_channel:<Discord channel>` | No | Configure/register a server with a friendly name and Activity channel in this guild. Returns a one-time pairing code, expiring after 30 minutes, when new pairing is needed; preserves existing pairing otherwise. `server_key` is a stable internal key, not a farm name. | `/server_register server_key:sin-fs25-01 name:SiN Hobo activity_channel:#hobo-activity` |
| `/server_info server_key:<pick>` | No | Read a registered server's name/key, Activity channel, enabled state and paired state. Server autocomplete is available. | `/server_info server_key:sin-fs25-01` |

### Farm administration and reports — #staff

| Command and arguments | A | Purpose | Example |
| --- | --- | --- | --- |
| `/farm_requests server:<pick>` | Yes | List pending requests for the server's selected save. | `/farm_requests server:sin-fs25-01` |
| `/farm_roster server:<pick>` | Yes | Inspect observed players and farms from the current game snapshot, with paging where needed. Session user numbers are diagnostic values, not durable account identities. | `/farm_roster server:sin-fs25-01` |
| `/farm_approve server:<pick> member:<pick requester>` | Yes | Approve a pending request and queue farm provisioning. Land and manager authority follow confirmed FS25 receipts. Choose the member from pending-request autocomplete; it submits a Discord account ID. | `/farm_approve server:sin-fs25-01 member:<pick Matt70>` |
| `/farm_reject server:<pick> member:<pick requester> reason:<text>` | Yes | Reject the selected pending request with a reason. Uses pending-requester autocomplete, not a native FS25 user number. | `/farm_reject server:sin-fs25-01 member:<pick Matt70> reason:Please choose another field` |
| `/farm_status_staff server:<pick> member:<pick member>` | Yes | Inspect a member's current-world farm lifecycle, identity, authority and permission/operation state. Select the member's name from autocomplete. | `/farm_status_staff server:sin-fs25-01 member:<pick Matt70>` |
| `/farmland_status server:<pick> farmland_id:<integer>` | Yes | Read current authoritative ownership of a farmland parcel. ID must be positive. A field number may differ from its farmland ID. | `/farmland_status server:sin-fs25-01 farmland_id:33` |
| `/farm_assign member:<Discord member> server:<pick> farm_id:<integer> role:<choice>` | Yes | Assign/revoke an already verified player's farm role. `farm_id` is a positive native farm ID; this option has no farm-name autocomplete. Valid roles: `farm_manager`, `contractor`, `worker`, `visitor`, `revoked`. Applied authority waits for game confirmation. | `/farm_assign member:@Matt70 server:sin-fs25-01 farm_id:3 role:worker` |
| `/farm_report_admin server:<pick> farm_id:<pick farm> [days:7]` | Yes | Report finance/activity and measured native-contract work for another farm. Select the farm by name after the server; the option remains named `farm_id`. Native IDs are 1-254. Window is 1-30 real-world days, default 7. | `/farm_report_admin server:sin-fs25-01 farm_id:<pick Matt's farm> days:7` |
| `/farm_assess_admin server:<pick> farm:<pick farm>` | Yes | Read-only closure assessment and itemized CSV: cash, loan, land/equipment/buildings, eligible building stock, livestock and proposed SiN credit. Missing coverage withholds a final proposed credit. Does not sell assets, credit money or close the farm. | `/farm_assess_admin server:sin-fs25-01 farm:<pick Matt's farm>` |
| `/contract_report server:<pick> contract_code:<pick mission>` | Yes | Inspect measured player/AI work on a native FS25 mission. Choose by field/type from recent current-world mission autocomplete. Only measured linked work is attributed. This is separate from a SiN work agreement. | `/contract_report server:sin-fs25-01 contract_code:<pick Field 19 Plow>` |

Farm report, assessment and vehicle destination pickers use the owner/farm-name
pattern, for example **Repton | Repton Does**, when a canonical owner is known.
A farm without that owner context falls back to its observed name; IDs remain
internal picker values. The numeric `farm_id` in `/farm_assign` and
`farmland_id` in `/farmland_status` still require explicit IDs.

Closure credit is a preview of eligible farm net value, with the existing SiN
balance displayed separately. Building-only stock and livestock require server
mod **0.2.2.25**, the updated Agent and updated bot/core. Older inventories do
not produce a final proposed credit. Loaded livestock trailers/ridden animals
must be returned to an owned barn. Bunker and stored bale/pallet inventories
can still require separate review. See [closure assessment](farm-closure-assessment.md)
for valuation rules and live validation status.

### Money, vehicle transfers and events — #staff

| Command and arguments | A | Purpose | Example |
| --- | --- | --- | --- |
| `/admin_pay player:<Discord member> amount:<integer> memo:<text>` | Yes | Issue a system-funded SiN credit to a linked player, with an audit memo. Neither the admin's wallet nor native farm cash is debited. Private admin confirmation; best-effort recipient DM. This command is currently named `admin_pay`, not `pay_admin`. | `/admin_pay player:@Matt70 amount:500 memo:Approved reimbursement` |
| `/vehicle_transfer_admin destination_server:<pick> destination_farm:<pick> vehicle_code:<code> reason:<text>` | Yes | Initiate an audited transfer without source-manager authority. Reason is required. Same server/world restriction, live checks, shop relocation and private confirmation as the user command. | `/vehicle_transfer_admin destination_server:sin-fs25-01 destination_farm:<pick Matt's farm> vehicle_code:PJT7H reason:Approved farm move` |
| `/vehicle_garage_store_admin server:<pick> vehicle_code:<code> reason:<text>` | Yes | Preserve one owned vehicle for its canonical farm owner's account. Final storage requires independent archive durability, live removal and native save confirmation. Audit reason is 1-200 characters. | `/vehicle_garage_store_admin server:sin-fs25-01 vehicle_code:PJT7H reason:Retain before farm closure` |
| `/vehicle_garage_admin member:<Discord user> [page:1]` | Yes | Inspect a farmer's retained vehicles and pending/review operations. Failed stores and completed retrievals are excluded; results go to the originating command's private reply while available. Ten rows per page, up to 100 records; pages 1-10. Stored vehicles earn no farm sale credit. | `/vehicle_garage_admin member:@Matt70 page:1` |
| `/vehicle_garage_retrieve_admin destination_server:<pick> destination_farm:<pick> vehicle_code:<code> reason:<text>` | Yes | Restore a stored vehicle at the shop in the original active server/world to a farm owned by its account holder. Requires an available mod, vehicle slots and shop space; archive custody stays held until a native save confirms retrieval. | `/vehicle_garage_retrieve_admin destination_server:sin-fs25-01 destination_farm:<pick Matt's farm> vehicle_code:PJT7H reason:Farmer returning` |
| `/transfer_list` | Yes | List up to 50 most recent durable vehicle/product transfer records with status and readable failure reason where available. Does not list bank deposits/withdrawals. | `/transfer_list` |
| `/transfer_dispatch transfer_id:<id>` | Yes | Dispatch/revisit an existing requested/pending-game transfer after a dispatch problem. No autocomplete. It cannot reopen a completed or definitively failed transfer; a failed transfer needs a new request. | `/transfer_dispatch transfer_id:<transfer UUID>` |
| `/event_create name:<text> description:<text> scheduled_start:<text>` | Yes | Create a community event and post its board when configured. Name is required, up to 120 characters; description up to 1,000. Use an ISO timestamp with offset, or local `YYYY-MM-DD HH:MM` in `DISCORD_TIMEZONE` (default UTC). | `/event_create name:Harvest Night description:Community harvesting scheduled_start:2026-10-25T19:00:00-04:00` |
| `/event_cancel event_id:<pick>` | Yes | Mark a nonterminal event cancelled. Event autocomplete is available. | `/event_cancel event_id:<pick Harvest Night>` |
| `/event_complete event_id:<pick>` | Yes | Mark a nonterminal event completed. Event autocomplete is available. | `/event_complete event_id:<pick Harvest Night>` |

The current transfer command names are `/vehicle_transfer` and
`/vehicle_transfer_admin`. `/transfer_vehicle` and `/admin_transfer_vehicle`
are retired names, not registered aliases.

The first garage version is admin only and requires server mod 0.2.2.26 plus
updated Central/API, Agent and JiN. Before decommissioning a farm, wait for
every retained vehicle's status to become **stored**, then rerun its assessment.
Use the [garage guide](vehicle-garage.md) for the save-confirmation and recovery
rules. Full behavioral/in-game validation remains pending.

### Operations — #operations

| Command and arguments | Access | Purpose | Example |
| --- | --- | --- | --- |
| `/server_reconcile server:<pick>` | Staff (A) | Reconcile required server resources, including the SiN Harvest system farm. Reports active, pending or reconciliation required. Does not authorize guessing a farm or forcing an uncertain operation. | `/server_reconcile server:sin-fs25-01` |
| `/chat_send server:<pick> message:<text>` | Staff (A) | Queue a message to the selected FS25 server's chat. Message is required, sanitized, up to 500 characters. Queueing is not proof that clients saw it; an unsupported native runtime remains pending validation. | `/chat_send server:sin-fs25-01 message:Restart in ten minutes` |
| `/activity_status member:<Discord member>` | Staff | Inspect another member's current-world activity telemetry. Same command as the user self-service form; specifying any member activates the role/channel checks. | `/activity_status member:@Matt70` |

## Common problems

| Symptom | What to check |
| --- | --- |
| Command not visible | The running JiN version and its command sync, Discord integration command permissions, and access to the required channel. A Staff (A) command still requires the configured operator role at execution. |
| No server/farm suggestions | Choose the server first. Verify an active save/world, a fresh Agent snapshot, and the caller's applicable identity context. Some staff pickers also require the operator role before returning suggestions. |
| Farm ID versus field/user ID confusion | Use name pickers where provided. `/farm_assign` needs a farm ID; `/farmland_status` needs a parcel ID. Neither takes an FS25 session user number. |
| Transfer is queued | Wait for the game receipt. Staff can inspect `/transfer_list`; do not treat submission as success or resend an uncertain move. |
| Bank result arrives late | Use `/balance`; its pending amounts and latest definitive result survive a bot restart. |
| Assessment has no proposed credit | Read its CSV's unresolved rows and the closure guide. Stale or incomplete valuation and unresolved ownership/operations block the final figure. |

## Keeping the reference current

When a slash command is added, removed or renamed, update its entry, arguments,
example, access and channel here in the same change. Check the actual handler,
autocomplete decorators and `channel_policy.py`; descriptions alone do not
capture conditional access such as `/activity_status member:...`.
Discord buttons/forms are related controls, not additional slash commands.

Related guides: [authorization](discord-and-authorization.md),
[community workflows](community-workflows.md),
[vehicle transfers](vehicle-inventory-and-garage.md),
[native contract contributions](contract-contributions.md),
[farm finance](farm-finance-telemetry.md),
[bank finance validation](bank-finance-validation.md), and
[deployment](deploy-runbook.md).
