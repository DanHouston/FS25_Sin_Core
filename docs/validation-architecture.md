# Validation architecture

The v0.1.26 live-load failure exposed a boundary that the previous green
validation surface did not cover: Python tests, offline scenarios, and archive
shape checks all passed while the Lua payload was never parsed by an FS25
compatible parser. The repository now fails earlier and validates the bytes
that would actually be shipped.

## Validation layers

| Layer | Normal command | Proves | Deliberately cannot prove |
| --- | --- | --- | --- |
| Static/syntax | `python scripts/validate_repository.py` | Python source compiles, SiN Python modules import, every mod Lua source passes the FS25 Lua 5.1 compatibility gate, deterministic JSON/XML configuration is valid | GIANTS engine behavior, undocumented API availability, live credentials or services |
| Unit | `python -m unittest discover -s tests` (or focused modules) | Pure service, persistence, renderer, protocol, configuration, and source-contract behavior | Real FS25 parser/runtime, Discord/Mongo/FS25 network behavior |
| Contract | `python -m unittest tests.test_integration_campaign` | Adapter envelopes, authentication, receipts, scenario registry, and stable cross-component contracts | Live engine state and external delivery |
| Integration/scenario | `python scripts/run_integration_campaign.py --scenario <name> ...` | Deterministic mailbox → Agent → Central → persistence → presentation workflows and composed replacement scenarios | GIANTS authority, real Discord rendering, real Mongo durability |
| Packaged artifact | `python scripts/build_release.py ...` followed by `python scripts/validate_release.py <dir> --version <version> --require-clean` | Release manifest/checksums, archive structure, mod descriptor membership, ZIP CRCs, every Lua file inside `FS25_SiN_Server.zip`, and passed campaign evidence | The game loading the ZIP under a particular GIANTS build |
| Live/external | The procedures in `docs/live-validation.md` | Actual FS25 load, world marker, snapshots, map extraction, Discord delivery, Mongo durability, and authoritative runtime read-back | Nothing beyond the evidence collected in that live environment |

The CI and release workflows run the static layer before the test suite. The
release builder and the shared release validator both inspect the completed
FS25 archive; this is an intentional artifact boundary, not a source-only
check. `compileall` remains an explicit cheap release proof for compatibility
with existing operator procedures, while the static layer gives the faster
fail-first result.

## What the audit found

Before this change, the test portfolio had 296 tests and green offline
integration/package checks. `NetworkLocal.lua` contained Lua 5.2 `goto`/label
syntax, but no test or build step parsed Lua. The package checks verified that
`NetworkLocal.lua` existed and that its ZIP hash was recorded, not that the
file was accepted by the FS25 parser.

The expensive work is concentrated in the deliberately broad integration
tests and release build:

- `tests/test_integration_campaign.py` exercises named scenarios individually
  so each contract has a focused failure signal; the complete campaign also
  runs as a composed report.
- `tests/test_deployment.py` builds a real release directory and checks the
  artifact boundary. The build includes a campaign report, so this repeats
  some scenario execution by design to bind evidence to the exact package.
- World replacement, map, farm lifecycle, activity, authority, and bridge
  tests overlap in data but prove different generation or receipt boundaries;
  none was removed.
- Source-contract tests in `tests/test_registration.py` and byte-level archive
  tests prove different things and remain separate.
- The former CI inline archive assertions duplicated packaging knowledge. They
  now call the shared `release_validation` implementation, eliminating drift
  without reducing coverage.

The audit found no safe test deletion. The meaningful improvement is moving
cheap syntax/configuration and canonical artifact checks ahead of expensive
tests and release publication, not reducing the test count.

## Developer and release paths

Fast fail-first developer check:

```powershell
python scripts/validate_repository.py
```

Normal pre-commit path:

```powershell
python scripts/validate_repository.py
python -m unittest discover -s tests
python scripts/run_integration_campaign.py --output local-test/integration-campaign.json
python -m compileall -q fs25_network_core tests scripts
git diff --check
```

Release path:

```powershell
python scripts/build_release.py --version <version> --output release
python scripts/validate_release.py release --version <version> --require-clean
```

The tag workflow runs the same static check, complete suite, compile/diff
proof, build, and packaged-artifact validator before `gh release create`.

Authority receipts have their own fail-closed contract inside the contract and
integration layers. A permission job is not applied merely because a receipt
file exists or says `applied`: manager receipts must read back the matching
current farm and manager state; contractor/revocation receipts must read back
the matching `source_farm_id -> target_farm_id` native relationship; and any
unsupported role must provide a role-specific authoritative read-back. Pending,
failed, false, malformed, wrong-save, or wrong-generation receipts are retained
as bounded reconciliation evidence and are never committed as applied. The
Agent quarantines permanent HTTP rejection and malformed receipts so an old
Hobo/Courtright message cannot retry forever.

## Remaining live-only gaps

The static gate is intentionally not described as a GIANTS compiler. A real
FS25 load is still required for the target engine build, mod dependency
resolution, startup callbacks, world-generation marker persistence, map
geometry extraction, Discord attachment delivery, Mongo durability, and all
authoritative farm/land/authority operations. FS25 money, loans, teleport,
farm deletion, and asset mutation remain capability-gated and are not enabled
by this validation work.
