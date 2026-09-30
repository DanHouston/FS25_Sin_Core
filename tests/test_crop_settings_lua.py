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
            addConsoleCommand = function(name, help, callback, target)
                probeCommand = function(fruit) return target[callback](target, fruit) end
            end
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
    def test_live_regression_withering_runs_on_entry_not_exit_of_dead_month(self):
        policy = parse_policy(POLICY)
        # End of harvest month -> first forbidden month, including year wrap.
        for name, outgoing, entering in (("PEA", 8, 9), ("OAT", 8, 9),
                                         ("BARLEY", 4, 5), ("CANOLA", 4, 5),
                                         ("WHEAT", 5, 6), ("SOYBEAN", 10, 11)):
            with self.subTest(crop=name):
                entry = next(f for f in policy.fruits if f.name == name)
                desc, path = descriptor(entry)
                h = Harness([desc]); h.apply()
                periods = h.periods(name)
                ids = desc["nameToGrowthState"]
                ready, dead = ids[path[-1]], ids["DEAD"]
                self.assertTrue(periods[PERIODS[outgoing - 1]]["isHarvestable"])
                self.assertFalse(periods[PERIODS[entering - 1]]["isHarvestable"])
                # Native uses outgoing index while the displayed calendar enters next.
                self.assertEqual(periods[PERIODS[outgoing - 1]]["growthMapping"][ready], dead)
                self.assertEqual(periods[PERIODS[outgoing - 2]]["growthMapping"][ready], ready)

    def test_readonly_probe_preserves_engine_arguments_returns_and_mappings(self):
        entry = next(f for f in parse_policy(POLICY).fruits if f.name == "OAT")
        desc, _ = descriptor(entry)
        harness = Harness([desc])
        harness.apply()
        original = harness.periods("OAT")
        harness.lua.execute('''
            g_fruitTypeManager = manager
            local calls = 0
            GrowthSystem = {setMonthEngineState = function(self, period, extra)
                assert(period == 9 and extra == "unchanged")
                calls = calls + 1
                return nil, 27, "native"
            end}
            assert(string.find(probeCommand("OAT"), "armed"))
            for i = 1, 6 do
                local a, b, c = GrowthSystem:setMonthEngineState(9, "unchanged")
                assert(a == nil and b == 27 and c == "native")
            end
            assert(calls == 6)
            assert(string.find(probeCommand("MISSING"), "not registered"))
        ''')
        self.assertEqual(original, harness.periods("OAT"))
        probes = [line for line in harness.logs if "growth-probe source=" in line]
        self.assertEqual(len(probes), 5)  # console plus four bounded engine calls
        self.assertIn("source=setMonthEngineState(9,string)", probes[1])

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
                    if entry.name == "SUNFLOWER" and not short:
                        self.assertIn("unsupported=1", harness.logs[-1])
                        self.assertEqual(actual, desc["growthDataSeasonal"]["periods"])
                        continue
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
                        for step in range(1, last - start + 1):
                            month = (start + step) % 12
                            current = actual[PERIODS[(month - 1) % 12]]["growthMapping"].get(current, current)
                            if not visited or current != visited[-1]:
                                visited.append(current)
                            if current == ids[path[-1]]:
                                self.assertIn(month, harvest)
                                seen_ready = True
                            elif seen_ready:
                                self.fail("mature crop lost inside harvest window")
                        self.assertTrue(seen_ready, (entry.name, start))
                        self.assertEqual(visited, [ids[s] for s in path])
                        next_period = PERIODS[last % 12]
                        self.assertEqual(actual[next_period]["growthMapping"].get(current, current), ids["DEAD"])
                        later_period = PERIODS[(last + 1) % 12]
                        self.assertEqual(actual[later_period]["growthMapping"].get(current, current), ids["DEAD"])
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

    def test_sorghum_uses_annual_policy_and_both_cohorts_complete(self):
        entry = next(x for x in parse_policy(POLICY).fruits if x.name == "SORGHUM")
        desc, path = descriptor(entry)
        h = Harness([desc])
        h.apply()
        actual = h.periods("SORGHUM")
        self.assertIn("unsupported=0", h.logs[-1])
        ids = desc["nameToGrowthState"]
        for i, p in enumerate(PERIODS, 1):
            mapping = actual[p]["growthMapping"]
            self.assertEqual(set(mapping), set(ids.values()))
            self.assertTrue(all(isinstance(value, int) for value in mapping.values()))
        self.assertEqual(actual["MID_SUMMER"]["growthMapping"][ids["GREENMIDDLE"]], ids["GREENBIG"])
        self.assertEqual(actual["EARLY_SUMMER"]["growthMapping"][ids["GREENBIG"]], ids["GREENBIG"])
        self.assertEqual(actual["LATE_SUMMER"]["growthMapping"][ids["GREENBIG"]], ids["HARVESTREADY"])
        self.assertEqual(actual["EARLY_WINTER"]["growthMapping"][ids["HARVESTREADY"]], ids["DEAD"])
        for start in (1, 2):  # April / May
            state = ids["INVISIBLE"]
            for month in range(start + 1, 10):  # boundaries through December
                state = actual[PERIODS[month - 1]]["growthMapping"].get(state, state)
                if month < 5:
                    self.assertNotEqual(state, ids["HARVESTREADY"])
                elif month < 9:
                    # May sowing matures in September on the full path;
                    # April sowing reaches readiness in August.
                    if month >= start + len(path):
                        self.assertEqual(state, ids["HARVESTREADY"])
                    self.assertTrue(actual[PERIODS[month]]["isHarvestable"])
                else:
                    self.assertEqual(state, ids["DEAD"])
        h.apply()
        self.assertEqual(h.periods("SORGHUM"), actual)

    def test_annual_policy_withers_native_harvest_state_range(self):
        entry = next(x for x in parse_policy(POLICY).fruits if x.name == "OAT")
        desc, _ = descriptor(entry)
        ids = desc["nameToGrowthState"]
        ids["HARVESTREADY2"] = 5
        ids["HARVESTREADY3"] = 6
        # Keep the extra native harvest-ready states visible to the runtime
        # descriptor without adding them to SiN's concise stateChain.
        for period in desc["growthDataSeasonal"]["periods"].values():
            period["growthMapping"][5] = 5
            period["growthMapping"][6] = 6
        desc["minHarvestingGrowthState"] = 4
        desc["maxHarvestingGrowthState"] = 6
        harness = Harness([desc])
        harness.apply()
        late_autumn = harness.periods("OAT")["LATE_AUTUMN"]["growthMapping"]
        self.assertEqual({late_autumn[state] for state in (4, 5, 6)}, {ids["DEAD"]})

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

    def test_lua_rejects_non_integer_native_mapping_without_partial_gate_change(self):
        entry = next(x for x in parse_policy(POLICY).fruits if x.name == "WHEAT")
        desc, _ = descriptor(entry)
        desc["growthDataSeasonal"]["periods"]["MID_SUMMER"]["growthMapping"]["bad"] = 4
        original = copy.deepcopy(desc["growthDataSeasonal"]["periods"])
        harness = Harness([desc])
        harness.apply()
        self.assertIn("unsupported=1", harness.logs[-1])
        actual = harness.periods("WHEAT")
        for period in PERIODS:
            self.assertEqual(actual[period]["plantingAllowed"], original[period]["plantingAllowed"])
            self.assertEqual(actual[period]["isHarvestable"], original[period]["isHarvestable"])
            self.assertEqual(actual[period]["growthMapping"], original[period]["growthMapping"])
