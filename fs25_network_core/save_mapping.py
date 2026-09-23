"""Inspect or explicitly associate a physical FS25 save ID with a SiN save."""
import argparse
import json

from .config import load_local_environment
from .database import Database
from .server_registry import ServerRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--save", help="canonical logical save_key")
    parser.add_argument("--fs25-save-id", help="physical FS25 save ID to associate")
    parser.add_argument("--expected-current-fs25-save-id",
                        help="require this existing ID before changing a mapping")
    parser.add_argument("--show", action="store_true", help="show all mappings without changing them")
    args = parser.parse_args()
    if not args.show and (not args.save or args.fs25_save_id is None):
        parser.error("--save and --fs25-save-id are required unless --show is used")

    load_local_environment()
    database = Database()
    registry = ServerRegistry(database)
    if args.show:
        rows = list(database.db.sin_saves.find({"server_key": args.server}).sort("save_key", 1))
        print(json.dumps(rows, default=str, sort_keys=True, indent=2))
        return 0

    result = registry.configure_save(args.server, args.save, args.fs25_save_id,
                                     expected_fs25_save_id=args.expected_current_fs25_save_id)
    print("save mapping configured server=%s save_key=%s fs25_save_id=%s" %
          (args.server, args.save, result.get("fs25_save_id")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
