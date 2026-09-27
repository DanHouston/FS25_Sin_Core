"""Execute the shipped Lua hook and policy, with GIANTS XML/registry adapters.

These are descriptor contract fixtures, not an emulation of density-map jobs.
The shorter paths exercise transitions present in the inspected Regional Crop
Calendar definitions; full paths exercise maps using all configured stages.
"""
import copy
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from lupa.lua51 import LuaRuntime

from fs25_network_core.crop_settings import apply_policy, parse_policy


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "mods/SiN_FS25_Crop_Settings/config/fruit-policy.xml"
SCRIPT = ROOT / "mods/SiN_FS25_Crop_Settings/scripts/SiNCropSettings.lua"
PERIODS = ("EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER",
           "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN",
           "LATE_AUTUMN", "EARLY_WINTER", "MID_WINTER", "LATE_WINTER")
# Native-style shorter paths: the ordering list may include unused visual states.
SHORT_PATHS = {
    "SUNFLOWER": "greenSmall greenMiddle greenBig harvestReady",
    "COTTON": "greenSmall greenSmall2 greenMiddle greenMiddle2 greenBig harvestReady",
    "SOYBEAN": "greenSmall greenMiddle greenMiddle2 greenBig harvestReady",
    "SUGARBEET": "greenSmall greenSmall2 greenMiddle greenMiddle2 greenBig harvestReady",
    "MAIZE": "greenSmall greenMiddle greenBig harvestReadyGreen harvestReady3",
    "PEA": "greenSmall greenMiddle harvestReady",
    "GREENBEAN": "greenSmall greenMiddle harvestReady",
    "RICE": "greenSmall greenMiddle greenMiddle2 harvestReady",
    "RICELONGGRAIN": "greenSmall greenMiddle greenMiddle2 harvestReady",
}


def descriptor(entry, short=False):
    tokens = ("INVISIBLE", *entry.state_chain, "DEAD")
    ids = {name: i for i, name in enumerate(tokens)}
    path = SHORT_PATHS.get(entry.name, " ".join(entry.state_chain)) if short else " ".join(entry.state_chain)
    path = path.upper().split()
    periods = {name: {"plantingAllowed": True, "isHarvestable": False,
                      "growthMapping": {}} for name in PERIODS}
    # Deliberately different native calendar: validates that policy truly
    # replaces both gates and growth transitions, without touching density state.
    for i, (a, b) in enumerate(zip(path, path[1:])):
        periods[PERIODS[i]]["growthMapping"][ids[a]] = ids[b]
    periods["MID_SUMMER"]["growthMapping"][ids["INVISIBLE"]] = ids[path[0]]
    periods["LATE_SUMMER"]["growthMapping"][ids[path[-1]]] = ids["DEAD"]
    return {"name": entry.name, "nameToGrowthState": ids,
            "growthDataSeasonal": {"periods": periods}}, path


class Harness:
    def __init__(self, descriptors, policy_text=None):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        xml_root = ET.fromstring(policy_text or POLICY.read_text())
        values, nodes = {}, set()

        def flatten(node, key):
            nodes.add(key)
            values.update({key + "#" + k: v for k, v in node.attrib.items()})
            counts = {}
            for child in node:
                index = counts.get(child.tag, 0)
                counts[child.tag] = index + 1
                suffix = f"({index})" if child.tag in {"fruit", "period", "update"} else ""
                flatten(child, key + "." + child.tag + suffix)

        flatten(xml_root, xml_root.tag)
        g = self.lua.globals()
        g.read_value = lambda key: values.get(key)
        g.has_node = lambda key: key in nodes or key in values
        self.logs = []
        g.record_log = self.logs.append
        g.fixtures = self.lua.table()
        self.policy_root = xml_root
        for desc in descriptors:
            data = copy.deepcopy(desc)
            data["growthDataSeasonal"]["periods"] = {
                i: data["growthDataSeasonal"]["periods"][name]
                for i, name in enumerate(PERIODS, 1)
            }
            g.fixtures[desc["name"]] = self.lua.table_from(data, recursive=True)
        self.lua.execute('''
            Logging = { info = record_log, warning = record_log }
            local xml = {}
            function xml:getString(key) return read_value(key) end
            function xml:getInt(key) return tonumber(read_value(key)) end
            function xml:hasProperty(key) return has_node(key) end
            function xml:delete() end
            XMLFile = { load = function() return xml end }
            -- The native registry becomes accessible only after native loading.
            FruitTypeManager = {
                loadMapData = function(self) self.loaded = true; return true end
            }
            manager = {}
            function manager:getFruitTypes() assert(self.loaded); return fixtures end
            function manager:getFruitTypeByName(name) assert(self.loaded); return fixtures[name] end
            for _, fruit in pairs(fixtures) do
                function fruit:loadGrowth()
                    error("preserveNative entries must not call native XML reload")
                end
            end
        ''')
        self.lua.execute(SCRIPT.read_text())

    def apply(self):
        self.lua.execute('FruitTypeManager.loadMapData(manager, nil, {mapId="test-map"}, "")')

    def periods(self, name):
        raw = self.lua.globals().fixtures[name].growthDataSeasonal.periods
        return {p: {"plantingAllowed": raw[i].plantingAllowed,
                    "isHarvestable": raw[i].isHarvestable,
                    "growthMapping": dict(raw[i].growthMapping.items())}
                for i, p in enumerate(PERIODS, 1)}


class CropLuaTests(unittest.TestCase):
    def test_every_annual_both_sowing_months_complete_in_actual_lua(self):
        policy = parse_policy(POLICY)
        for short in (False, True):
            for entry in policy.fruits:
                if entry.lifecycle != "ANNUAL":
                    continue
                with self.subTest(crop=entry.name, short=short):
                    desc, path = descriptor(entry, short)
                    harness = Harness([desc])
                    harness.apply()
                    actual = harness.periods(entry.name)
                    self.assertIn("unsupported=0", harness.logs[-1])
                    expected = copy.deepcopy(desc)
                    self.assertEqual(apply_policy(policy, [expected]).unsupported, 0)
                    self.assertEqual(actual, expected["growthDataSeasonal"]["periods"])
                    planting = [i for i, p in enumerate(entry.periods) if p.planting_allowed]
                    harvest = {i for i, p in enumerate(entry.periods) if p.harvest_allowed}
                    self.assertEqual({i for i, p in enumerate(PERIODS) if actual[p]["plantingAllowed"]}, set(planting))
                    self.assertEqual({i for i, p in enumerate(PERIODS) if actual[p]["isHarvestable"]}, harvest)
                    ids = desc["nameToGrowthState"]
                    last = max((h - planting[0]) % 12 for h in harvest) + planting[0]
                    for start in planting:
                        current = ids["INVISIBLE"]
                        seen_ready = False
                        visited = []
                        for step in range(last - start + 1):
                            month = (start + step) % 12
                            current = actual[PERIODS[month]]["growthMapping"].get(current, current)
                            if not visited or current != visited[-1]:
                                visited.append(current)
                            if current == ids[path[-1]]:
                                self.assertIn(month, harvest)
                                seen_ready = True
                            elif seen_ready:
                                self.fail("mature crop lost inside harvest window")
                        self.assertTrue(seen_ready, (entry.name, start))
                        self.assertEqual(visited, [ids[s] for s in path])
                        next_period = PERIODS[(last + 1) % 12]
                        self.assertEqual(actual[next_period]["growthMapping"].get(current, current), ids["DEAD"])
                    harness.apply()
                    self.assertEqual(harness.periods(entry.name), actual)

    def test_invalid_native_path_or_missing_terminal_keeps_entire_fruit_unchanged(self):
        entry = next(x for x in parse_policy(POLICY).fruits if x.name == "MAIZE")
        for invalid in ("no_edges", "terminal", "germination", "mapping", "gate", "too_late"):
            with self.subTest(invalid=invalid):
                desc, _ = descriptor(entry)
                custom_policy = None
                if invalid == "no_edges":
                    for p in desc["growthDataSeasonal"]["periods"].values():
                        p["growthMapping"] = {}
                elif invalid == "terminal":
                    del desc["nameToGrowthState"]["HARVESTREADY3"]
                elif invalid == "germination":
                    del desc["nameToGrowthState"]["GREENSMALL"]
                elif invalid == "mapping":
                    desc["growthDataSeasonal"]["periods"]["LATE_WINTER"]["growthMapping"] = False
                elif invalid == "gate":
                    desc["growthDataSeasonal"]["periods"]["LATE_WINTER"]["plantingAllowed"] = "yes"
                else:
                    root = ET.fromstring(POLICY.read_text())
                    maize = root.find("./fruits/fruit[@name='MAIZE']")
                    maize.set("harvestPeriods", "EARLY_SUMMER")
                    custom_policy = ET.tostring(root, encoding="unicode")
                harness = Harness([desc], custom_policy)
                harness.apply()
                raw = harness.lua.globals().fixtures.MAIZE.growthDataSeasonal.periods
                for i, name in enumerate(PERIODS, 1):
                    original = desc["growthDataSeasonal"]["periods"][name]
                    self.assertEqual(raw[i].plantingAllowed, original["plantingAllowed"])
                    self.assertEqual(raw[i].isHarvestable, original["isHarvestable"])
                    if isinstance(original["growthMapping"], dict):
                        self.assertEqual(dict(raw[i].growthMapping.items()), original["growthMapping"])
                    else:
                        self.assertEqual(raw[i].growthMapping, False)
                self.assertIn("applied=0", harness.logs[-1])
                self.assertIn("unsupported=1", harness.logs[-1])

    def test_optional_state_absent_is_supported_using_native_short_path(self):
        entry = next(x for x in parse_policy(POLICY).fruits if x.name == "MAIZE")
        desc, _ = descriptor(entry, short=True)
        del desc["nameToGrowthState"]["HARVESTREADYGREEN2"]
        harness = Harness([desc])
        harness.apply()
        self.assertIn("unsupported=0", harness.logs[-1])
        self.assertTrue(harness.periods("MAIZE")["MID_SPRING"]["plantingAllowed"])

    def test_sorghum_uses_unchanged_explicit_loader_and_both_cohorts_complete(self):
        ids = {"INVISIBLE": 0, "GREENSMALL": 1, "GREENMIDDLE": 2,
               "GREENBIG": 3, "HARVESTREADY": 4, "DEAD": 5}
        desc = {"name": "SORGHUM", "nameToGrowthState": ids,
                "growthDataSeasonal": {"periods": {
                    p: {"plantingAllowed": False, "isHarvestable": False,
                        "growthMapping": {}} for p in PERIODS}}}
        h = Harness([desc])
        seasonal = h.policy_root.find("./fruits/fruit[@name='SORGHUM']/growth/seasonal")
        native_loaded = {}
        for period in seasonal:
            native_loaded[PERIODS.index(period.get("name")) + 1] = {
                "plantingAllowed": period.get("plantingAllowed") == "true",
                "isHarvestable": False,
                "growthMapping": {ids[u.get("startState").upper()]: ids[u.get("endState").upper()]
                                  for u in period},
            }
        h.lua.globals().native_loaded = h.lua.table_from(native_loaded, recursive=True)
        h.lua.execute('''
            native_loads = 0
            function fixtures.SORGHUM:loadGrowth(xml, key)
                assert(string.match(key, "%.growth$"))
                native_loads = native_loads + 1
                self.growthDataSeasonal.periods = native_loaded
                return true
            end
        ''')
        h.apply()
        self.assertEqual(h.lua.globals().native_loads, 1)
        actual = h.periods("SORGHUM")
        for i, p in enumerate(PERIODS, 1):
            mapping = actual[p]["growthMapping"]
            self.assertEqual(set(mapping), set(ids.values()))
            self.assertTrue(all(isinstance(value, int) for value in mapping.values()))
        self.assertEqual(actual["MID_SPRING"]["growthMapping"][0], 1)
        self.assertEqual(actual["LATE_SPRING"]["growthMapping"][1], 2)
        self.assertEqual(actual["EARLY_AUTUMN"]["growthMapping"][4], 4)
        self.assertEqual(actual["EARLY_WINTER"]["growthMapping"][4], 5)
        for start in (1, 2):  # April / May
            state = 0
            for month in range(start, 10):  # through December
                state = actual[PERIODS[month]]["growthMapping"].get(state, state)
                if month < 5:
                    self.assertNotEqual(state, 4)
                elif month < 9:
                    self.assertEqual(state, 4)
                    self.assertTrue(actual[PERIODS[month]]["isHarvestable"])
                else:
                    self.assertEqual(state, 5)
        h.apply()
        self.assertEqual(h.lua.globals().native_loads, 1)

    def test_annual_mapping_has_integer_fallback_for_every_registered_state(self):
        entry = next(x for x in parse_policy(POLICY).fruits if x.name == "WHEAT")
        desc, _ = descriptor(entry)
        harness = Harness([desc])
        harness.apply()
        self.assertIn("unsupported=0", harness.logs[-1])
        for period in harness.periods("WHEAT").values():
            mapping = period["growthMapping"]
            self.assertEqual(set(mapping), set(desc["nameToGrowthState"].values()))
            self.assertTrue(all(isinstance(value, int) for value in mapping.values()))
