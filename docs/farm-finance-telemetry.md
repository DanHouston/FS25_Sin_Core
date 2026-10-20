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

`game_day` is the native `environment.currentDay` counter, not a day of the
month. Central accepts its original integer value from 0 through 2147483647;
days 32 and later must not reject otherwise valid observations. The amount,
balance delta, farm, source sequence, period/year/time and world validation
remain enforced. No telemetry replay changes native farm money or SiN balances.

## Recover day-limit rejections

Older Central builds rejected entire batches with `game_day is invalid` once
the game day counter exceeded 31. Their Agent files remain quarantined as
`events/*.xml.failed` (or `.failed.N`). Deploy Central **v0.1.87 or later** and
restart `sin-central.service` before replaying them. Prefer **v0.1.88 or later**,
which also preserves raw-versus-hourly storage replay identity across the
day-counter upgrade. This validation fix needs
no game-mod change or FS25 restart. The separate Agent XML crash fix is in
v0.1.86 or later.

On the Windows Agent host, obtain `scripts/Restore-SiN-FinanceEvents.ps1` from
the released tag. Preview first, then apply:

```powershell
curl.exe -fL 'https://raw.githubusercontent.com/DanHouston/FS25_Sin_Core/v0.1.88/scripts/Restore-SiN-FinanceEvents.ps1' -o 'C:\SiN\Deploy\Restore-SiN-FinanceEvents.ps1'
if ($LASTEXITCODE -ne 0) { throw 'Recovery helper download failed' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\SiN\Deploy\Restore-SiN-FinanceEvents.ps1'
# After reviewing the listed candidates:
powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\SiN\Deploy\Restore-SiN-FinanceEvents.ps1' -Apply
```

The helper only selects well-formed quarantined `farm_finance_batch` files
containing an integer day above 31 within the corrected range. It leaves other
event types, malformed day values, and existing pending destination filenames
alone. Apply mode copies the original bytes into a unique
`recovery-archive/finance-day-*` directory outside the active queue, then
restores the original `.xml` filename without editing the envelope or row IDs.
The running Agent can deliver those files normally. Central validates each
batch and uses the existing world-scoped row IDs to prevent duplicate finance
observations if any rows were accepted previously. Fresh log entries should
show acceptance/removal; another rejection remains quarantined for review.
Recovered observations retain their native game date, but their Central
`observed_at` is the recovery ingestion time, so arrival-based daily reports can
include them on the recovery date. Previously accepted raw storage records are
not automatically rebucketed by this fix.

## Reports and capture coverage

The first read-only `/farm_report` now summarizes a verified farm manager's
current-world history for the last 1-30 real-world days (7 by default). It
shows native finance income/expense categories, physical NPC sale liters,
storage in/out movement, completed AI-job hours, and sampled vehicle use. It
is an ephemeral Discord response and never changes money or gameplay. This is
**not yet** the complete corporate summary or a replacement for the native
finance sheet. Collection began only when the telemetry build loaded. It does
not backfill historic daily finance totals or observe mods that directly call
`Farm:changeBalance`; those require reconciliation against the existing farm
balance snapshots. The separate [farm operations capture](farm-operations-telemetry.md)
adds physical sales, storage movements, AI jobs, and vehicle usage. Field
provenance, production recipe attribution, and per-player labor remain open.
The raw native money-type labels should be checked on a dedicated server before
using them as final report headings.

Server-mod candidate `0.2.2.23` passes the native Misc money type to the
banking bridge's existing `Farm:changeBalance` call. It records independent
cash and Misc readbacks on the operation receipt. The
[bank finance validation procedure](bank-finance-validation.md) includes the
read-only admin-client probe `sinBankFinance <farmId>` and the required live
deposit, withdrawal, multiplayer, and persistence checks. Separate bank rows
remain a follow-up. This direct farm path is still outside the passive
`Mission:addMoney` telemetry hook; operation receipts retain bank classification.

JiN also maintains one daily finance message in the configured market channel.
At or after 00:10 America/New_York time, it sums the previous completed local
calendar day's positive and negative native farm-money changes across all
eligible servers' active saves/worlds. The message reports server-wide inflows,
outflows, and net observed cash change; it is **not** net operating profit or
crop-sales revenue, because loans and other native money categories can appear.
One Mongo-backed message ID is kept per date, and late-arriving observations
edit the existing message. A restart scans the channel for the dated card
before sending, preventing ordinary restart duplication. Dates use Central's
`observed_at` ingestion timestamp, not accelerated in-game calendar days.

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
