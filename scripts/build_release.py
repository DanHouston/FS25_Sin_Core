"""Build the versioned, secret-free SiN runtime release assets."""
import argparse
import hashlib
import json
import os
import subprocess
import shutil
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fs25_network_core.integration_campaign import authoritative_manifest, run_campaign
from fs25_network_core.lua_validation import validate_fs25_lua_source
from fs25_network_core.release_validation import validate_release_directory
AGENT_FILES = ("fs25_network_core/__init__.py", "fs25_network_core/agent.py")
MOD_ASSET = "FS25_SiN_Server.zip"
CROP_MOD_ASSET = "SiN_FS25_Crop_Settings.zip"
CONTRACTS_MOD_ASSET = "SiN_FS25_Contracts.zip"
POLICY_MOD_ASSET = "SiN_FS25_Policy.zip"
CLIENT_UPDATER_ASSET = "Update-SiN-Client.ps1"
AGENT_RESTART_ASSET = "Restart-SiN-Agent.ps1"
MODPACK_PUBLISHER_ASSET = "Publish-SiN-Modpack.ps1"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def zip_files(destination, files):
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for archive_name, source in files:
            info = ZipInfo(archive_name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, Path(source).read_bytes())


def build_mod(destination):
    source = ROOT / "mods" / "FS25_SiN_Server"
    descriptor = source / "modDesc.xml"
    icon_name = None
    import xml.etree.ElementTree as ET
    descriptor_xml = ET.parse(descriptor)
    icon_name = descriptor_xml.findtext("iconFilename")
    if not icon_name or Path(icon_name).name != icon_name or not (source / icon_name).is_file():
        raise ValueError("FS25_SiN_Server mod descriptor/icon is invalid")
    source_names = [node.get("filename") for node in descriptor_xml.findall("./extraSourceFiles/sourceFile")]
    if any(not name for name in source_names):
        raise ValueError("FS25_SiN_Server mod descriptor contains an invalid source file")
    names_to_package = ["modDesc.xml"] + source_names + [icon_name]
    for lua_path in source.rglob("*.lua"):
        validate_fs25_lua_source(lua_path.read_bytes(), str(lua_path.relative_to(ROOT)))
    files = [(name, source / name) for name in names_to_package]
    zip_files(destination, files)
    with ZipFile(destination) as archive:
        names = set(archive.namelist())
        if not {"modDesc.xml", "NetworkLocal.lua"}.issubset(names):
            raise ValueError("FS25_SiN_Server ZIP is missing required root files")
        if set(names_to_package) != names:
            raise ValueError("FS25_SiN_Server ZIP does not match mod descriptor sources")
        for name in names:
            if name.lower().endswith(".lua"):
                validate_fs25_lua_source(archive.read(name), name)


def build_crop_mod(destination):
    """Build the standalone crop policy mod without map/server dependencies."""
    source = ROOT / "mods" / "SiN_FS25_Crop_Settings"
    descriptor = source / "modDesc.xml"
    import xml.etree.ElementTree as ET
    descriptor_xml = ET.parse(descriptor)
    icon_name = descriptor_xml.findtext("iconFilename")
    if not icon_name or Path(icon_name).name != icon_name or not (source / icon_name).is_file():
        raise ValueError("SiN_FS25_Crop_Settings mod descriptor/icon is invalid")
    source_names = [node.get("filename") for node in descriptor_xml.findall("./extraSourceFiles/sourceFile")]
    if any(not name or Path(name).is_absolute() or ".." in Path(name).parts for name in source_names):
        raise ValueError("SiN_FS25_Crop_Settings descriptor contains an invalid source file")
    config_name = "config/fruit-policy.xml"
    names_to_package = ["modDesc.xml"] + source_names + [config_name, icon_name]
    for name in names_to_package:
        if not (source / name).is_file():
            raise ValueError(f"SiN_FS25_Crop_Settings source is missing: {name}")
    for lua_path in source.rglob("*.lua"):
        validate_fs25_lua_source(lua_path.read_bytes(), str(lua_path.relative_to(ROOT)))
    files = [(name, source / name) for name in names_to_package]
    zip_files(destination, files)
    with ZipFile(destination) as archive:
        names = set(archive.namelist())
        if set(names_to_package) != names:
            raise ValueError("SiN_FS25_Crop_Settings ZIP does not match its declared sources")
        for name in names:
            if name.lower().endswith(".lua"):
                validate_fs25_lua_source(archive.read(name), name)
        ET.fromstring(archive.read(config_name))


def build_contracts_mod(destination):
    """Build the standalone, read-only native contract diagnostics mod."""
    source = ROOT / "mods" / "SiN_FS25_Contracts"
    descriptor = source / "modDesc.xml"
    import xml.etree.ElementTree as ET
    descriptor_xml = ET.parse(descriptor)
    icon_name = descriptor_xml.findtext("iconFilename")
    source_names = [node.get("filename") for node in descriptor_xml.findall("./extraSourceFiles/sourceFile")]
    if not icon_name or Path(icon_name).name != icon_name or not (source / icon_name).is_file():
        raise ValueError("SiN_FS25_Contracts mod descriptor/icon is invalid")
    if source_names != ["scripts/SiNContracts.lua"]:
        raise ValueError("SiN_FS25_Contracts descriptor has unexpected source files")
    names_to_package = ["modDesc.xml", *source_names, icon_name]
    for name in names_to_package:
        if not (source / name).is_file():
            raise ValueError(f"SiN_FS25_Contracts source is missing: {name}")
    for lua_path in source.rglob("*.lua"):
        validate_fs25_lua_source(lua_path.read_bytes(), str(lua_path.relative_to(ROOT)))
    zip_files(destination, [(name, source / name) for name in names_to_package])
    with ZipFile(destination) as archive:
        names = set(archive.namelist())
        if names != set(names_to_package):
            raise ValueError("SiN_FS25_Contracts ZIP does not match its descriptor")


def build_production_policy_mod(destination):
    """Build the standalone runtime production policy mod."""
    source = ROOT / "mods" / "SiN_FS25_ProductionPolicy"
    descriptor = source / "modDesc.xml"
    import xml.etree.ElementTree as ET
    descriptor_xml = ET.parse(descriptor)
    icon_name = descriptor_xml.findtext("iconFilename")
    source_names = [node.get("filename") for node in descriptor_xml.findall("./extraSourceFiles/sourceFile")]
    if not icon_name or Path(icon_name).name != icon_name or not (source / icon_name).is_file():
        raise ValueError("SiN_FS25_ProductionPolicy mod descriptor/icon is invalid")
    if source_names != ["scripts/SiNProductionPolicy.lua"]:
        raise ValueError("SiN_FS25_ProductionPolicy descriptor has unexpected source files")
    config_names = ["config/production-policy.xml", "config/construction-policy.xml"]
    names_to_package = ["modDesc.xml", *source_names, *config_names, icon_name]
    for name in names_to_package:
        if not (source / name).is_file():
            raise ValueError(f"SiN_FS25_ProductionPolicy source is missing: {name}")
    for lua_path in source.rglob("*.lua"):
        validate_fs25_lua_source(lua_path.read_bytes(), str(lua_path.relative_to(ROOT)))
    zip_files(destination, [(name, source / name) for name in names_to_package])
    with ZipFile(destination) as archive:
        if set(archive.namelist()) != set(names_to_package):
            raise ValueError("SiN_FS25_ProductionPolicy ZIP does not match its descriptor")
        for config_name in config_names:
            ET.fromstring(archive.read(config_name))
        for name in archive.namelist():
            if name.lower().endswith(".lua"):
                validate_fs25_lua_source(archive.read(name), name)


def build_vehicle_pricing_policy_mod(destination):
    """Build the standalone third-party motor-vehicle price policy mod."""
    source = ROOT / "mods" / "SiN_FS25_Vehicle_Pricing_Policy"
    descriptor = source / "modDesc.xml"
    import xml.etree.ElementTree as ET
    descriptor_xml = ET.parse(descriptor)
    icon_name = descriptor_xml.findtext("iconFilename")
    source_names = [node.get("filename") for node in descriptor_xml.findall("./extraSourceFiles/sourceFile")]
    if icon_name != "icon_vehicle_pricing_policy.dds" or not (source / icon_name).is_file():
        raise ValueError("SiN_FS25_Vehicle_Pricing_Policy mod descriptor/icon is invalid")
    if source_names != ["scripts/SiNVehiclePricingPolicy.lua"]:
        raise ValueError("SiN_FS25_Vehicle_Pricing_Policy descriptor has unexpected source files")
    config_name = "config/vehicle-pricing-policy.xml"
    names_to_package = ["modDesc.xml", *source_names, config_name, icon_name]
    for name in names_to_package:
        if not (source / name).is_file():
            raise ValueError(f"SiN_FS25_Vehicle_Pricing_Policy source is missing: {name}")
    for lua_path in source.rglob("*.lua"):
        validate_fs25_lua_source(lua_path.read_bytes(), str(lua_path.relative_to(ROOT)))
    zip_files(destination, [(name, source / name) for name in names_to_package])
    with ZipFile(destination) as archive:
        if set(archive.namelist()) != set(names_to_package):
            raise ValueError("SiN_FS25_Vehicle_Pricing_Policy ZIP does not match its descriptor")
        ET.fromstring(archive.read(config_name))
        validate_fs25_lua_source(archive.read("scripts/SiNVehiclePricingPolicy.lua"), "scripts/SiNVehiclePricingPolicy.lua")


def build_policy_mod(destination):
    """Build the modular production + vehicle policy mod."""
    source = ROOT / "mods" / "SiN_FS25_Policy"
    descriptor = source / "modDesc.xml"
    import xml.etree.ElementTree as ET
    descriptor_xml = ET.parse(descriptor)
    icon_name = descriptor_xml.findtext("iconFilename")
    source_names = [node.get("filename") for node in descriptor_xml.findall("./extraSourceFiles/sourceFile")]
    config_names = ["config/production-policy.xml", "config/construction-policy.xml", "config/vehicle-pricing-policy.xml"]
    expected_sources = [
        "scripts/SiNProductionPolicy.lua",
        "scripts/SiNVehiclePricingPolicy.lua",
        "scripts/SiNSellCoveragePolicy.lua",
        "scripts/SiNBuyingStationPolicy.lua",
    ]
    if icon_name != "icon_policy.dds" or not (source / icon_name).is_file() or source_names != expected_sources:
        raise ValueError("SiN_FS25_Policy descriptor is invalid")
    names_to_package = ["modDesc.xml", *source_names, *config_names, icon_name]
    for name in names_to_package:
        if not (source / name).is_file():
            raise ValueError(f"SiN_FS25_Policy source is missing: {name}")
    for lua_path in source.rglob("*.lua"):
        validate_fs25_lua_source(lua_path.read_bytes(), str(lua_path.relative_to(ROOT)))
    zip_files(destination, [(name, source / name) for name in names_to_package])
    with ZipFile(destination) as archive:
        if set(archive.namelist()) != set(names_to_package):
            raise ValueError("SiN_FS25_Policy ZIP does not match its descriptor")
        for config_name in config_names:
            ET.fromstring(archive.read(config_name))
        for source_name in source_names:
            validate_fs25_lua_source(archive.read(source_name), source_name)


def build_agent(destination):
    files = [(name, ROOT / name) for name in AGENT_FILES]
    zip_files(destination, files)
    with ZipFile(destination) as archive:
        names = set(archive.namelist())
        if set(AGENT_FILES) != names:
            raise ValueError("Agent ZIP contains an unexpected or incomplete runtime layout")


def build(version, output):
    output = Path(output).resolve()
    commit = git("rev-parse", "HEAD")
    try:
        branch = git("branch", "--show-current") or os.environ.get("GITHUB_REF_NAME", "detached")
    except subprocess.CalledProcessError:
        branch = os.environ.get("GITHUB_REF_NAME", "detached")
    try:
        dirty = bool(git("status", "--porcelain"))
    except subprocess.CalledProcessError:
        dirty = False
    canonical_dist = (ROOT / "dist").resolve()
    if output != canonical_dist and canonical_dist in output.parents:
        raise ValueError("release output must be the canonical dist directory, not a nested dist subdirectory")
    if output == canonical_dist and output.exists():
        # dist is generated state. Clean only this exact repository directory;
        # caller-supplied output directories remain reusable and untouched.
        for child in output.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    output.mkdir(parents=True, exist_ok=True)
    legacy_mod = output / "FS25_SiN_NetworkLocal.zip"
    if legacy_mod.exists():
        legacy_mod.unlink()
    agent = output / "sin-agent.zip"
    mod = output / MOD_ASSET
    crop_mod = output / CROP_MOD_ASSET
    contracts_mod = output / CONTRACTS_MOD_ASSET
    policy_mod = output / POLICY_MOD_ASSET
    build_agent(agent)
    build_mod(mod)
    build_crop_mod(crop_mod)
    build_contracts_mod(contracts_mod)
    build_policy_mod(policy_mod)
    updater = output / "Update-SiN.ps1"
    shutil.copy2(ROOT / "scripts" / "Update-SiN.ps1", updater)
    client_updater = output / CLIENT_UPDATER_ASSET
    shutil.copy2(ROOT / "scripts" / CLIENT_UPDATER_ASSET, client_updater)
    agent_restart = output / AGENT_RESTART_ASSET
    shutil.copy2(ROOT / "scripts" / AGENT_RESTART_ASSET, agent_restart)
    modpack_publisher = output / MODPACK_PUBLISHER_ASSET
    shutil.copy2(ROOT / "scripts" / MODPACK_PUBLISHER_ASSET, modpack_publisher)
    # Ship reproducible validation evidence beside the runtime assets.  The
    # evidence is never included in either runtime ZIP.
    campaign_report = output / "integration-campaign.json"
    run_campaign(campaign_report)
    live_manifest = output / "live-validation-manifest.json"
    shutil.copy2(ROOT / "docs" / "live-validation-manifest.json", live_manifest)
    manifest = {
        "version": version,
        "git_commit": commit,
        "git_branch": branch,
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "agent_sha256": sha256(agent),
        "server_sha256": sha256(mod),
        "crop_settings_sha256": sha256(crop_mod),
        "contracts_sha256": sha256(contracts_mod),
        "policy_sha256": sha256(policy_mod),
        "updater_sha256": sha256(updater),
        "client_updater_sha256": sha256(client_updater),
        "agent_restart_sha256": sha256(agent_restart),
        "modpack_publisher_sha256": sha256(modpack_publisher),
        "campaign_report_sha256": sha256(campaign_report),
        "live_validation_manifest_sha256": sha256(live_manifest),
        "required_scenarios": json.loads(live_manifest.read_text(encoding="utf-8"))["required_scenarios"],
        "authoritative_adapters": [adapter["id"] for adapter in authoritative_manifest()["adapters"]],
        "agent_entrypoint": "python -m fs25_network_core.agent --watch",
        "agent_restart_asset": AGENT_RESTART_ASSET,
        "minimum_python": "3.11",
        "release_format_version": 1,
        "source_repository": "DanHouston/FS25_Sin_Core",
        "dirty": dirty,
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
    }
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    checksummed = (agent, mod, crop_mod, contracts_mod, policy_mod, updater, client_updater, agent_restart, modpack_publisher, campaign_report, live_manifest)
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in checksummed), encoding="utf-8")
    validate_release_directory(output, expected_version=version)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist",
                        help="canonical generated output directory (default: dist)")
    args = parser.parse_args()
    build(args.version, args.output)
    print(args.output)


if __name__ == "__main__":
    main()
