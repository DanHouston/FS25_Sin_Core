# Hobo save backup and restore runbook

This runbook is the safety gate for any future FS25 economic mutation when a
disposable save is unavailable.  It is a filesystem backup/restore procedure;
it does not modify MongoDB and it does not make a financial operation safe by
itself.  Do not run a mutation until the backup has been created, its hashes
recorded, and a restore rehearsal has been reviewed by an operator.

## Scope and paths

The examples below use the current Hobo v1 physical save (slot 4):

```powershell
$fs25Root = 'C:\Users\SiNAdmin\Documents\My Games\FarmingSimulator2025'
$savePath = Join-Path $fs25Root 'savegame4'
$mailboxPath = Join-Path $fs25Root 'modSettings\FS25_SiN_Server'
$backupRoot = 'C:\SiN\Backups\Hobo'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupPath = Join-Path $backupRoot $stamp
```

If the physical slot or canonical mailbox changes, substitute the actual
configured paths.  Never infer a slot from a Discord channel or a display
name.

## Create a consistent backup (read-only with respect to the live save)

1. Let FS25 finish a normal save, then stop the dedicated server using its
   normal operator/service control.  Stop the SiN Agent as well so it cannot
   consume or create mailbox files while the copy is in progress.  Confirm no
   FS25 or Agent process still has either path open; do not force-kill an
   unknown process.
2. Confirm the save-backed marker exists and record its world identity:

   ```powershell
   $marker = Join-Path $savePath 'FS25_SiN_Server_world.xml'
   if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
       throw "Missing save-backed world marker: $marker"
   }
   Get-Content -LiteralPath $marker -Raw
   ```

3. Copy the complete save and canonical mailbox into a new, timestamped
   directory.  `robocopy` exit codes 0--7 are successful copy outcomes; a
   code of 8 or greater is a failed/incomplete backup.

   ```powershell
   New-Item -ItemType Directory -Force -Path $backupPath | Out-Null
   robocopy $savePath (Join-Path $backupPath 'savegame4') /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ
   if ($LASTEXITCODE -ge 8) { throw "save backup failed: $LASTEXITCODE" }
   robocopy $mailboxPath (Join-Path $backupPath 'FS25_SiN_Server') /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ
   if ($LASTEXITCODE -ge 8) { throw "mailbox backup failed: $LASTEXITCODE" }
   ```

4. Record hashes and a directory listing outside the copied trees.  Keep this
   output with the backup; it proves what was restored later.

   ```powershell
   Get-ChildItem -LiteralPath $backupPath -File -Recurse |
     Get-FileHash -Algorithm SHA256 |
     Export-Csv -NoTypeInformation -LiteralPath (Join-Path $backupPath 'SHA256.csv')
   Get-ChildItem -LiteralPath $backupPath -Force -Recurse |
     Select-Object FullName,Length,LastWriteTimeUtc |
     Export-Csv -NoTypeInformation -LiteralPath (Join-Path $backupPath 'FILES.csv')
   ```

5. Separately record the current Central scope with the repository's existing
   read-only Mongo inspection procedure: server `sin-fs25-01`, save
   `sin-fs25-hobo-v1`, physical save ID `4`, active `world_id`, runtime
   generation/session, current farms/farmland, pending operations, and
   pending receipts.  Do not use an update/insert/delete command for this
   inventory.  A filesystem restore cannot roll MongoDB backward.

## Mutation gate

The current economy capability probe is read-only.  It proves method
availability and observations only; it does not prove a safe mutation,
replication, persistence, idempotency, or loan setter.  Until those contracts
are separately validated, do not issue a cash/loan mutation against Hobo.

If a mutation is eventually approved, perform exactly one operation after the
backup above, record the before value, operation ID (if any), receipt, and
authoritative readback, then save and restart FS25 before declaring it valid.
Do not submit a Central operation or receipt unless the game readback proves
the intended result.  This prevents a restore from leaving a durable Central
operation that no longer matches the save.

## Restore procedure (only after FS25 is stopped)

Restoration is deliberately conservative and recoverable.  It never deletes a
directory in place:

```powershell
$restore = 'C:\SiN\Backups\Hobo\<timestamp>'
$quarantine = "C:\SiN\Backups\Hobo\quarantine-$(Get-Date -Format yyyyMMdd-HHmmss)"
New-Item -ItemType Directory -Force -Path $quarantine | Out-Null
Move-Item -LiteralPath $savePath -Destination (Join-Path $quarantine 'savegame4')
Move-Item -LiteralPath $mailboxPath -Destination (Join-Path $quarantine 'FS25_SiN_Server')
Copy-Item -LiteralPath (Join-Path $restore 'savegame4') -Destination $fs25Root -Recurse
Copy-Item -LiteralPath (Join-Path $restore 'FS25_SiN_Server') -Destination (Join-Path $fs25Root 'modSettings') -Recurse
```

Before starting anything, compare the restored files with `SHA256.csv` and
confirm the restored world marker has the pre-test `world_id`.  Start FS25,
allow one normal save, and verify the marker remains unchanged.  Only after
that proof should the Agent be restarted and its mailbox allowed to resume.

If the mutation produced a Central operation, receipt, or bank ledger entry,
stop and reconcile with the operator before restarting the Agent.  Do not
manually patch MongoDB or replay a receipt to make the restored save appear
consistent.  A Central backup/restore procedure must be established separately
before any operation that writes durable Central financial state.

## Required post-restore proof

- the restored save is still physical slot 4 and its marker has the original
  Hobo `world_id`;
- a restart creates a new runtime generation/session but does not create a new
  world identity;
- the first snapshot is accepted for that same world and no stale operation or
  receipt is executed;
- farm, farmland, money, and loan readbacks match the pre-test inventory;
- Agent event/receipt processing resumes only after those checks pass.

The existing deployment updater's rollback restores Agent code and the mod ZIP
only.  It is not a substitute for this save/mailbox backup when testing
gameplay mutations.
