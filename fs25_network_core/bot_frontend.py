"""Run with python -m fs25_network_core.bot_frontend."""
import asyncio
import json
import logging
import os

import discord
from discord import app_commands

from .banking_engine import BankingEngine
from .database import Database
from .authorization import AuthorizationManager, require_operator, ROLES
from .server_scraper import ServerScraper
from .config import load_local_environment, required_setting, PROJECT_ROOT
from .channel_policy import require_command_channel


class DiscordSetupError(RuntimeError):
    """An actionable installation error safe to display without credentials."""


def player_choice_label(player_id, nickname):
    return f"Nickname: {nickname or 'Unnamed player'} | FS25 player ID: {player_id}"


class NetworkBot(discord.Client):
    def __init__(self, bank, servers, guild_id, operator_role_ids=(), channels=None, authorizations=None):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.bank, self.servers = bank, servers
        self.guild = discord.Object(id=guild_id)
        self.channels = {name: int(value) for name, value in (channels or {}).items()}
        self.tree = app_commands.CommandTree(self)
        self.authorization = AuthorizationManager(bank.database)
        self.authorizations = authorizations or {}
        role_override = os.environ.get("DISCORD_OPERATOR_ROLE_IDS")
        self.operator_role_ids = (
            {int(value.strip()) for value in role_override.split(",") if value.strip()}
            if role_override is not None else {int(value) for value in operator_role_ids}
        )

        async def channel_check(interaction):
            try:
                require_command_channel(interaction.command.name, interaction.guild_id,
                                        self.guild.id, interaction.channel_id, self.channels)
            except ValueError as error:
                raise app_commands.CheckFailure(str(error)) from None
            return True

        def server_config(interaction, server):
            if interaction.guild_id != self.guild.id:
                raise ValueError("Use this command in the configured Discord server")
            if server not in self.servers:
                raise ValueError("Unknown game server")
            return self.servers[server]

        def auth_for(server):
            return self.authorizations.get(server, self.authorization)

        def staff_check(interaction):
            require_operator(interaction.guild_id, self.guild.id,
                             [item.id for item in getattr(interaction.user, "roles", [])], self.operator_role_ids)

        async def observed_players(interaction: discord.Interaction, current: str):
            server = getattr(interaction.namespace, "server", None)
            if not server or server not in self.servers:
                return []
            try:
                snapshot = await asyncio.to_thread(ServerScraper(self.servers).snapshot, server)
            except ValueError:
                return []
            choices = []
            for player_id, name in snapshot["players"].items():
                label = player_choice_label(player_id, name)
                if current.lower() in label.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=player_id))
            return choices[:25]

        async def observed_farms(interaction: discord.Interaction, current: str):
            server = getattr(interaction.namespace, "server", None)
            if not server or server not in self.servers:
                return []
            try:
                snapshot = await asyncio.to_thread(ServerScraper(self.servers).snapshot, server)
            except ValueError:
                return []
            choices = []
            for farm_id, name in snapshot["farms"].items():
                if not name.strip():
                    continue
                label = f"Farm {farm_id} ? {name}"
                if current.lower() in label.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=str(farm_id)))
            return choices[:25]

        async def pending_requesters(interaction: discord.Interaction, current: str):
            server = getattr(interaction.namespace, "server", None)
            if not server or server not in self.servers:
                return []
            try:
                records = await asyncio.to_thread(auth_for(server).requests, server, self.servers[server]["save_id"])
            except ValueError:
                return []
            choices = []
            for record in records:
                display_name = record["discord_id"]
                try:
                    member = await interaction.guild.fetch_member(int(record["discord_id"]))
                    if not member.bot:
                        display_name = member.display_name
                except (AttributeError, TypeError, ValueError, discord.HTTPException):
                    pass
                label = f"Requester: {display_name} | Farm: {record['farm_name']}"
                if current.lower() in label.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=record["discord_id"]))
            return choices[:25]

        @self.tree.command(name="farm_request", description="Request a farm and starting field for staff review")
        @app_commands.check(channel_check)
        async def farm_request(interaction: discord.Interaction, server: str, farm_name: str, starting_field: str):
            config = server_config(interaction, server)
            await interaction.response.defer(ephemeral=True)
            record = await asyncio.to_thread(auth_for(server).request_farm, str(interaction.user.id), server,
                                            config["save_id"], farm_name, starting_field)
            await interaction.followup.send(
                f"Your request for **{record['farm_name']}** at **{record['starting_field']}** is pending staff review. "
                "Staff will create the farm and confirm your in-game identity. Repeated submissions keep this request.",
                ephemeral=True)

        @self.tree.command(name="farm_requests", description="Staff: list pending farm requests")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def farm_requests(interaction: discord.Interaction, server: str):
            staff_check(interaction)
            config = server_config(interaction, server)
            await interaction.response.defer(ephemeral=True)
            records = await asyncio.to_thread(auth_for(server).requests, server, config["save_id"])
            rows = [f"<@{r['discord_id']}> requested **{r['farm_name']}**; starting field: **{r['starting_field']}**. "
                    "Use `/farm_approve` and select this member." for r in records]
            for row in rows or ["No pending requests."]:
                await interaction.followup.send(row, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

        @self.tree.command(name="farm_roster", description="Staff: inspect players and farms reported by the local mod")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def farm_roster(interaction: discord.Interaction, server: str):
            staff_check(interaction)
            server_config(interaction, server)
            await interaction.response.defer(ephemeral=True)
            snapshot = await asyncio.to_thread(ServerScraper(self.servers).snapshot, server)
            rows = [f"Farm {farm_id}: {name or '(unnamed; cannot approve)'}" for farm_id, name in snapshot["farms"].items()]
            rows += [f"Player ID: {player_id} | name: {name}" for player_id, name in snapshot["players"].items()]
            for row in rows or ["No farms or players reported."]:
                await interaction.followup.send(row[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

        @self.tree.command(name="farm_approve", description="Staff: associate a request with an observed player and created farm")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def farm_approve(interaction: discord.Interaction, server: str, member: str,
                               farm_id: str, player_id: str, identity_and_land_confirmed: bool):
            staff_check(interaction)
            config = server_config(interaction, server)
            await interaction.response.defer(ephemeral=True)
            snapshot = await asyncio.to_thread(ServerScraper(self.servers).snapshot, server)
            request = await asyncio.to_thread(auth_for(server).pending_request_for_user, member, server, config["save_id"])
            try:
                selected_farm_id = int(farm_id)
            except ValueError:
                raise ValueError("Choose a farm from the suggestions") from None
            operation = await asyncio.to_thread(auth_for(server).approve_request, request["_id"], server, config["save_id"],
                selected_farm_id, player_id, snapshot, str(interaction.user.id), identity_and_land_confirmed)
            await interaction.followup.send(
                f"Approved requester `{member}` for **{request['farm_name']}**. "
                f"Permission sync is pending (operation `{operation}`). The game has not applied permissions yet.",
                ephemeral=True)

        @farm_approve.autocomplete("farm_id")
        async def farm_approve_farm_autocomplete(interaction: discord.Interaction, current: str):
            return await observed_farms(interaction, current)

        @farm_approve.autocomplete("player_id")
        async def farm_approve_player_autocomplete(interaction: discord.Interaction, current: str):
            return await observed_players(interaction, current)

        @farm_approve.autocomplete("member")
        async def farm_approve_member_autocomplete(interaction: discord.Interaction, current: str):
            return await pending_requesters(interaction, current)

        @self.tree.command(name="farm_reject", description="Staff: reject a pending farm request with a reason")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def farm_reject(interaction: discord.Interaction, server: str, member: str, reason: str):
            staff_check(interaction)
            config = server_config(interaction, server)
            await interaction.response.defer(ephemeral=True)
            request = await asyncio.to_thread(auth_for(server).pending_request_for_user, member, server, config["save_id"])
            await asyncio.to_thread(auth_for(server).reject_request, request["_id"], server, config["save_id"], str(interaction.user.id), reason)
            await interaction.followup.send(f"Farm request for requester `{member}` rejected.", ephemeral=True)

        @farm_reject.autocomplete("member")
        async def farm_reject_member_autocomplete(interaction: discord.Interaction, current: str):
            return await pending_requesters(interaction, current)

        @self.tree.command(name="farm_status", description="View your farm assignment and permission sync state")
        @app_commands.check(channel_check)
        async def farm_status(interaction: discord.Interaction, server: str):
            config = server_config(interaction, server)
            await interaction.response.defer(ephemeral=True)
            authorization = auth_for(server)
            record = await asyncio.to_thread(authorization.status, str(interaction.user.id), server, config["save_id"])
            message = "No farm assignment yet. Submit /farm_request for staff review."
            if not record:
                from .authorization import key
                request = await asyncio.to_thread(authorization.db.farm_requests.find_one,
                                                 {"_id": key(server, config["save_id"], str(interaction.user.id))})
                if request:
                    message = f"Farm request: {request['state']}."
                    if request.get("reason"):
                        message += f" Reason: {request['reason']}"
            if record:
                message = f"Farm ID: {record['farm_id']}; requested role: {record['desired_role']}; applied role: {record['applied_role'] or 'none'}; sync: {record['state']}."
            await interaction.followup.send(message, ephemeral=True)

        @self.tree.command(name="farm_assign", description="Staff: assign or revoke a verified player's farm role")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        @app_commands.choices(role=[app_commands.Choice(name=value, value=value) for value in sorted(ROLES)])
        async def farm_assign(interaction: discord.Interaction, member: discord.Member, server: str,
                              farm_id: app_commands.Range[int, 1], role: app_commands.Choice[str]):
            config = server_config(interaction, server)
            require_operator(interaction.guild_id, self.guild.id,
                             [item.id for item in getattr(interaction.user, "roles", [])], self.operator_role_ids)
            await interaction.response.defer(ephemeral=True)
            farms = await asyncio.to_thread(ServerScraper(self.servers).farms, server)
            operation = await asyncio.to_thread(auth_for(server).assign, str(member.id), server,
                config["save_id"], farm_id, role.value, farms, str(interaction.user.id))
            await interaction.followup.send(f"Permission operation `{operation}` queued. It becomes active after the server mod confirms it.", ephemeral=True)

        @self.tree.command(name="balance", description="View your central available balance")
        @app_commands.check(channel_check)
        async def balance(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            value = await asyncio.to_thread(self.bank.balance, str(interaction.user.id))
            await interaction.followup.send(f"Available balance: {value:,}", ephemeral=True)

        @self.tree.command(name="deposit", description="Learn how to credit a verified game transfer")
        @app_commands.check(channel_check)
        async def deposit(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000_000]):
            await interaction.response.send_message(
                f"To deposit {amount:,}, transfer funds to your server's designated bank farm and give an operator the transfer ID. "
                "Your wallet is credited after the sender and completed transfer are verified. This command does not credit funds.", ephemeral=True)

        @self.tree.command(name="withdraw", description="Reserve funds for delivery to your approved farm")
        @app_commands.check(channel_check)
        async def withdraw(interaction: discord.Interaction, server: str, amount: app_commands.Range[int, 1, 1_000_000_000]):
            config = self.servers.get(server)
            if not config or not config.get("withdrawals_enabled", False):
                await interaction.response.send_message("Withdrawals are not enabled for this server.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            state = await asyncio.to_thread(self.bank.request_withdrawal, str(interaction.id),
                                           str(interaction.user.id), server, config["save_id"], amount)
            await interaction.followup.send(f"Withdrawal {interaction.id}: {state}. Pending funds are reserved until delivery is confirmed.", ephemeral=True)

        @self.tree.error
        async def on_error(interaction, error):
            original = getattr(error, "original", error)
            expected_error = isinstance(original, (ValueError, app_commands.CheckFailure))
            message = str(original) if expected_error else "Request could not be confirmed. Check your balance and contact an operator before retrying."
            if not expected_error:
                logging.exception("Discord command failed", exc_info=original)
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    async def setup_hook(self):
        self.tree.copy_global_to(guild=self.guild)
        try:
            commands = await self.tree.sync(guild=self.guild)
        except discord.Forbidden:
            invite = discord.utils.oauth_url(
                self.application_id, permissions=discord.Permissions(view_channel=True, send_messages=True),
                guild=self.guild, scopes=("bot", "applications.commands"), disable_guild_select=True)
            raise DiscordSetupError(
                f"Discord accepted the token, but application {self.application_id} cannot register commands "
                f"in server {self.guild.id}. Verify the server ID and install this application using "
                f"Guild Install with bot and applications.commands scopes.\n"
                f"Open this invite in your browser, authorize it for your server, then restart:\n{invite}"
            ) from None
        logging.info("Registered %s commands in Discord server %s", len(commands), self.guild.id)

    async def on_ready(self):
        logging.info("Discord bot connected as %s (ID %s)", self.user, self.user.id)


def main():
    logging.basicConfig(level=logging.INFO)
    load_local_environment()
    try:
        token = required_setting("DISCORD_TOKEN")
        required_setting("MONGODB_URI")
    except ValueError as error:
        raise SystemExit(str(error)) from None
    with open(os.environ.get("FS25_DISCORD_FILE", PROJECT_ROOT / "discord.json"), encoding="utf-8") as stream:
        discord_config = json.load(stream)
    guild_id = os.environ.get("DISCORD_GUILD_ID") or discord_config["guild_id"]
    # Do not reuse another guild's operator configuration when overriding guild ID.
    operator_role_ids = discord_config.get("operator_role_ids", []) if str(guild_id) == str(discord_config["guild_id"]) else []
    channels = discord_config.get("channels", {}) if str(guild_id) == str(discord_config["guild_id"]) else {}
    with open(os.environ.get("FS25_SERVERS_FILE", PROJECT_ROOT / "servers.json"), encoding="utf-8") as stream:
        servers = json.load(stream)
    database = Database()
    database.initialize()
    authorizations = {}
    for server_id, config in servers.items():
        if config.get("development"):
            test_name = "fs25_network_local_test"
            if database.db.name == test_name:
                raise SystemExit("The central database must differ from fs25_network_local_test")
            if config.get("withdrawals_enabled"):
                raise SystemExit("Withdrawals must remain disabled for development servers")
            test_database = Database(name=test_name)
            test_database.initialize()
            authorizations[server_id] = AuthorizationManager(test_database)
    try:
        NetworkBot(BankingEngine(database), servers, int(guild_id), operator_role_ids, channels, authorizations).run(token, log_handler=None)
    except DiscordSetupError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
