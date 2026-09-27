import textwrap
import unittest

from fs25_network_core.crop_settings import CropPolicyError, apply_policy, normalize_fruit_name, parse_policy


def policy_xml(body: str) -> str:
    return textwrap.dedent(f"""\
        <cropPolicy schemaVersion="1" policyVersion="test-1">
          <fruits>{body}</fruits>
        </cropPolicy>
    """)


class CropSettingsTests(unittest.TestCase):
    def test_empty_placeholder_is_valid_and_noop(self):
        policy = parse_policy('<cropPolicy schemaVersion="1" policyVersion="0.1.0"><fruits/></cropPolicy>')
        descriptor = {"name": "WHEAT", "growthDataSeasonal": {"periods": {}}}
        result = apply_policy(policy, [descriptor])
        self.assertEqual(result.changed, 0)
        self.assertEqual(descriptor, {"name": "WHEAT", "growthDataSeasonal": {"periods": {}}})

    def test_normalization_and_duplicate_policy_fail_closed(self):
        self.assertEqual(normalize_fruit_name(" wheat "), "WHEAT")
        with self.assertRaises(CropPolicyError):
            parse_policy(policy_xml('<fruit name="wheat" enabled="true"/><fruit name="WHEAT" enabled="false"/>'))

    def test_supported_fruit_applies_planting_harvest_and_transition(self):
        policy = parse_policy(policy_xml('''
          <fruit name="wheat" enabled="true">
            <seasonal>
              <period name="early_spring" plantingAllowed="false" harvestAllowed="true" growthTime="2.5">
                <growth><update fromState="2" toState="4"/></growth>
              </period>
            </seasonal>
          </fruit>
        '''))
        descriptor = {"name": "WHEAT", "growthDataSeasonal": {"periods": {
            "EARLY_SPRING": {"plantingAllowed": True, "harvestAllowed": False, "growthTime": 1.0, "growthMapping": {2: 3}}
        }}}
        result = apply_policy(policy, [descriptor])
        self.assertEqual((result.applied, result.changed, result.unsupported), (1, 1, 0))
        period = descriptor["growthDataSeasonal"]["periods"]["EARLY_SPRING"]
        self.assertEqual((period["plantingAllowed"], period["harvestAllowed"], period["growthTime"], period["growthMapping"][2]), (False, True, 2.5, 4))

    def test_absent_fruit_is_not_created(self):
        policy = parse_policy(policy_xml('<fruit name="MAIZE" enabled="true"/>'))
        descriptor = {"name": "WHEAT", "growthDataSeasonal": {"periods": {}}}
        result = apply_policy(policy, [descriptor])
        self.assertEqual(result.applied, 0)
        self.assertEqual(result.skipped, 0)
        self.assertNotIn("MAIZE", [descriptor["name"]])

    def test_unsupported_runtime_structures_are_not_mutated(self):
        policy = parse_policy(policy_xml('''
          <fruit name="WHEAT" enabled="true"><seasonal><period name="EARLY_SPRING"
            plantingAllowed="false" harvestAllowed="false"><growth><update fromState="1" toState="2"/>
            </growth></period></seasonal></fruit>
        '''))
        descriptor = {"name": "WHEAT", "growthDataSeasonal": {"periods": {
            "EARLY_SPRING": {"plantingAllowed": "yes", "growthMapping": []}
        }}}
        before = repr(descriptor)
        result = apply_policy(policy, [descriptor])
        self.assertGreaterEqual(result.unsupported, 2)
        self.assertEqual(descriptor["growthDataSeasonal"]["periods"]["EARLY_SPRING"]["plantingAllowed"], "yes")
        self.assertNotEqual(before, repr(descriptor))  # marker records an attempted policy version

    def test_application_is_idempotent(self):
        policy = parse_policy(policy_xml('''
          <fruit name="WHEAT" enabled="true"><seasonal><period name="EARLY_SPRING"
            plantingAllowed="false"/></seasonal></fruit>
        '''))
        descriptor = {"name": "WHEAT", "growthDataSeasonal": {"periods": {
            "EARLY_SPRING": {"plantingAllowed": True}
        }}}
        first = apply_policy(policy, [descriptor])
        second = apply_policy(policy, [descriptor])
        self.assertEqual((first.changed, second.changed, second.applied), (1, 0, 0))

    def test_shipped_sorghum_policy_is_only_enabled_crop_and_has_exact_windows(self):
        policy = parse_policy(__import__("pathlib").Path("mods/SiN_FS25_Crop_Settings/config/fruit-policy.xml"))
        self.assertEqual(len(policy.fruits), 25)
        self.assertEqual({fruit.name for fruit in policy.fruits}, {
            "BARLEY", "CANOLA", "CARROT", "MAIZE", "COTTON", "GRAPE", "GRASS",
            "GREENBEAN", "RICELONGGRAIN", "OAT", "OILSEEDRADISH", "OLIVE", "PARSNIP",
            "PEA", "POPLAR", "POTATO", "BEETROOT", "RICE", "SORGHUM", "SOYBEAN",
            "SPINACH", "SUGARBEET", "SUGARCANE", "SUNFLOWER", "WHEAT",
        })
        sorghum = next(fruit for fruit in policy.fruits if fruit.name == "SORGHUM")
        self.assertTrue(sorghum.enabled)
        periods = {period.name: period for period in sorghum.periods}
        self.assertEqual(
            {name for name, period in periods.items() if period.planting_allowed},
            {"MID_SPRING", "LATE_SPRING"},
        )
        self.assertEqual(
            {name for name, period in periods.items() if period.harvest_allowed},
            {"LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN"},
        )
        self.assertEqual(periods["MID_SPRING"].transitions,
                         (("INVISIBLE", "GREENSMALL"),))
        self.assertEqual(periods["LATE_SPRING"].transitions,
                         (("INVISIBLE", "GREENSMALL"), ("GREENSMALL", "GREENMIDDLE")))
        self.assertEqual(periods["EARLY_SUMMER"].transitions,
                         (("GREENSMALL", "GREENMIDDLE"), ("GREENMIDDLE", "GREENBIG")))
        self.assertEqual(periods["MID_SUMMER"].transitions,
                         (("GREENMIDDLE", "GREENBIG"), ("GREENBIG", "GREENBIG")))
        self.assertEqual(periods["LATE_SUMMER"].transitions,
                         (("GREENBIG", "HARVESTREADY"),))
        for name in ("EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN"):
            self.assertEqual(periods[name].transitions,
                             (("HARVESTREADY", "HARVESTREADY"),))
        self.assertEqual(periods["EARLY_WINTER"].transitions,
                         (("HARVESTREADY", "DEAD"),))
        self.assertTrue(all(period.growth_time is None for period in periods.values()))
        self.assertTrue(all(not period.transitions for name, period in periods.items()
                            if name in {"EARLY_SPRING", "MID_WINTER", "LATE_WINTER"}))

    def test_full_policy_has_requested_windows_and_runtime_names(self):
        policy = parse_policy(__import__("pathlib").Path("mods/SiN_FS25_Crop_Settings/config/fruit-policy.xml"))
        expected = {
            "BARLEY": ({"EARLY_AUTUMN", "MID_AUTUMN"}, {"EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER"}),
            "CANOLA": ({"LATE_SUMMER", "EARLY_AUTUMN"}, {"EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER"}),
            "CARROT": ({"MID_SPRING", "LATE_SPRING"}, {"LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN"}),
            "MAIZE": ({"MID_SPRING", "LATE_SPRING"}, {"EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN", "EARLY_WINTER"}),
            "COTTON": ({"EARLY_SPRING", "MID_SPRING"}, {"EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN", "EARLY_WINTER"}),
            "GRAPE": ({"EARLY_SPRING", "MID_SPRING"}, {"LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN"}),
            "GRASS": ({"EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER", "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN"}, {"EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER", "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN", "EARLY_WINTER"}),
            "GREENBEAN": ({"MID_SPRING", "LATE_SPRING"}, {"MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN"}),
            "RICELONGGRAIN": ({"MID_SPRING", "LATE_SPRING"}, {"LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN"}),
            "OAT": ({"EARLY_SPRING", "MID_SPRING"}, {"MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN"}),
            "OILSEEDRADISH": ({"EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER", "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN"}, set()),
            "OLIVE": ({"EARLY_SPRING", "MID_SPRING"}, {"EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN", "EARLY_WINTER"}),
            "PARSNIP": ({"MID_SPRING", "LATE_SPRING"}, {"LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN"}),
            "PEA": ({"EARLY_SPRING", "MID_SPRING"}, {"EARLY_SUMMER", "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN"}),
            "POPLAR": ({"EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER", "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN"}, {"EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER", "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN", "EARLY_WINTER"}),
            "POTATO": ({"EARLY_SPRING", "MID_SPRING"}, {"MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN"}),
            "BEETROOT": ({"MID_SPRING", "LATE_SPRING"}, {"LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN"}),
            "RICE": ({"MID_SPRING", "LATE_SPRING"}, {"LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN"}),
            "SORGHUM": ({"MID_SPRING", "LATE_SPRING"}, {"LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN"}),
            "SOYBEAN": ({"MID_SPRING", "LATE_SPRING"}, {"EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN", "EARLY_WINTER"}),
            "SPINACH": ({"EARLY_SPRING", "MID_SPRING"}, {"EARLY_SUMMER", "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN"}),
            "SUGARBEET": ({"EARLY_SPRING", "MID_SPRING"}, {"EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN", "EARLY_WINTER"}),
            "SUGARCANE": ({"EARLY_SPRING", "MID_SPRING"}, {"EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER", "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN", "EARLY_WINTER"}),
            "SUNFLOWER": ({"MID_SPRING", "LATE_SPRING"}, {"LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN"}),
            "WHEAT": ({"MID_AUTUMN", "LATE_AUTUMN"}, {"MID_SPRING", "LATE_SPRING", "EARLY_SUMMER", "MID_SUMMER"}),
        }
        for fruit in policy.fruits:
            periods = {period.name: period for period in fruit.periods}
            self.assertEqual(
                {name for name, period in periods.items() if period.planting_allowed}, expected[fruit.name][0], fruit.name)
            self.assertEqual(
                {name for name, period in periods.items() if period.harvest_allowed}, expected[fruit.name][1], fruit.name)

    def test_all_annual_crops_reach_ready_only_in_their_harvest_window(self):
        policy = parse_policy(__import__("pathlib").Path("mods/SiN_FS25_Crop_Settings/config/fruit-policy.xml"))
        period_names = ("EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER",
                        "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN",
                        "LATE_AUTUMN", "EARLY_WINTER", "MID_WINTER", "LATE_WINTER")
        for fruit in policy.fruits:
            if fruit.lifecycle != "ANNUAL":
                continue
            tokens = ("INVISIBLE", *fruit.state_chain, "DEAD")
            state_ids = {token: index for index, token in enumerate(tokens)}
            descriptor = {
                "name": fruit.name,
                "nameToGrowthState": state_ids,
                "growthDataSeasonal": {"periods": {
                    name: {"plantingAllowed": False, "isHarvestable": False,
                           "growthMapping": {99: 98}} for name in period_names
                }},
            }
            result = apply_policy(policy, [descriptor])
            self.assertEqual(result.unsupported, 0, fruit.name)
            periods = descriptor["growthDataSeasonal"]["periods"]
            terminal = state_ids[fruit.state_chain[-1]]
            preterminal = state_ids[fruit.state_chain[-2]]
            harvest_names = {period.name for period in fruit.periods if period.harvest_allowed}
            self.assertTrue(harvest_names, fruit.name)
            for name in period_names:
                mapping = periods[name]["growthMapping"]
                if name in harvest_names:
                    self.assertEqual(mapping.get(preterminal), terminal, fruit.name)
                    self.assertEqual(mapping.get(terminal), terminal, fruit.name)
                else:
                    self.assertNotEqual(mapping.get(preterminal), terminal, fruit.name)

    def test_annual_lifecycle_withholds_maturity_until_harvest_window(self):
        policy = parse_policy('''
          <cropPolicy schemaVersion="1" policyVersion="lifecycle-1"
              defaultPlantingAllowed="false" defaultHarvestAllowed="false"><fruits>
            <fruit name="WHEAT" enabled="true" plantPeriods="MID_SPRING,LATE_SPRING"
                harvestPeriods="LATE_SUMMER,EARLY_AUTUMN">
              <growth preserveNative="true" lifecycle="annual"
                  stateChain="greenSmall,greenMiddle,greenBig,harvestReady"><seasonal/></growth>
            </fruit></fruits></cropPolicy>''')
        names = ("EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER",
                 "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN",
                 "LATE_AUTUMN", "EARLY_WINTER", "MID_WINTER", "LATE_WINTER")
        descriptor = {
            "name": "WHEAT",
            "growthStateIds": {"INVISIBLE": 0, "GREENSMALL": 1, "GREENMIDDLE": 2,
                                "GREENBIG": 3, "HARVESTREADY": 4, "DEAD": 5},
            "growthDataSeasonal": {"periods": {
                name: {"plantingAllowed": False, "isHarvestable": False,
                       "growthMapping": {99: 98}} for name in names}},
        }
        result = apply_policy(policy, [descriptor])
        self.assertEqual(result.unsupported, 0)
        periods = descriptor["growthDataSeasonal"]["periods"]
        self.assertNotIn(4, periods["MID_SUMMER"]["growthMapping"])
        self.assertEqual(periods["LATE_SUMMER"]["growthMapping"][3], 4)
        self.assertEqual(periods["EARLY_AUTUMN"]["growthMapping"][4], 4)
        self.assertEqual(periods["MID_AUTUMN"]["growthMapping"], {4: 5})

    def test_annual_lifecycle_uses_native_path_and_omits_optional_visual_states(self):
        policy = parse_policy(__import__("pathlib").Path("mods/SiN_FS25_Crop_Settings/config/fruit-policy.xml"))
        names = ("EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER",
                 "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN",
                 "LATE_AUTUMN", "EARLY_WINTER", "MID_WINTER", "LATE_WINTER")
        # Native maps are allowed to skip visual states.  This is the native
        # sunflower path used by the base regional definitions.
        ids = {"INVISIBLE": 0, "GREENSMALL": 1, "GREENSMALL2": 2,
               "GREENMIDDLE": 3, "GREENMIDDLE2": 4, "GREENBIG": 5,
               "GREENBIG2": 6, "HARVESTREADY": 7, "DEAD": 8}
        descriptor = {
            "name": "SUNFLOWER",
            "growthStateIds": ids,
            "growthDataSeasonal": {"periods": {
                name: {"plantingAllowed": False, "isHarvestable": False,
                       "growthMapping": {}} for name in names
            }},
        }
        descriptor["growthDataSeasonal"]["periods"]["LATE_SPRING"]["growthMapping"] = {1: 3}
        descriptor["growthDataSeasonal"]["periods"]["EARLY_SUMMER"]["growthMapping"] = {3: 5}
        descriptor["growthDataSeasonal"]["periods"]["MID_SUMMER"]["growthMapping"] = {5: 7}
        descriptor["growthDataSeasonal"]["periods"]["MID_AUTUMN"]["growthMapping"] = {7: 8}
        result = apply_policy(policy, [descriptor])
        self.assertEqual(result.unsupported, 0)
        periods = descriptor["growthDataSeasonal"]["periods"]
        self.assertEqual(periods["MID_SUMMER"]["growthMapping"], {1: 3, 3: 5})
        self.assertEqual(periods["LATE_SUMMER"]["growthMapping"], {1: 3, 3: 5, 5: 7, 7: 7})
        self.assertNotIn(2, periods["MID_SUMMER"]["growthMapping"])
        self.assertNotIn(4, periods["MID_SUMMER"]["growthMapping"])

    def test_annual_lifecycle_accepts_map_without_optional_maize_state(self):
        policy = parse_policy(__import__("pathlib").Path("mods/SiN_FS25_Crop_Settings/config/fruit-policy.xml"))
        names = ("EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER",
                 "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN",
                 "LATE_AUTUMN", "EARLY_WINTER", "MID_WINTER", "LATE_WINTER")
        # This map has no harvestReadyGreen2 state and uses the direct native
        # harvestReadyGreen -> harvestReady3 transition.
        ids = {"INVISIBLE": 0, "GREENSMALL": 1, "GREENMIDDLE": 2,
               "GREENBIG": 3, "HARVESTREADYGREEN": 4, "HARVESTREADY3": 5,
               "DEAD": 6}
        descriptor = {
            "name": "MAIZE",
            "growthStateIds": ids,
            "growthDataSeasonal": {"periods": {
                name: {"plantingAllowed": False, "isHarvestable": False,
                       "growthMapping": {}} for name in names
            }},
        }
        descriptor["growthDataSeasonal"]["periods"]["LATE_SPRING"]["growthMapping"] = {1: 2}
        descriptor["growthDataSeasonal"]["periods"]["EARLY_SUMMER"]["growthMapping"] = {2: 3}
        descriptor["growthDataSeasonal"]["periods"]["MID_SUMMER"]["growthMapping"] = {3: 4}
        descriptor["growthDataSeasonal"]["periods"]["LATE_SUMMER"]["growthMapping"] = {4: 5}
        descriptor["growthDataSeasonal"]["periods"]["LATE_AUTUMN"]["growthMapping"] = {5: 6}
        result = apply_policy(policy, [descriptor])
        self.assertEqual(result.unsupported, 0)
        periods = descriptor["growthDataSeasonal"]["periods"]
        self.assertEqual(periods["EARLY_AUTUMN"]["growthMapping"], {1: 2, 2: 3, 3: 4, 4: 5, 5: 5})

    def test_annual_lifecycle_fails_closed_before_partial_mapping_mutation(self):
        policy = parse_policy('''
          <cropPolicy schemaVersion="1" policyVersion="lifecycle-unsupported"
              defaultPlantingAllowed="false" defaultHarvestAllowed="false"><fruits>
            <fruit name="WHEAT" enabled="true" plantPeriods="MID_SPRING"
                harvestPeriods="LATE_SUMMER">
              <growth preserveNative="true" lifecycle="annual"
                  stateChain="greenSmall,greenMiddle,greenBig,harvestReady"><seasonal/></growth>
            </fruit></fruits></cropPolicy>''')
        names = ("EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER",
                 "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN",
                 "LATE_AUTUMN", "EARLY_WINTER", "MID_WINTER", "LATE_WINTER")
        descriptor = {
            "name": "WHEAT",
            "growthStateIds": {"INVISIBLE": 0, "GREENSMALL": 1, "GREENMIDDLE": 2,
                                "GREENBIG": 3, "HARVESTREADY": 4, "DEAD": 5},
            "growthDataSeasonal": {"periods": {
                name: {"plantingAllowed": False, "isHarvestable": False,
                       "growthMapping": {99: 98}} for name in names
            }},
        }
        del descriptor["growthDataSeasonal"]["periods"]["LATE_WINTER"]["growthMapping"]
        before = repr(descriptor)
        result = apply_policy(policy, [descriptor])
        self.assertGreaterEqual(result.unsupported, 1)
        self.assertEqual(repr(descriptor), before)

    def test_perennial_and_oilseed_entries_preserve_native_growth_mapping(self):
        policy = parse_policy(__import__("pathlib").Path("mods/SiN_FS25_Crop_Settings/config/fruit-policy.xml"))
        for name in ("GRASS", "POPLAR", "SUGARCANE", "OILSEEDRADISH"):
            fruit = next(item for item in policy.fruits if item.name == name)
            descriptor = {"name": name, "growthDataSeasonal": {"periods": {
                period.name: {"plantingAllowed": False, "isHarvestable": False,
                              "growthMapping": {1: 2}} for period in fruit.periods}}}
            result = apply_policy(policy, [descriptor])
            self.assertEqual(result.unsupported, 0)
            self.assertTrue(all(period["growthMapping"] == {1: 2}
                                for period in descriptor["growthDataSeasonal"]["periods"].values()))

    def test_shipped_policy_uses_native_growth_shape_and_harvest_flags(self):
        policy = parse_policy(__import__("pathlib").Path("mods/SiN_FS25_Crop_Settings/config/fruit-policy.xml"))
        periods = ("EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER",
                   "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN",
                   "LATE_AUTUMN", "EARLY_WINTER", "MID_WINTER", "LATE_WINTER")
        descriptor = {
            "name": "SORGHUM",
            "nameToGrowthState": {"INVISIBLE": 0, "GREENSMALL": 1,
                                   "GREENMIDDLE": 2, "GREENBIG": 3,
                                   "HARVESTREADY": 4, "DEAD": 5},
            "growthDataSeasonal": {"periods": {
                name: {"plantingAllowed": False, "isHarvestable": False,
                       "growthMapping": {3: 4, 4: 5}}
                for name in periods
            }},
        }
        result = apply_policy(policy, [descriptor])
        self.assertEqual((result.applied, result.unsupported), (1, 0))
        native_periods = descriptor["growthDataSeasonal"]["periods"]
        self.assertEqual(
            {name for name, value in native_periods.items() if value["plantingAllowed"]},
            {"MID_SPRING", "LATE_SPRING"},
        )
        self.assertEqual(
            {name for name, value in native_periods.items() if value["isHarvestable"]},
            {"LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN"},
        )
        self.assertEqual(native_periods["MID_SUMMER"]["growthMapping"][3], 3)
        self.assertEqual(native_periods["EARLY_WINTER"]["growthMapping"][4], 5)

    def test_sorghum_harvest_gate_and_required_wither_transition(self):
        policy = parse_policy(__import__("pathlib").Path("mods/SiN_FS25_Crop_Settings/config/fruit-policy.xml"))
        descriptor = {
            "name": "sorghum",
            "growthStateIds": {"INVISIBLE": 0, "GREENSMALL": 1,
                                "GREENMIDDLE": 2, "GREENBIG": 3,
                                "HARVESTREADY": 4, "DEAD": 5},
            "growthDataSeasonal": {"periods": {
                name: {"plantingAllowed": name in {"MID_SPRING", "LATE_SPRING"},
                       "growthMapping": {2: 3, 3: 4}}
                for name in ("EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER",
                             "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN",
                             "LATE_AUTUMN", "EARLY_WINTER", "MID_WINTER", "LATE_WINTER")
            }},
            "harvestableInPeriod": {name: name in {"LATE_SUMMER", "EARLY_AUTUMN"}
                                    for name in ("EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER",
                                                 "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN",
                                                 "LATE_AUTUMN", "EARLY_WINTER", "MID_WINTER", "LATE_WINTER")},
        }
        original_mapping = {name: dict(period["growthMapping"])
                             for name, period in descriptor["growthDataSeasonal"]["periods"].items()}
        result = apply_policy(policy, [descriptor])
        self.assertEqual((result.applied, result.unsupported), (1, 0))
        self.assertEqual(
            {name for name, allowed in descriptor["harvestableInPeriod"].items() if allowed},
            {"LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN", "LATE_AUTUMN"},
        )
        self.assertEqual(
            {name for name, period in descriptor["growthDataSeasonal"]["periods"].items()
             if period["plantingAllowed"]},
            {"MID_SPRING", "LATE_SPRING"},
        )
        actual_mapping = {name: dict(period["growthMapping"])
                          for name, period in descriptor["growthDataSeasonal"]["periods"].items()}
        expected_mapping = {name: dict(mapping) for name, mapping in original_mapping.items()}
        expected_mapping["MID_SPRING"][0] = 1
        expected_mapping["LATE_SPRING"][0] = 1
        expected_mapping["LATE_SPRING"][1] = 2
        expected_mapping["EARLY_SUMMER"][1] = 2
        expected_mapping["EARLY_SUMMER"][2] = 3
        expected_mapping["MID_SUMMER"][2] = 3
        expected_mapping["MID_SUMMER"][3] = 3
        expected_mapping["EARLY_AUTUMN"][4] = 4
        expected_mapping["MID_AUTUMN"][4] = 4
        expected_mapping["LATE_AUTUMN"][4] = 4
        expected_mapping["EARLY_WINTER"][4] = 5
        self.assertEqual(expected_mapping, actual_mapping)

    def test_sorghum_growth_transitions_align_ready_window_and_winter_withering(self):
        policy = parse_policy(__import__("pathlib").Path("mods/SiN_FS25_Crop_Settings/config/fruit-policy.xml"))
        descriptor = {
            "name": "SORGHUM",
            "growthStateIds": {"INVISIBLE": 0, "GREENSMALL": 1,
                                "GREENMIDDLE": 2, "GREENBIG": 3,
                                "HARVESTREADY": 4, "DEAD": 5},
            "growthDataSeasonal": {"periods": {
                "MID_SUMMER": {
                    "plantingAllowed": False,
                    "growthMapping": {3: 4},
                },
                "EARLY_AUTUMN": {
                    "plantingAllowed": False,
                    "growthMapping": {4: 5},
                },
                "MID_AUTUMN": {"plantingAllowed": False, "growthMapping": {}},
                "LATE_AUTUMN": {"plantingAllowed": False, "growthMapping": {}},
                "EARLY_WINTER": {"plantingAllowed": False, "growthMapping": {4: 4}},
            }},
            "harvestableInPeriod": {"EARLY_AUTUMN": False},
        }
        result = apply_policy(policy, [descriptor])
        self.assertEqual(result.unsupported, 0)
        self.assertEqual(descriptor["growthDataSeasonal"]["periods"]["MID_SUMMER"]["growthMapping"], {2: 3, 3: 3})
        self.assertEqual(descriptor["growthDataSeasonal"]["periods"]["EARLY_AUTUMN"]["growthMapping"], {4: 4})
        self.assertEqual(descriptor["growthDataSeasonal"]["periods"]["MID_AUTUMN"]["growthMapping"], {4: 4})
        self.assertEqual(descriptor["growthDataSeasonal"]["periods"]["LATE_AUTUMN"]["growthMapping"], {4: 4})
        self.assertEqual(descriptor["growthDataSeasonal"]["periods"]["EARLY_WINTER"]["growthMapping"], {4: 5})

    def test_named_growth_state_resolution_matches_native_casing(self):
        policy = parse_policy(policy_xml('''
          <fruit name="SORGHUM" enabled="true"><seasonal>
            <period name="EARLY_AUTUMN"><growth>
              <update startState="harvestReady" endState="harvestReady"/>
            </growth></period>
          </seasonal></fruit>
        '''))
        descriptor = {
            "name": "SORGHUM",
            "growthStateIds": {"harvestReady": 4},
            "growthDataSeasonal": {"periods": {
                "EARLY_AUTUMN": {"growthMapping": {4: 5}},
            }},
        }
        result = apply_policy(policy, [descriptor])
        self.assertEqual(result.unsupported, 0)
        self.assertEqual(descriptor["growthDataSeasonal"]["periods"]["EARLY_AUTUMN"]["growthMapping"], {4: 4})

    def test_harvest_method_policy_keeps_native_readiness_boundary(self):
        policy = parse_policy(__import__("pathlib").Path("mods/SiN_FS25_Crop_Settings/config/fruit-policy.xml"))
        native = lambda _growth_mode, period: period == "LATE_SUMMER"
        descriptor = {"name": "SORGHUM", "growthDataSeasonal": {"periods": {
            name: {"plantingAllowed": False, "growthMapping": {}} for name in (
                "EARLY_SPRING", "MID_SPRING", "LATE_SPRING", "EARLY_SUMMER",
                "MID_SUMMER", "LATE_SUMMER", "EARLY_AUTUMN", "MID_AUTUMN",
                "LATE_AUTUMN", "EARLY_WINTER", "MID_WINTER", "LATE_WINTER")
        }}, "growthStateIds": {"INVISIBLE": 0, "GREENSMALL": 1,
                                 "GREENMIDDLE": 2, "GREENBIG": 3,
                                 "HARVESTREADY": 4, "DEAD": 5},
        "getIsHarvestableInPeriod": native}
        descriptor["growthDataSeasonal"]["periods"]["EARLY_AUTUMN"]["growthMapping"] = {4: 5}
        result = apply_policy(policy, [descriptor])
        self.assertEqual(result.unsupported, 0)
        self.assertEqual(descriptor["_sin_crop_harvest_policy"]["LATE_AUTUMN"], True)
        # The policy changes the period gate; native maturity/readiness remains
        # a separate runtime check and is not rewritten by this layer.
        self.assertTrue(native(0, "LATE_SUMMER"))
        self.assertFalse(native(0, "LATE_AUTUMN"))

    def test_other_fruit_is_unchanged_by_sorghum_policy(self):
        policy = parse_policy(__import__("pathlib").Path("mods/SiN_FS25_Crop_Settings/config/fruit-policy.xml"))
        wheat = {"name": "ONION", "growthDataSeasonal": {"periods": {
            "MID_SPRING": {"plantingAllowed": False, "growthMapping": {2: 3}}
        }}}
        before = repr(wheat)
        result = apply_policy(policy, [wheat])
        self.assertEqual(result.applied, 0)
        self.assertEqual(repr(wheat), before)
