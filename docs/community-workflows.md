# Community workflow boundaries

The first central-only foundations for community features live in
`fs25_network_core/business_workflows.py`. They are intentionally separate
from farm membership and manager authority.

## Chat

`chat_messages` stores sanitized, source-tagged messages with an idempotent
message ID. FS25-originated messages enter through the existing authenticated
`POST /api/server/events` route with `event_type=chat_message`. Discord-originated
messages become durable `farm_operations` entries with
`operation_type=chat_message`.

The current generic mod has no verified GIANTS chat-capture hook, so ordinary
FS25 chat is not yet emitted to Discord. Discord-to-FS25 injection is also
capability-gated: no message is queued from the normal command while the
server-side adapter is unverified. Existing durable `pending_validation`
operations remain auditable for controlled adapter work, but are never treated
as delivery. Do not claim either direction is live until its runtime adapter is
proven independently.

## Banking

Wallet changes use immutable `ledger_entries` plus a projected wallet balance.
Wallet-to-wallet payments use an idempotent transaction ID. Deposits and
withdrawals create durable game operations first; central credit or final
settlement requires an authenticated FS25 receipt. Game money mutation is not
claimed complete by queueing an operation.

## Contracts, invoices, and events

Contracts are separate `contracts` documents with participant checks and an
open/accepted/in-progress/completed/cancelled lifecycle. New contracts use
structured work type, numeric field list, and fixed/hourly compensation while
retaining the durable value/rate fields. A creator cannot accept their own
contract. Invoices are separate `invoices` documents and pay through the
banking ledger exactly once. Community events are `community_events` documents
with scheduled/active/completed/cancelled state and participant registration.
Event input requires an explicit ISO-8601 timezone and is stored in UTC;
Discord renders it with per-user timestamp markup. Discord selectors use
friendly autocomplete labels while durable IDs remain internal.

## Transfers

`transfers` records vehicle/product requests. Source-farm manager authority is
required before a request is created; destination acceptance and a durable
operation receipt are required before completion. The current mod has no
unverified vehicle or storage mutation call: unsupported operations are not
reported as applied and remain a reconciliation concern.

## Durable operation rules

Game-facing value operations remain receipt-gated.  A queued operation is not
completion: the Agent writes it to the FS25 mailbox, the mod returns a durable
receipt, and the central API commits the business projection only after the
receipt is authenticated for the same server/save and matches the queued
operation ID.  Repeated identical receipts are safe; conflicting terminal
receipts are rejected and left for reconciliation.

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

- Money: `/balance`, `/deposit`, `/withdraw` (withdrawal availability is
  server-configured).
- Contracts: `/contract_create`, `/contract_list`, `/contract_view`,
  `/contract_accept`, `/contract_cancel`, `/contract_complete`.
- Invoices: `/invoice_create`, `/invoice_list`, `/invoice_view`,
  `/invoice_pay`, `/invoice_cancel`.
- Community events: `/event_create`, `/event_list`, `/event_view`,
  `/event_join`, `/event_leave`, `/event_cancel`, `/event_complete`.
- Transfers: staff-only `/transfer_request`, `/transfer_list`,
  `/transfer_accept`, `/transfer_dispatch`.
- Diagnostics: staff-only `/activity_status`; server/runtime diagnostics remain
  available through the read-only FS25 `sinSelfTest` and `sinPermissions`
  commands.

Server/save selectors use the central registry and friendly display names;
internal keys remain the durable scope identifiers.  Operations that touch
the game are shown as queued until the authenticated Agent/mod receipt closes
them.

For local protocol testing, `MailboxOperationHarness` in
`fs25_network_core/protocol_harness.py` consumes command XML and can simulate
applied, duplicate-execution, failed, or lost-receipt outcomes.  It is a
developer test boundary only: it has no Mongo access and does not prove any
GIANTS runtime API.
