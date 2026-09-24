"""Perform the one-time, evidence-backed migration from a pre-marker world.

The default is a read-only plan.  ``--apply`` is required to create current
world projections; ``--reconcile`` additionally runs the normal Agent-poll
repair path so manager and shared-contractor operations are receipt-gated in
the target generation.  This is intentionally not a general farm-adoption
command.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fs25_network_core.database import Database
from fs25_network_core.farm_lifecycle import FarmLifecycle


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-key", required=True)
    parser.add_argument("--save-key", required=True)
    parser.add_argument("--source-world-id", required=True,
                        help="historical pre-persistence world ID")
    parser.add_argument("--target-world-id", required=True,
                        help="currently active world ID")
    parser.add_argument("--farm-id", required=True, type=int)
    parser.add_argument("--farm-name", required=True)
    parser.add_argument("--farmland-id", required=True, type=int)
    parser.add_argument("--discord-id", required=True)
    parser.add_argument("--unique-user-id", required=True)
    parser.add_argument("--operator-id", help="required with --apply")
    parser.add_argument("--apply", action="store_true",
                        help="write the validated current-world projection")
    parser.add_argument("--reconcile", action="store_true",
                        help="after apply, queue normal current-world authority repairs")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.reconcile and not args.apply:
        _parser().error("--reconcile requires --apply")
    if args.apply and not str(args.operator_id or "").strip():
        _parser().error("--operator-id is required with --apply")

    database = Database()
    lifecycle = FarmLifecycle(database)
    values = dict(
        server_key=args.server_key,
        save_key=args.save_key,
        source_world_id=args.source_world_id,
        target_world_id=args.target_world_id,
        farm_id=args.farm_id,
        farm_name=args.farm_name,
        farmland_id=args.farmland_id,
        discord_id=args.discord_id,
        unique_user_id=args.unique_user_id,
    )
    if not args.apply:
        plan = lifecycle.plan_same_physical_world_migration(**values)
        print(json.dumps({"mode": "dry-run", "status": "validated", "plan": plan},
                         default=str, sort_keys=True, indent=2))
        return 0

    # Normal service startup creates the indexes used by the transaction.  Do
    # this only for an explicit write; a dry-run remains read-only.
    database.initialize()
    result = lifecycle.migrate_same_physical_world(
        **values, operator_id=args.operator_id)
    output = {"mode": "apply", **result}
    if args.reconcile:
        output["reconciliation_operations"] = lifecycle.operations_for(
            args.server_key, args.save_key, args.target_world_id)
        output["reconciliation"] = "queued through current-world receipt/readback gates"
    print(json.dumps(output, default=str, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
