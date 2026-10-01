"""Validation of the actual release directory and packaged FS25 artifact."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree

from fs25_network_core.lua_validation import validate_fs25_lua_source
from fs25_network_core.crop_settings import CropPolicyError, parse_policy


class ReleaseValidationError(ValueError):
    """The release directory is incomplete, inconsistent, or unsafe."""


RELEASE_ASSETS = frozenset({
    "build-manifest.json",
    "FS25_SiN_Server.zip",
    "SiN_FS25_Crop_Settings.zip",
    "SiN_FS25_Contracts.zip",
    "SiN_FS25_Policy.zip",
    "integration-campaign.json",
    "live-validation-manifest.json",
    "Restart-SiN-Agent.ps1",
    "Publish-SiN-Modpack.ps1",
    "SHA256SUMS.txt",
    "sin-agent.zip",
    "Update-SiN-Client.ps1",
    "Update-SiN.ps1",
})
CHECKSUM_ASSETS = frozenset({
    "sin-agent.zip",
    "FS25_SiN_Server.zip",
    "SiN_FS25_Crop_Settings.zip",
    "SiN_FS25_Contracts.zip",
    "SiN_FS25_Policy.zip",
    "Update-SiN.ps1",
    "Update-SiN-Client.ps1",
    "Restart-SiN-Agent.ps1",
    "Publish-SiN-Modpack.ps1",
    "integration-campaign.json",
    "live-validation-manifest.json",
})
REQUIRED_SCENARIOS = [
    "registration",
    "control_plane_backlog",
    "activity_disconnect",
    "map_contract",
    "contract_scope",
    "authority_regression",
]
_SHA_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+([^\s]+)$")
_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReleaseValidationError(f"invalid JSON: {path.name}") from error
    if not isinstance(value, dict):
        raise ReleaseValidationError(f"JSON object required: {path.name}")
    return value


def _safe_archive_names(archive: ZipFile, name: str) -> list[str]:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ReleaseValidationError(f"{name} contains duplicate archive members")
    for member in names:
        path = Path(member)
        if path.is_absolute() or ".." in path.parts or "\\" in member:
            raise ReleaseValidationError(f"{name} contains unsafe member: {member}")
    if archive.testzip() is not None:
        raise ReleaseValidationError(f"{name} failed ZIP CRC validation")
    return names


def _validate_server_archive(path: Path) -> None:
    try:
        with ZipFile(path) as archive:
            names = _safe_archive_names(archive, path.name)
            if "modDesc.xml" not in names or "NetworkLocal.lua" not in names:
                raise ReleaseValidationError("FS25_SiN_Server.zip is missing required root files")
            try:
                descriptor = ElementTree.fromstring(archive.read("modDesc.xml"))
            except (ValueError, ElementTree.ParseError) as error:
                raise ReleaseValidationError("FS25_SiN_Server.zip has invalid modDesc.xml") from error
            source_names = [node.get("filename") for node in descriptor.findall("./extraSourceFiles/sourceFile")]
            icon_name = descriptor.findtext("iconFilename")
            if not icon_name or Path(icon_name).name != icon_name:
                raise ReleaseValidationError("FS25_SiN_Server.zip has an invalid iconFilename")
            expected = {"modDesc.xml", icon_name, *source_names}
            if None in expected or set(names) != expected:
                raise ReleaseValidationError("FS25_SiN_Server.zip does not match modDesc.xml sources")
            for member in names:
                if member.lower().endswith(".lua"):
                    validate_fs25_lua_source(archive.read(member), member)
    except BadZipFile as error:
        raise ReleaseValidationError("FS25_SiN_Server.zip is not a valid ZIP archive") from error


def _validate_agent_archive(path: Path) -> None:
    expected = {"fs25_network_core/__init__.py", "fs25_network_core/agent.py"}
    try:
        with ZipFile(path) as archive:
            names = set(_safe_archive_names(archive, path.name))
    except BadZipFile as error:
        raise ReleaseValidationError("sin-agent.zip is not a valid ZIP archive") from error
    if names != expected:
        raise ReleaseValidationError("sin-agent.zip has an unexpected runtime layout")


def _validate_crop_archive(path: Path) -> None:
    expected_required = {"modDesc.xml", "scripts/SiNCropSettings.lua", "config/fruit-policy.xml"}
    try:
        with ZipFile(path) as archive:
            names = set(_safe_archive_names(archive, path.name))
            if not expected_required.issubset(names):
                raise ReleaseValidationError("SiN_FS25_Crop_Settings.zip is missing required files")
            try:
                descriptor = ElementTree.fromstring(archive.read("modDesc.xml"))
                policy_bytes = archive.read("config/fruit-policy.xml")
                policy = ElementTree.fromstring(policy_bytes)
            except (ValueError, ElementTree.ParseError) as error:
                raise ReleaseValidationError("SiN_FS25_Crop_Settings.zip has invalid XML") from error
            if descriptor.get("descVersion") != "92":
                raise ReleaseValidationError("SiN_FS25_Crop_Settings.zip has an unexpected descVersion")
            source_names = [node.get("filename") for node in descriptor.findall("./extraSourceFiles/sourceFile")]
            if source_names != ["scripts/SiNCropSettings.lua"]:
                raise ReleaseValidationError("SiN_FS25_Crop_Settings.zip has unexpected source files")
            icon_name = descriptor.findtext("iconFilename")
            if not icon_name or Path(icon_name).name != icon_name or icon_name not in names:
                raise ReleaseValidationError("SiN_FS25_Crop_Settings.zip has an invalid iconFilename")
            if policy.tag != "cropPolicy" or policy.get("schemaVersion") != "1":
                raise ReleaseValidationError("SiN_FS25_Crop_Settings.zip has an invalid policy schema")
            try:
                parse_policy(policy_bytes)
            except CropPolicyError as error:
                raise ReleaseValidationError("SiN_FS25_Crop_Settings.zip has an invalid crop policy") from error
            for member in names:
                if member.lower().endswith(".lua"):
                    validate_fs25_lua_source(archive.read(member), member)
    except BadZipFile as error:
        raise ReleaseValidationError("SiN_FS25_Crop_Settings.zip is not a valid ZIP archive") from error


def _validate_contracts_archive(path: Path) -> None:
    expected_required = {"modDesc.xml", "scripts/SiNContracts.lua", "icon_contracts.dds"}
    try:
        with ZipFile(path) as archive:
            names = set(_safe_archive_names(archive, path.name))
            if names != expected_required:
                raise ReleaseValidationError("SiN_FS25_Contracts.zip has an unexpected layout")
            try:
                descriptor = ElementTree.fromstring(archive.read("modDesc.xml"))
            except (ValueError, ElementTree.ParseError) as error:
                raise ReleaseValidationError("SiN_FS25_Contracts.zip has invalid modDesc.xml") from error
            if descriptor.get("descVersion") != "92":
                raise ReleaseValidationError("SiN_FS25_Contracts.zip has an unexpected descVersion")
            source_names = [node.get("filename") for node in descriptor.findall("./extraSourceFiles/sourceFile")]
            if source_names != ["scripts/SiNContracts.lua"]:
                raise ReleaseValidationError("SiN_FS25_Contracts.zip has unexpected source files")
            icon_name = descriptor.findtext("iconFilename")
            if icon_name != "icon_contracts.dds":
                raise ReleaseValidationError("SiN_FS25_Contracts.zip has an invalid iconFilename")
            validate_fs25_lua_source(archive.read("scripts/SiNContracts.lua"), "scripts/SiNContracts.lua")
    except BadZipFile as error:
        raise ReleaseValidationError("SiN_FS25_Contracts.zip is not a valid ZIP archive") from error


def _validate_policy_archive(path: Path) -> None:
    expected = {"modDesc.xml", "scripts/SiNProductionPolicy.lua", "scripts/SiNVehiclePricingPolicy.lua", "config/production-policy.xml", "config/construction-policy.xml", "config/vehicle-pricing-policy.xml", "icon_policy.dds"}
    try:
        with ZipFile(path) as archive:
            names = set(_safe_archive_names(archive, path.name))
            if names != expected:
                raise ReleaseValidationError("SiN_FS25_Policy.zip has an unexpected layout")
            try:
                descriptor = ElementTree.fromstring(archive.read("modDesc.xml"))
                policy = ElementTree.fromstring(archive.read("config/production-policy.xml"))
                construction_policy = ElementTree.fromstring(archive.read("config/construction-policy.xml"))
                vehicle_policy = ElementTree.fromstring(archive.read("config/vehicle-pricing-policy.xml"))
            except (ValueError, ElementTree.ParseError) as error:
                raise ReleaseValidationError("SiN_FS25_Policy.zip has invalid XML") from error
            if descriptor.get("descVersion") != "92" or descriptor.findtext("iconFilename") != "icon_policy.dds":
                raise ReleaseValidationError("SiN_FS25_Policy.zip has invalid modDesc.xml")
            if [node.get("filename") for node in descriptor.findall("./extraSourceFiles/sourceFile")] != ["scripts/SiNProductionPolicy.lua", "scripts/SiNVehiclePricingPolicy.lua"]:
                raise ReleaseValidationError("SiN_FS25_Policy.zip has unexpected source files")
            if policy.tag != "productionPolicy" or policy.get("schemaVersion") != "1":
                raise ReleaseValidationError("SiN_FS25_Policy.zip has an invalid production policy schema")
            if construction_policy.tag != "constructionPolicy" or construction_policy.get("schemaVersion") != "1":
                raise ReleaseValidationError("SiN_FS25_Policy.zip has an invalid construction policy schema")
            if vehicle_policy.tag != "vehiclePricingPolicy" or vehicle_policy.get("schemaVersion") != "1":
                raise ReleaseValidationError("SiN_FS25_Policy.zip has an invalid vehicle policy schema")
            validate_fs25_lua_source(archive.read("scripts/SiNProductionPolicy.lua"), "scripts/SiNProductionPolicy.lua")
            validate_fs25_lua_source(archive.read("scripts/SiNVehiclePricingPolicy.lua"), "scripts/SiNVehiclePricingPolicy.lua")
    except BadZipFile as error:
        raise ReleaseValidationError("SiN_FS25_Policy.zip is not a valid ZIP archive") from error


def validate_release_directory(root: str | Path, *, expected_version: str | None = None,
                               require_clean: bool = False) -> dict:
    """Validate the complete release output, including packaged Lua bytes."""
    root = Path(root)
    if not root.is_dir():
        raise ReleaseValidationError(f"release directory does not exist: {root}")
    actual = {path.name for path in root.iterdir()}
    if actual != RELEASE_ASSETS:
        raise ReleaseValidationError(f"release assets differ: expected {sorted(RELEASE_ASSETS)}, got {sorted(actual)}")

    manifest = _json(root / "build-manifest.json")
    if manifest.get("release_format_version") != 1 or not _COMMIT.fullmatch(str(manifest.get("git_commit", ""))):
        raise ReleaseValidationError("build manifest has an invalid release format or commit")
    if expected_version is not None and manifest.get("version") != expected_version:
        raise ReleaseValidationError("build manifest version does not match the requested version")
    if require_clean and manifest.get("dirty") is not False:
        raise ReleaseValidationError("release build manifest reports a dirty checkout")
    if manifest.get("required_scenarios") != REQUIRED_SCENARIOS:
        raise ReleaseValidationError("build manifest required scenarios are invalid")

    sums = {}
    for line in (root / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        match = _SHA_LINE.fullmatch(line.strip())
        if match is None:
            raise ReleaseValidationError("SHA256SUMS.txt contains an invalid line")
        sums[match.group(2)] = match.group(1).lower()
    if set(sums) != CHECKSUM_ASSETS:
        raise ReleaseValidationError("SHA256SUMS.txt does not cover exactly the checksummed assets")
    manifest_fields = {
        "sin-agent.zip": "agent_sha256",
        "FS25_SiN_Server.zip": "server_sha256",
        "SiN_FS25_Crop_Settings.zip": "crop_settings_sha256",
        "SiN_FS25_Contracts.zip": "contracts_sha256",
        "SiN_FS25_Policy.zip": "policy_sha256",
        "Update-SiN.ps1": "updater_sha256",
        "Update-SiN-Client.ps1": "client_updater_sha256",
        "Restart-SiN-Agent.ps1": "agent_restart_sha256",
        "Publish-SiN-Modpack.ps1": "modpack_publisher_sha256",
        "integration-campaign.json": "campaign_report_sha256",
        "live-validation-manifest.json": "live_validation_manifest_sha256",
    }
    for name in CHECKSUM_ASSETS:
        actual_hash = _sha256(root / name)
        if sums[name] != actual_hash or manifest.get(manifest_fields[name], "").lower() != actual_hash:
            raise ReleaseValidationError(f"checksum mismatch: {name}")

    campaign = _json(root / "integration-campaign.json")
    if campaign.get("status") != "passed":
        raise ReleaseValidationError("integration-campaign.json is not a passed report")
    live_manifest = _json(root / "live-validation-manifest.json")
    if live_manifest.get("manifest_schema") != "sin.live-validation/1":
        raise ReleaseValidationError("live validation manifest schema is invalid")
    if live_manifest.get("required_scenarios") != REQUIRED_SCENARIOS:
        raise ReleaseValidationError("live validation manifest required scenarios are invalid")
    _validate_agent_archive(root / "sin-agent.zip")
    _validate_server_archive(root / "FS25_SiN_Server.zip")
    _validate_crop_archive(root / "SiN_FS25_Crop_Settings.zip")
    _validate_contracts_archive(root / "SiN_FS25_Contracts.zip")
    _validate_policy_archive(root / "SiN_FS25_Policy.zip")
    return manifest
