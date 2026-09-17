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
AGENT_FILES = ("fs25_network_core/__init__.py", "fs25_network_core/agent.py")
MOD_ASSET = "FS25_SiN_Server.zip"
CLIENT_UPDATER_ASSET = "Update-SiN-Client.ps1"


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
    files = [(name, source / name) for name in names_to_package]
    zip_files(destination, files)
    with ZipFile(destination) as archive:
        names = set(archive.namelist())
        if not {"modDesc.xml", "NetworkLocal.lua"}.issubset(names):
            raise ValueError("FS25_SiN_Server ZIP is missing required root files")
        if set(names_to_package) != names:
            raise ValueError("FS25_SiN_Server ZIP does not match mod descriptor sources")


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
    output.mkdir(parents=True, exist_ok=True)
    legacy_mod = output / "FS25_SiN_NetworkLocal.zip"
    if legacy_mod.exists():
        legacy_mod.unlink()
    agent = output / "sin-agent.zip"
    mod = output / MOD_ASSET
    build_agent(agent)
    build_mod(mod)
    updater = output / "Update-SiN.ps1"
    shutil.copy2(ROOT / "scripts" / "Update-SiN.ps1", updater)
    client_updater = output / CLIENT_UPDATER_ASSET
    shutil.copy2(ROOT / "scripts" / CLIENT_UPDATER_ASSET, client_updater)
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
        "updater_sha256": sha256(updater),
        "client_updater_sha256": sha256(client_updater),
        "campaign_report_sha256": sha256(campaign_report),
        "live_validation_manifest_sha256": sha256(live_manifest),
        "required_scenarios": json.loads(live_manifest.read_text(encoding="utf-8"))["required_scenarios"],
        "authoritative_adapters": [adapter["id"] for adapter in authoritative_manifest()["adapters"]],
        "agent_entrypoint": "python -m fs25_network_core.agent --watch",
        "minimum_python": "3.11",
        "release_format_version": 1,
        "source_repository": "DanHouston/FS25_Sin_Core",
        "dirty": dirty,
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
    }
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    checksummed = (agent, mod, updater, client_updater, campaign_report, live_manifest)
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in checksummed), encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.version, args.output)
    print(args.output)


if __name__ == "__main__":
    main()
