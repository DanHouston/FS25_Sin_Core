"""Create a transparent FS25 production-margin report from live game XML.

This is analysis only: it never changes a policy or a mod.  It values every
recipe at the arithmetic mean of the active FS25 annual fill-type price curve.
Inputs are opportunity-costed at that same value; outputs use the same average.
Actual selling-station multipliers, transport and player-selected seasonal sale
timing are intentionally not assumed.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


@dataclass(frozen=True)
class Asset:
    canonical_id: str
    source_price: float
    effective_price: float
    xml: bytes


def xml_prices(path: Path) -> dict[str, float]:
    root = ET.parse(path).getroot()
    result: dict[str, float] = {}
    for node in root.findall(".//fillType"):
        economy = node.find("economy")
        raw = economy is not None and economy.get("pricePerLiter")
        if not raw or not node.get("name"):
            continue
        factors = [float(item.get("value")) for item in economy.findall("./factors/factor") if item.get("value")]
        result[node.get("name").upper()] = float(raw) * (mean(factors) if factors else 1.0)
    return result


def write_price_calendar(path: Path, source: Path) -> None:
    """Write native base and all 12 native monthly values in $/1,000 L."""
    root = ET.parse(source).getroot()
    month_names = ("MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC", "JAN", "FEB")
    rows = []
    for node in root.findall(".//fillType"):
        economy = node.find("economy")
        raw = economy is not None and economy.get("pricePerLiter")
        name = node.get("name")
        if not raw or not name:
            continue
        base = float(raw) * 1000
        factors = {int(item.get("period")): float(item.get("value"))
                   for item in economy.findall("./factors/factor")
                   if item.get("period") and item.get("value")}
        row: dict[str, object] = {"fill_type": name.upper(), "base_price_per_kl": base}
        for index, month in enumerate(month_names, 1):
            row[month] = base * factors.get(index, 1.0)
        row["annual_average_per_kl"] = sum(row[month] for month in month_names) / len(month_names)
        rows.append(row)
    rows.sort(key=lambda row: str(row["fill_type"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=("fill_type", "base_price_per_kl", *month_names, "annual_average_per_kl"))
        writer.writeheader(); writer.writerows(rows)
    print(f"Wrote {len(rows)} native fill-type price calendars to {path}")


def mod_xml(mod_name: str, relative: str, mods: Path, game: Path) -> bytes:
    if mod_name == "FS25_BaseGame":
        return (game / relative).read_bytes()
    direct = mods / mod_name / relative
    if direct.is_file():
        return direct.read_bytes()
    archive = mods / f"{mod_name}.zip"
    if not archive.is_file() and mod_name == "pdlc_strawHarvestPack":
        archive = game / "pdlc" / "strawHarvestPack.dlc"
    with ZipFile(archive) as zip_file:
        return zip_file.read(relative)


def assets(catalog: Path, mods: Path, game: Path) -> list[Asset]:
    result = []
    with catalog.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            try:
                result.append(Asset(row["canonical_id"], float(row["source_price"]),
                                    float(row["effective_price"]),
                                    mod_xml(row["mod_name"], row["xml_path"], mods, game)))
            except (BadZipFile, FileNotFoundError, KeyError, ValueError, OSError) as error:
                print(f"SKIPPED {row.get('canonical_id', '<unknown>')}: {error}")
    return result


def amount_text(nodes: list[ET.Element], prices: dict[str, float]) -> tuple[str, float | None]:
    total = 0.0
    text = []
    for node in nodes:
        name, amount = (node.get("fillType") or "").upper(), float(node.get("amount") or 0)
        text.append(f"{name}:{amount:g}")
        if name not in prices:
            return ";".join(text), None
        total += amount * prices[name]
    return ";".join(text), total


def rows(asset: Asset, prices: dict[str, float]) -> list[dict[str, object]]:
    root = ET.fromstring(asset.xml)
    output = []
    for recipe in root.findall("./productionPoint/productions/production"):
        cycles = float(recipe.get("cyclesPerHour") or 0)
        operating = float(recipe.get("costsPerActiveHour") or 0)
        inputs, input_value = amount_text(recipe.findall("./inputs/input"), prices)
        outputs, output_value = amount_text(recipe.findall("./outputs/output"), prices)
        revenue = output_value * cycles if output_value is not None else None
        input_cost = input_value * cycles if input_value is not None else None
        margin = revenue - input_cost - operating if revenue is not None and input_cost is not None else None
        output.append({
            "canonical_id": asset.canonical_id, "source_price": asset.source_price,
            "effective_price": asset.effective_price, "recipe": recipe.get("id", ""),
            "cycles_per_hour": cycles, "inputs": inputs, "outputs": outputs,
            "annual_average_output_value_per_hour": revenue,
            "annual_average_input_cost_per_hour": input_cost,
            "operating_cost_per_hour": operating, "net_margin_per_active_hour": margin,
            "payback_active_hours_at_effective_price": asset.effective_price / margin if margin and margin > 0 else None,
        })
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--mods-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--price-calendar-output", type=Path)
    args = parser.parse_args()
    prices = xml_prices(args.game_root / "data" / "maps" / "maps_fillTypes.xml")
    if args.price_calendar_output:
        write_price_calendar(args.price_calendar_output, args.game_root / "data" / "maps" / "maps_fillTypes.xml")
    report = [item for asset in assets(args.catalog, args.mods_dir, args.game_root) for item in rows(asset, prices)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = list(report[0]) if report else []
    with args.output.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader(); writer.writerows(report)
    print(f"Wrote {len(report)} recipes to {args.output}; priced fill types={len(prices)}")


if __name__ == "__main__":
    main()
