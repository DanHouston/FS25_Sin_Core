"""Fail-fast validation for a built SiN release directory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fs25_network_core.release_validation import ReleaseValidationError, validate_release_directory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    try:
        manifest = validate_release_directory(
            args.release_dir, expected_version=args.version, require_clean=args.require_clean)
    except ReleaseValidationError as error:
        parser.exit(1, f"release validation failed: {error}\n")
    print(f"release validation passed: {manifest['version']} commit={manifest['git_commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
