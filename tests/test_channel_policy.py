import unittest

from fs25_network_core.channel_policy import COMMAND_CHANNELS, require_command_channel


class ChannelPolicyTests(unittest.TestCase):
    def test_each_command_accepts_its_designated_channel(self):
        channels = {"link_account": 11, "bank": 12, "farm_approvals": 13}
        for command, channel_key in COMMAND_CHANNELS.items():
            with self.subTest(command=command):
                require_command_channel(command, 1, 1, channels[channel_key], channels)
                with self.assertRaises(ValueError):
                    require_command_channel(command, 1, 1, 99, channels)

    def test_rejects_dm_other_guild_missing_mapping_and_unknown_command(self):
        for command, guild, channels in [("balance", None, {"bank": 12}),
                                         ("balance", 2, {"bank": 12}),
                                         ("balance", 1, {}), ("unknown", 1, {"bank": 12})]:
            with self.subTest(command=command, guild=guild, channels=channels):
                with self.assertRaises(ValueError):
                    require_command_channel(command, guild, 1, 12, channels)
