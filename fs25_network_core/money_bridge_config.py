"""Explicitly enable or disable the receipt-gated FS25 wallet bridge."""
import argparse

from .config import load_local_environment
from .database import Database
from .server_registry import ServerRegistry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="registered server_key")
    state = parser.add_mutually_exclusive_group(required=True)
    state.add_argument("--enable", action="store_true",
                       help="enable only after live native mutation/readback validation")
    state.add_argument("--disable", action="store_true")
    args = parser.parse_args(argv)
    load_local_environment()
    registry = ServerRegistry(Database())
    record = registry.configure_money_bridge(args.server, args.enable)
    print("FS25 money bridge {} for server={}".format(
        "enabled" if record.get("fs25_money_bridge_enabled") else "disabled", args.server))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
