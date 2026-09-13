"""Central CLI for configuring a save-scoped SiN clock policy."""
import argparse

from .clock_policy import validate_policy
from .config import load_local_environment
from .database import Database
from .server_registry import ServerRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--save", required=True, help="canonical save_key")
    parser.add_argument("--fs25-save-id", help="explicitly create/update the server/save mapping")
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--offset-minutes", type=int, required=True)
    parser.add_argument("--normal-scale", type=float, default=1)
    parser.add_argument("--catchup-scale", type=float, default=15)
    parser.add_argument("--fast-catchup-threshold", type=float, default=60)
    parser.add_argument("--fast-catchup-scale", type=float, default=360)
    parser.add_argument("--ahead-scale", type=float, default=0)
    parser.add_argument("--tolerance", type=float, default=2)
    parser.add_argument("--hard-threshold", type=float, default=180)
    parser.add_argument("--check-interval", type=int, default=60)
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--hard-resync", action="store_true")
    args = parser.parse_args()
    policy = validate_policy({"enabled": args.enabled, "timezone": args.timezone,
                              "offset_minutes": args.offset_minutes, "normal_time_scale": args.normal_scale,
                              "catchup_time_scale": args.catchup_scale, "ahead_time_scale": args.ahead_scale,
                              "fast_catchup_threshold_minutes": args.fast_catchup_threshold,
                              "fast_catchup_time_scale": args.fast_catchup_scale,
                              "tolerance_minutes": args.tolerance,
                              "hard_resync_threshold_minutes": args.hard_threshold,
                              "hard_resync_enabled": args.hard_resync,
                              "check_interval_seconds": args.check_interval})
    load_local_environment()
    database = Database()
    registry = ServerRegistry(database)
    if args.fs25_save_id is not None:
        registry.configure_save(args.server, args.save, args.fs25_save_id)
    registry.configure_clock_policy(args.server, args.save, policy)
    print(f"Clock policy configured for server={args.server} save_key={args.save}")


if __name__ == "__main__":
    main()
