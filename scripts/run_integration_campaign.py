"""Run a named, deterministic, offline SiN integration scenario."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fs25_network_core.integration_campaign import (  # noqa: E402
    SCENARIOS, SEMANTIC_SCENARIOS, AUTHORITATIVE_SCENARIOS, run_named_scenario,
)


def run(output, scenario="offline-full"):
    """Run *scenario* and write its secret-free report to *output*."""
    return run_named_scenario(scenario, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=AUTHORITATIVE_SCENARIOS.names() + SCENARIOS.names() + tuple(SEMANTIC_SCENARIOS), default="offline-full",
                        help="scenario to execute (authoritative names are recommended)")
    parser.add_argument("--list-scenarios", action="store_true",
                        help="list scenario names and descriptions, then exit")
    parser.add_argument("--output", type=Path,
                        default=Path("local-test") / "integration-campaign.json")
    args = parser.parse_args()
    if args.list_scenarios:
        print(json.dumps({"authoritative": AUTHORITATIVE_SCENARIOS.describe(),
                          "semantic": {name: value.description for name, value in SEMANTIC_SCENARIOS.items()},
                          "boundary": SCENARIOS.describe()}, indent=2))
        return 0
    try:
        report = run(args.output, args.scenario)
    except (AssertionError, OSError, ValueError):
        parser.exit(1, "SiN integration scenario failed\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
