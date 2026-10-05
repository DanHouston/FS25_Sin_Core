# Community workflow boundaries

## Mod suggestions forum

`channels.mod_suggestions` identifies the existing Discord forum (currently
`1556486221092487219`); `DISCORD_MOD_SUGGESTIONS_CHANNEL_ID` can override it
for another deployment. On a new forum post, JiN applies the `Under Review`
tag without removing the author's other tags. If that tag does not exist, JiN
creates it as a moderator-only tag. JiN then reacts to the starter message with
both 🟢 and 🔴. Discord counts these two bot reactions in its visible totals,
so a member-only tally is `max(0, green - 1)` and `max(0, red - 1)`. The pair
contributes zero net preference; it does not make either displayed count zero.
Setup is idempotent across duplicate gateway events and restarts. JiN needs
View Channel, Read Message History, Add Reactions, Manage Threads, and, if it
must create the tag, Manage Channels. Missing permissions or a full five-tag
post are logged and do not silently claim the post was prepared.

The first central-only foundations for community features live in
`fs25_network_core/business_workflows.py`. They are intentionally separate
from farm membership and manager authority.

`/contract_create` has an eligible server selector with autocomplete. A caller
with exactly one eligible identity context is scoped automatically; multiple
contexts require an explicit server, whose configured save must also be
unambiguous. Work contracts are never network-wide: missing, stale, or
ambiguous server/save context fails closed before persistence.

## Chat

`chat_messages` stores sanitized, source-tagged messages with an idempotent
message ID. FS25-originated messages enter through the existing authenticated
`POST /api/server/events` route with `event_type=chat_message`; Central's
`ActivityOutbox` then publishes them to that server's configured Activity
channel. Ordinary member messages in that Activity channel become durable
`farm_operations` entries with `operation_type=chat_message`; the Discord
message ID is the idempotency key and the Lua adapter displays them with a
`[Discord]` sender prefix.

Only the configured Activity channel is bridged. Bot/system messages, malformed
messages, ambiguous channel mappings, stale worlds, and messages from another
server are ignored. The Lua capture hook ignores the Discord prefix and uses an
injection-depth guard, preventing echo loops. A runtime without the native
mission chat method returns `pending_validation`, never a false delivered
receipt. The native hook and injection still require the live FS25 proof below.
The Discord application must have the Message Content intent enabled because
the reverse direction consumes ordinary Activity-channel message content;
slash-command operation remains available independently of that intent.

The source audit did confirm the documented `ChatDisplay` client HUD class,
including its message-history/display state, but that is not a server-side
capture or remote injection contract. The project therefore does not treat
`ChatDisplay` as evidence that a dedicated-server relay is safe. See the
[GIANTS FS25 ChatDisplay reference](https://gdn.giants-software.com/documentation_scripting_fs25.php?category=1&class=16&version=script)
for the UI-side API that was inspected; a live runtime hook is still required
before enabling either bridge direction.

## Banking

Wallet changes use immutable `ledger_entries` plus a projected wallet balance.
Wallet-to-wallet payments use an idempotent transaction ID. Deposits and
withdrawals create durable game operations first; central credit or final
settlement requires an authenticated FS25 receipt. Game money mutation is not
claimed complete by queueing an operation.  This wallet bridge is deliberately
separate from farm provisioning: it does not grant starting cash, create loans,
or alter an existing farm's initial economy.  `/deposit` debits the
authenticated farm only after the deployed FS25 money adapter proves the native
balance change; `/withdraw` reserves wallet funds and credits that same farm
only after authoritative readback.  Invoice payments move value between SiN
wallets without mutating FS25 farm balances. `/pay player:<member> amount:<whole-number>
memo:<text>` immediately transfers available SiN bank funds between two
registered players in one MongoDB transaction. It rejects self-payments,
unlinked recipients, invalid memos, and insufficient funds; the payer receives
a private confirmation only after the recipient credit is read back in the
transaction. The confirmation includes a payment reference and the payer's
updated available balance, but does not reveal the recipient's total balance.
The recipient gets a best-effort private receipt with the memo and reference.
A blocked recipient DM does not undo the payment. Repeating the same Discord
interaction cannot debit twice or send a second receipt. `/pay` never queues
an FS25 money operation or changes either farm's in-game balance.
Deposit and withdrawal commands acknowledge submission in an ephemeral reply
visible only to the caller in the channel where they invoked the command. If
the game confirms within Discord's 15-minute interaction window, JiN edits
that same reply with the definitive outcome and balances. No bank DM is sent.
If confirmation arrives later or JiN restarts, `/balance` shows pending amounts
and the latest definitive outcome; the short-lived interaction token is never
stored in MongoDB. The FS25 balance in a success notice is the native receipt's
balance read-back, not an older server snapshot. A definitive failure says to
retry later; an uncertain or delayed operation remains reserved/queued and is
never falsely called failed.

`/equity` is an ephemeral, caller-only snapshot. It combines the caller's SiN
checking balance with the active, verified farm's native cash balance; the
farm's assets are shared by its members, not personally owned by the caller.
The FS25 server mod exports owned parcel prices from the live farmland manager,
native `getSellPrice()` values for owned equipment and structures, and the
farm ID attached to those objects. Leased vehicles and consumable inventory
(pallets, bales, big bags) are excluded. When a structure has no usable native
sell price, the mod uses 50% of its live purchase price and `/equity` discloses
the number of such estimates. Vehicle values are never estimated from catalog
prices. Missing or stale data, or even one unpriced owned item in a category,
is shown as unavailable rather than silently counted as zero; the total is
withheld until every component is available. Loans, crops, stored products,
and pending bank transfers are not included. Both the updated FS25 server mod
and Agent must be running, and the mod must export a new snapshot, before the
new asset fields can appear in Discord. The displayed game snapshot timestamp
is interpreted as UTC even when MongoDB returns a timezone-naive datetime.

## Contracts, invoices, and events

Contracts are separate `contracts` documents with participant checks and an
open/accepted/in-progress/completed/cancelled lifecycle. New contracts use
structured work type, numeric field list, and fixed/hourly compensation while
retaining the durable value/rate fields. A creator cannot accept their own
contract. When `channels.jobs` is configured, creation posts a persistent
Accept card; the button calls the same atomic central acceptance path and open
cards are restored after restart. Accepted cards render the persisted acceptor
identity and Discord timestamp from the contract record. JiN refreshes stored
job-card content in place during startup, so attribution is retained across
card refreshes and bot restarts. Compensation is rendered as a
currency amount (for example `Fixed - $1` or `Hourly - $250/hour`). Without
that channel configuration, the durable slash commands remain available.
Cancelled contracts remain as durable history cards. Their cards show the
cancellation identity and timestamp, and all obsolete controls are disabled.

The jobs channel is guild-level deployment configuration, not a server or
business-logic constant. The checked-in `discord.json` carries the current
deployment value; an operator may instead supply `DISCORD_JOBS_CHANNEL_ID` in
the bot environment. The bot never guesses or creates a channel. A contract
creator with exactly one eligible registered server/save is scoped there
automatically; multiple eligible identity contexts fail closed rather than
silently selecting a server. Every persisted work contract contains explicit
`scope: "server"`, `server_key`, and `save_key`.

Invoices are separate `invoices` documents and pay through the banking ledger
exactly once; an issuer cannot invoice the same Discord account. Community
events are `community_events` documents with scheduled/active/completed/
cancelled state and participant registration. When `channels.events` is
configured, creation posts a persistent Join/Leave board card and open cards
are restored after restart. Event input accepts an ISO-8601 timestamp with an
offset, or a local `YYYY-MM-DD HH:MM` value interpreted using the configured
`DISCORD_TIMEZONE` (IANA name, default `UTC`); all values are stored in UTC.
Discord renders them with per-user timestamp markup. Discord selectors use
friendly autocomplete labels while durable IDs remain internal.

## Transfers

`transfers` records vehicle/product requests. Source-farm manager authority is
required before a request is created; destination acceptance and a durable
operation receipt are required before completion. The current mod has no
unverified vehicle or storage mutation call: unsupported operations are not
reported as applied and remain a reconciliation concern.

## Durable operation rules

Permission-command XML is consumed only after the mod has produced its receipt;
the runtime now removes the exact command file and manifest with GIANTS'
`deleteFile` API. A `.failed` receipt remains auditable and is not deleted by
cleanup. This prevents old command XML from being rediscovered. The Agent
retains transient/authentication failures for retry, but quarantines permanent
400/404/422 receipt rejections as `.failed` so malformed or stale scope
evidence cannot be retried forever.

Game-facing value operations remain receipt-gated.  A queued operation is not
completion: the Agent writes it to the FS25 mailbox, the mod returns a durable
receipt, and the central API commits the business projection only after the
receipt is authenticated for the same server/save/world generation and matches
the queued operation ID.  Repeated identical receipts are safe; conflicting
terminal receipts are rejected and left for reconciliation.

Processed server events, activity outbox records, telemetry intervals, and
chat observations are scoped by `server_key` and canonical `save_key` where
the source identifier is only locally unique.  Database startup replaces the
known pre-scope unique indexes only when their exact legacy definitions are
found; it does not drop arbitrary indexes.

The central API and Agent are locally tested for retries, malformed mailbox
input, lost receipt responses, and duplicate delivery.  The FS25-side money,
vehicle-ownership, and storage/fill mutations remain `LIVE VALIDATION
REQUIRED` until a verified GIANTS runtime API and replication path is proven.

## Current Discord command surface

- Money: `/balance`, `/equity`, `/deposit`, `/withdraw`, `/pay` (withdrawal availability is
  server-configured).
- Contracts: `/contract_create`, `/contract_list`, `/contract_view`,
  `/contract_accept`, `/contract_complete`. Cancellation is performed from the
  posted contract card by its creator.
- Invoices: `/invoice_create`, `/invoice_list`, `/invoice_view`,
  `/invoice_pay`, `/invoice_cancel`.
- Community events: `/event_create`, `/event_list`, `/event_view`,
  `/event_join`, `/event_leave`, `/event_cancel`, `/event_complete`.
- Transfers: staff-only `/transfer_request`, `/transfer_list`,
  `/transfer_accept`, `/transfer_dispatch`.
- Diagnostics: `/activity_status` is self-service anywhere in the configured
  guild; the optional `member` lookup remains staff-only and channel-scoped.
  Server/runtime diagnostics remain available through the read-only FS25
  `sinSelfTest` and `sinPermissions` commands.

Bank deposits and withdrawals resolve the invoking registered identity's
authenticated server/save context and no longer expose a redundant server
selector. Ambiguous or unavailable identity context fails closed.
`/pay` is central-wallet-only, so it checks both linked player identities but
does not require either player to be online or select a game server/save.

Server/save selectors use the central registry and friendly display names;
internal keys remain the durable scope identifiers.  Operations that touch
the game are shown as queued until the authenticated Agent/mod receipt closes
them.

For local protocol testing, `MailboxOperationHarness` in
`fs25_network_core/protocol_harness.py` consumes command XML and can simulate
applied, duplicate-execution, failed, or lost-receipt outcomes.  It is a
developer test boundary only: it has no Mongo access and does not prove any
GIANTS runtime API.
