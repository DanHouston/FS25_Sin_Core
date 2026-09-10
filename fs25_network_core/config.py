"""Load private local configuration without logging credential values."""
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_local_environment(root=PROJECT_ROOT):
    # Explicit process environment wins; Atlas settings precede general .env.
    # Disable interpolation so literal ${...} in secrets is preserved.
    for filename in ("atlas-credentials.env", ".env"):
        load_dotenv(Path(root) / filename, override=False, interpolate=False, encoding="utf-8-sig")


def required_setting(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing {name}. Set it in the process environment or a private .env file in the repository root.")
    return value
