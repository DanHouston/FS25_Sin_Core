"""Inspect active FS25 selling-station XML without changing a map or savegame."""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def load_station(zip_file: zipfile.ZipFile, filename: str) -> ET.Element | None:
    marker = Path(zip_file.filename).stem + "/"
    if marker not in filename:
        return None
    member = filename.split(marker, 1)[1]
    try:
        return ET.fromstring(zip_file.read(member)).find(".//sellingStation")
    except KeyError:
        return None


def describe_station(station: ET.Element, categories: dict[str, set[str]]) -> tuple[set[str], list[tuple[str, set[str]]]]:
    accepted = set(station.attrib.get("fillTypes", "").split())
    triggers = []
    for trigger in station:
        if trigger.tag not in {"unloadTrigger", "baleTrigger", "palletTrigger"}:
            continue
        types = set(trigger.attrib.get("fillTypes", "").split())
        for category in trigger.attrib.get("fillTypeCategories", "").split():
            types.update(categories.get(category, set()))
        types.difference_update(trigger.attrib.get("fillTypesExclude", "").split())
        triggers.append((trigger.tag, types))
        accepted.update(types)
    return accepted, triggers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("map_zip", type=Path)
    parser.add_argument("placeables_xml", type=Path)
    parser.add_argument("--fill-types", type=Path, help="Installed FS25 maps_fillTypes.xml for category expansion")
    args = parser.parse_args()
    categories = {}
    if args.fill_types is not None:
        fill_types = ET.parse(args.fill_types).getroot()
        categories = {node.attrib["name"]: set((node.text or "").split()) for node in fill_types.findall(".//fillTypeCategory")}
    placeables = ET.parse(args.placeables_xml).getroot()
    with zipfile.ZipFile(args.map_zip) as archive:
        for placeable in placeables.findall("placeable"):
            filename = placeable.attrib.get("filename", "")
            station = load_station(archive, filename)
            if station is None:
                continue
            accepted, triggers = describe_station(station, categories)
            saved_station = placeable.find("sellingStation")
            saved_types = {node.attrib.get("fillType") for node in saved_station.findall("stats") if node.attrib.get("fillType")} if saved_station is not None else set()
            print(f"{filename} farmId={placeable.attrib.get('farmId')} station={station.attrib}")
            print(f"  accepted={','.join(sorted(accepted))}")
            print(f"  savedStats={','.join(sorted(saved_types))}")
            for trigger in station:
                if trigger.tag in {"unloadTrigger", "baleTrigger", "palletTrigger", "woodTrigger"}:
                    print(f"  {trigger.tag}={trigger.attrib}")


if __name__ == "__main__":
    main()
