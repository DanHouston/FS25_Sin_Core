# Bank transfers on the in-game finance sheet

The user selected the native **Misc** category for the first implementation.
Candidate server mod `0.2.2.23` passes `MoneyType.OTHER` to the existing
`Farm:changeBalance` call for deposits and withdrawals. A deposit into the
SiN wallet records a negative farm amount; a withdrawal into the farm records
a positive amount. The operator confirmed the basic in-game Misc display on
2026-10-18; persistence, rollover, and additional-client checks remain pending.

Separate **SiN Bank Deposits** and **SiN Bank Withdrawals** rows are a desired
follow-up. They require proving report rendering, saving/loading, rollover,
and multiplayer synchronization before enabling custom finance fields.

## Misc booking and receipts

The adapter checks that the native money type uses `statistic="other"` and
that the farm has a finite current Misc value before attempting a mutation.
It allocates the receipt before changing cash. The existing server/world and
receipt replay checks remain in the command dispatcher.

One native balance call receives the amount and money type. Afterward the
adapter independently reads the balance and Misc field and records:

- `finance_category="other"`
- `finance_before` and `finance_after`
- `finance_readback`, indicating whether the Misc delta matched the transfer

The server logs `[SiN Bank Finance]` for an attempted transfer. Successful
booking should show both `financeReadback=true` and `balanceReadback=true`.
The read-only `sinBankFinance <farmId>` command is also retained.

A verified cash transfer still settles against the existing cash receipt
criteria if finance booking cannot be verified. Its receipt and warning
explicitly report the missing history verification. No second money or
statistics writer is called to repair history, and no automatic replay or
historical backfill is performed. This prevents an accounting diagnostic
from moving the same cash a second time.

If a native call throws after changing cash, the adapter keeps the result in
`pending_validation`. A thrown call is classified as definitively not applied
only when readback proves both balance and Misc are unchanged.

## First step: inspect the deployed finance schema

The candidate server mod provides the read-only client console command:

```text
sinBankFinance 2
```

Use the farm's native ID (2 for Repton Does). Install the same candidate ZIP
on the dedicated server and client, log into the game as server admin, open
the in-game Finances sheet so its controls are initialized, then run the
command in the client's console. It records local finance categories and
report structures and requests the authoritative farm/statistics structures
from the server. The server checks the requesting connection's admin status;
its response is written back into that client's log. A server console is not
required. Look for `[SiN Finance Probe]` entries in `log.txt`.

The probe reads tables, function availability, and the existing balance. It
does not call balance/statistics writers, register money types, replay old
transactions, or perform a deposit or withdrawal. The command itself remains
read-only in the Misc-booking candidate.

## Separate bank rows: follow-up

The first implementation uses Misc. Add separate categories only after their
history, report rows, and multiplayer synchronization are validated through
the deployed APIs. The desired future rows are:

| Row | Action | Farm cash |
| --- | --- | --- |
| SiN Bank Deposits | Farm to SiN wallet | Negative |
| SiN Bank Withdrawals | SiN wallet to farm | Positive |

The implementation must retain the banking bridge's operation/receipt replay
protection and authoritative balance readback. Each transfer must change money
once and record history once. Uncertain outcomes must remain in reconciliation.

## Controlled validation of the Misc candidate

1. Install the same `0.2.2.23` candidate on server and clients, reload the game,
   and confirm the Agent is running. Record the farm balance, SiN wallet
   balance, and current finance-sheet Misc value. Keep the game in the same
   month for the two operations.
2. Run `/deposit amount:100`; wait for confirmed completion. The farm must fall
   by 100, the wallet rise by 100, and Misc decrease by 100 once. Check the
   server's `[SiN Bank Finance]` line for both readbacks being true.
3. Run `/withdraw amount:100`; wait for confirmed completion. The farm and wallet
   must return to their starting balances and Misc increase by 100 once.
   Inspect each operation separately: equal transfers net to zero in Misc.
4. Check the same farm's finance sheet from a second authorized client, then
   reconnect. Confirm matching values and no extra transactions.
5. Save and restart the server; confirm the period's Misc total and balances
   persist. Misc displays a net period total, not individual transfer rows.
6. Check a subsequent month rollover preserves the prior month's entries and
   starts the new period correctly. In an isolated validation save, confirm
   rejected/replayed commands create no extra balance or finance changes.

These are cash transfers. Any later operating-profit calculation should keep
their classification distinct from farm revenue and operating costs.

### Operator result: 2026-10-18

The user reported that a deposit showed **-100** in Misc and the subsequent
withdrawal brought Misc back to **0**. This confirms the expected direction
and visible netting for the two live operations in the candidate-validation
flow. The report did not include wallet/farm balance snapshots, receipt
readbacks, a second client, reconnect, save/reload, or month-rollover evidence;
those checks are still open.

For persistence validation, use a nonzero Misc change that can be distinguished
from the starting value and record it before saving. A zero total after equal
transfers cannot by itself show that bank booking survived a reload.

## Observed native schema: 2026-10-17

The `0.2.2.21` probe returned both client and authenticated server results for
farm 2. It confirmed:

- `MoneyType.OTHER` has `id=1`, `statistic="other"`, and `title="finance_other"`.
  Native money types use `statistic`; the initial probe's `statsName=nil` output
  did not mean categories were missing.
- `farm.stats.finances` contains the current period's finance fields, including
  `other`. `farm.stats.financesHistory` contained four prior periods.
- `FarmStats` exposes `changeFinanceStats`, `archiveFinances`, `saveToXMLFile`,
  and `loadFromXMLFile`. Their presence alone does not establish custom-field
  registration, serialization, or multiplayer behavior.
- The live report is `g_inGameMenu.pageStatistics` with a `financesList`;
  `InGameMenuFinancesFrame` is absent in this FS25 build.

The first output reached its size limit while expanding unrelated GUI layout
tables and did not inspect `FinanceStats`. Probe version 2 in mod `0.2.2.22`
prioritizes that class's registry and methods, current/past finance fields,
the active finance page, and list data instead of layout coordinates.

### Probe version 2: 14:44 on 2026-10-17

The second output completed without reaching the overall size limit and
confirmed the following on both client and server:

- `FinanceStats.statNames` and `statNameToIndex` contain 33 fields. `other`
  is index 32 and its native display label is `Misc`.
- `FinanceStats.statNamesI18n` maps field names to report labels. There is no
  custom-category registration function among the observed class methods.
- `MoneyType.TRANSFER` is ID 38 and also uses `statistic="other"`. A separate
  money-type title alone therefore does not create a separate report row.
- Farm 2's current `other` value is zero. Its current balance was
  `200358.921875`, and its four history entries use the same 33-field schema.
- The live UI uses `InGameMenuStatisticsFrame` methods including
  `updateFinances`, `setPastDayFinances`, and `populateCellForItemInSection`.
  `financesList.dataSource` and its delegate both point to the statistics page.
  The probe saw selected category 1 and an empty list-section array; this does
  not establish the row rendering or synchronization behavior of new fields.

The existing Misc field provides a concrete native target for booking. Separate
bank rows would require extending the finance schema and establishing report,
serialization, load, rollover, and multiplayer behavior. The presence of these
tables is not proof that extending them is sufficient.

This probe established the schema used by the later `0.2.2.23` Misc candidate.
The first live transfer must verify that the native call books the expected
Misc delta, followed by the persistence and multiplayer checks above. A final
zero after equal transfers alone is not evidence that either was booked.
