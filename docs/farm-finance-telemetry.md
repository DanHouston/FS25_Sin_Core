# Farm finance telemetry: first capture milestone

`FS25_SiN_Server` observes the authoritative server's `Mission:addMoney` calls. It
does not alter the amount, native finance category, game accounting, or return
value. After each call it compares the native farm balance before and after, and
buffers only actual changes. A bounded batch of up to 128 changes is written to
the existing server mailbox at most once per second; the Agent forwards it to
Central. Each row has its own world-scoped event ID so retries cannot duplicate
the stored transaction. Central retains the signed amount, balances, native
money-type label, in-game date/time, source order, and ingestion timestamp in
`farm_finance_changes`. There is no per-transaction INFO logging.

This is **not yet** the complete corporate summary or a replacement for the
native finance sheet. It begins collecting only when this build loads. It does
not backfill historic daily finance totals or observe mods that directly call
`Farm:changeBalance`; those require reconciliation against the existing farm
balance snapshots. The separate [farm operations capture](farm-operations-telemetry.md)
adds physical sales, storage movements, AI jobs, and vehicle usage. Field
provenance, production recipe attribution, and per-player labor remain open.
The raw native money-type labels should be checked on a dedicated server before
using them as final report headings.

The in-game hook does no XML or network I/O in the transaction path. It keeps
at most 2,048 pending records and logs one warning if it overflows. A failed
mailbox write leaves the batch queued for retry; a pre-existing batch path is
not accepted as proof of a successful write. On map unload the server attempts
to drain pending batches. The Agent and Central must be deployed alongside the
game mod or the new event type will be quarantined by older builds.

Dedicated-server validation after deployment:

1. Confirm one `[SiN Finance] native money observation installed` line.
2. Complete a small crop sale and a shop purchase. Verify native balances and
   finance-screen amounts are unchanged from normal gameplay.
3. Confirm a `farm_finance_batch` mailbox event reaches the Agent and Central,
   then inspect the corresponding `farm_finance_changes` rows for farm, amount,
   native money type, game time, and `source_sequence`.
4. Retry one batch and verify the row count does not increase. Check a second
   client's farm balance remains consistent with the server.

The implementation follows GIANTS' native `addMoney` path, which GIANTS itself
uses for purchases and recurring property costs; it does not intercept or
replace those native operations.
