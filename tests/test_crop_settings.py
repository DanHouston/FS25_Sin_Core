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
