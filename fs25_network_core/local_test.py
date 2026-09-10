"""Offline telemetry harness. Never connects to MongoDB or acknowledges game jobs."""
import argparse
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent.parent


def read_snapshot(path, max_age=30):
    path = Path(path)
    age = time.time() - path.stat().st_mtime
    if age < -5 or age > max_age:
        raise ValueError("Snapshot is stale or has a future timestamp; run the test save and retry")
    payload = path.read_bytes()
    if len(payload) > 1_000_000 or b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("Unsupported snapshot XML")
    root = ET.fromstring(payload)
    if root.tag != "networkLocal" or root.get("schemaVersion") != "1":
        raise ValueError("Unsupported snapshot schema")
    source = root.get("source")
    if source not in ("game", "simulator") or not root.get("session"):
        raise ValueError("Missing snapshot source or session")
    sequence = int(root.attrib["sequence"])
    if sequence < 1:
        raise ValueError("Invalid snapshot sequence")
    farms = {}
    for index, node in enumerate(root.findall("./farms/farm"), start=1):
        raw_id = node.get("farmId")
        try:
            farm_id = int(raw_id)
        except (TypeError, ValueError):
            raise ValueError(f"Farm record {index}: missing or non-integer farmId ({raw_id!r})") from None
        if not 1 <= farm_id <= 254:
            raise ValueError(f"Farm record {index}: farmId {farm_id} is outside the supported range 1–254")
        if farm_id in farms:
            raise ValueError(f"Farm record {index}: duplicate farmId {farm_id}")
        if "name" not in node.attrib:
            raise ValueError(f"Farm record {index}: farmId {farm_id} is missing its name attribute")
        farms[farm_id] = node.attrib["name"]
    players = {}
    for node in root.findall("./players/player"):
        player_id = node.get("uniqueId", "").strip()
        if not player_id or player_id in players:
            raise ValueError("Missing or duplicate player identity")
        players[player_id] = node.get("name", "")
    return dict(source=source, session=root.attrib["session"], sequence=sequence, players=players,
                savegame_index=int(root.attrib["savegameIndex"]), farms=farms,
                unnamed_farm_ids=[farm_id for farm_id, name in farms.items() if not name.strip()])


def simulate(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = ET.Element("networkLocal", schemaVersion="1", source="simulator",
                      session="offline-test", sequence="1", savegameIndex="0")
    farms = ET.SubElement(root, "farms")
    ET.SubElement(farms, "farm", farmId="1", name="Local Test Farm")
    # Exclusive creation prevents overwriting real telemetry or an existing fixture.
    with path.open("xb") as stream:
        ET.ElementTree(root).write(stream, encoding="utf-8", xml_declaration=True)
    return read_snapshot(path)


def build_mod():
    source = ROOT / "mods" / "FS25_SiN_NetworkLocal"
    destination = ROOT / "dist" / "FS25_SiN_NetworkLocal.zip"
    destination.parent.mkdir(exist_ok=True)
    descriptor = ET.parse(source / "modDesc.xml")
    icon = descriptor.findtext("iconFilename")
    if not icon or Path(icon).name != icon or not (source / icon).is_file():
        raise ValueError("Mod descriptor must name an existing icon in the mod root")
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for filename in ("modDesc.xml", "NetworkLocal.lua", icon):
            archive.write(source / filename, filename)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build-mod")
    simulator = sub.add_parser("simulate")
    simulator.add_argument("--output", type=Path, default=ROOT / "local-test" / "simulated-snapshot.xml")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "build-mod":
            print(build_mod())
        elif args.command == "simulate":
            print(json.dumps(simulate(args.output), indent=2))
        else:
            print(json.dumps(read_snapshot(args.path), indent=2))
    except (OSError, ValueError, KeyError, ET.ParseError) as error:
        parser.exit(1, f"Local test: {error}\n")


if __name__ == "__main__":
    main()
