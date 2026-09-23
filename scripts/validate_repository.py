"""Fast fail-first validation for executable source and deterministic config."""

from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sys
import time
from pathlib import Path

from defusedxml import ElementTree

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fs25_network_core.lua_validation import validate_fs25_lua_source
from fs25_network_core.release_validation import REQUIRED_SCENARIOS


class RepositoryValidationError(ValueError):
    """A source or deterministic configuration check failed."""


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RepositoryValidationError(f"invalid JSON: {path.relative_to(ROOT)}") from error
    if not isinstance(value, dict):
        raise RepositoryValidationError(f"JSON object required: {path.relative_to(ROOT)}")
    return value


def _numeric_id(value, label: str) -> None:
    if not str(value).isdigit() or int(value) <= 0:
        raise RepositoryValidationError(f"{label} must be a positive numeric Discord ID")


def _validate_discord_config(path: Path) -> None:
    config = _load_json(path)
    _numeric_id(config.get("guild_id"), "discord.guild_id")
    roles = config.get("roles")
    if not isinstance(roles, dict):
        raise RepositoryValidationError("discord.roles must be an object")
    _numeric_id(roles.get("sin_member"), "discord.roles.sin_member")
    channels = config.get("channels")
    required = {"staff", "operations", "sin_apply", "audit_log", "jobs"}
    if not isinstance(channels, dict) or not required.issubset(channels):
        raise RepositoryValidationError("discord.channels is missing a required destination")
    if "server_chat" in channels:
        raise RepositoryValidationError("discord.channels.server_chat is obsolete; use Central server records")
    retired = {"bank", "link_account", "sin_applications", "farm_approvals", "bank_reconciliation"}
    if retired.intersection(channels):
        raise RepositoryValidationError("discord.channels contains a retired destination")
    for name, value in channels.items():
        _numeric_id(value, f"discord.channels.{name}")


def _validate_server_config(path: Path) -> None:
    config = _load_json(path)
    for server_key, record in config.items():
        if not isinstance(record, dict) or not str(record.get("save_id", "")).strip():
            raise RepositoryValidationError(f"{path.name}: {server_key} is missing save_id")


def _validate_live_manifest(path: Path) -> None:
    manifest = _load_json(path)
    if manifest.get("manifest_schema") != "sin.live-validation/1":
        raise RepositoryValidationError("live-validation-manifest has an invalid schema")
    if manifest.get("required_scenarios") != REQUIRED_SCENARIOS:
        raise RepositoryValidationError("live-validation-manifest required scenarios are invalid")
    if not isinstance(manifest.get("live_validation_required"), list):
        raise RepositoryValidationError("live-validation-manifest live gates are missing")


def _validate_mod_descriptor(root: Path) -> None:
    mod_root = root / "mods" / "FS25_SiN_Server"
    try:
        descriptor = ElementTree.parse(mod_root / "modDesc.xml").getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise RepositoryValidationError("FS25_SiN_Server/modDesc.xml is invalid") from error
    if descriptor.get("descVersion") != "92":
        raise RepositoryValidationError("FS25_SiN_Server/modDesc.xml has an unexpected descVersion")
    source_names = [node.get("filename") for node in descriptor.findall("./extraSourceFiles/sourceFile")]
    icon_name = descriptor.findtext("iconFilename")
    if not icon_name or Path(icon_name).name != icon_name or not (mod_root / icon_name).is_file():
        raise RepositoryValidationError("FS25_SiN_Server/modDesc.xml has an invalid icon")
    for name in source_names:
        if not name or Path(name).name == name or (mod_root / name).is_file():
            # Root-level and nested source names are both valid; traversal is not.
            if not name or Path(name).is_absolute() or ".." in Path(name).parts or not (mod_root / name).is_file():
                raise RepositoryValidationError(f"FS25_SiN_Server source is missing or unsafe: {name}")
        else:
            raise RepositoryValidationError(f"FS25_SiN_Server source is missing: {name}")


def _validate_python_sources(root: Path) -> int:
    files = sorted(path for base in (root / "fs25_network_core", root / "tests", root / "scripts")
                   for path in base.rglob("*.py"))
    for path in files:
        try:
            compile(path.read_bytes(), str(path), "exec")
        except (SyntaxError, UnicodeDecodeError) as error:
            raise RepositoryValidationError(f"Python syntax failure: {path.relative_to(root)}") from error
    return len(files)


def _validate_imports() -> int:
    package = importlib.import_module("fs25_network_core")
    modules = sorted(module.name for module in pkgutil.iter_modules(package.__path__, package.__name__ + "."))
    for name in modules:
        importlib.import_module(name)
    return len(modules)


def _validate_lua_sources(root: Path) -> int:
    files = sorted((root / "mods" / "FS25_SiN_Server").rglob("*.lua"))
    for path in files:
        validate_fs25_lua_source(path.read_bytes(), str(path.relative_to(root)))
    return len(files)


def validate_repository(root: str | Path = ROOT, *, import_modules: bool = True) -> dict:
    root = Path(root)
    started = time.perf_counter()
    python_files = _validate_python_sources(root)
    imported_modules = _validate_imports() if import_modules else 0
    lua_files = _validate_lua_sources(root)
    _validate_discord_config(root / "discord.json")
    _validate_server_config(root / "servers.json")
    _validate_server_config(root / "servers.example.json")
    _validate_live_manifest(root / "docs" / "live-validation-manifest.json")
    _validate_mod_descriptor(root)
    return {
        "python_files": python_files,
        "imported_modules": imported_modules,
        "lua_files": lua_files,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--no-imports", action="store_true")
    args = parser.parse_args()
    try:
        result = validate_repository(args.root, import_modules=not args.no_imports)
    except RepositoryValidationError as error:
        parser.exit(1, f"static validation failed: {error}\n")
    print("static validation passed: " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

