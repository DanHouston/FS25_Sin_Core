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
from .community import CommunityApplications
from .server_registry import ServerRegistry
from .activity import ActivityPublisher
from .farm_lifecycle import FarmLifecycle, SYSTEM_FARM_NAME


class DiscordSetupError(RuntimeError):
    """An actionable installation error safe to display without credentials."""


def player_choice_label(player_id, nickname):
    return f"Nickname: {nickname or 'Unnamed player'} | FS25 player ID: {player_id}"


def approved_nickname(player_name, farm_name):
    return f"{player_name or 'Player'} | {farm_name}"[:32]


class NetworkBot(discord.Client):
    def __init__(self, bank, servers, guild_id, operator_role_ids=(), channels=None, authorizations=None, sin_member_role_id=None):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents)
        self.bank, self.servers = bank, servers
        self.guild = discord.Object(id=guild_id)
        self.channels = {name: int(value) for name, value in (channels or {}).items()}
        self.tree = app_commands.CommandTree(self)
        self.authorization = AuthorizationManager(bank.database)
        self.authorizations = authorizations or {}
        self.community = CommunityApplications(bank.database)
        self.server_registry = ServerRegistry(bank.database)
        self.farm_lifecycle = FarmLifecycle(bank.database, self.authorization)
        self.activity_publisher = ActivityPublisher(self, bank.database)
        self.sin_member_role_id = int(sin_member_role_id) if sin_member_role_id else None
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

        @self.tree.command(name="apply", description="Apply for SiN community membership")
        @app_commands.check(channel_check)
        async def apply(interaction, nickname: str, farm_name: str):
            record = await asyncio.to_thread(self.community.apply, str(interaction.user.id), nickname, farm_name)
            await interaction.response.send_message(f"Community application pending. Requested SiN display name: **{record['server_nickname']}**", ephemeral=True)

        @self.tree.command(name="application_pending", description="Staff: list pending community applications")
        @app_commands.check(channel_check)
        async def application_pending(interaction):
            staff_check(interaction); await interaction.response.defer(ephemeral=True)
            rows = await asyncio.to_thread(self.community.pending)
            await interaction.followup.send("\n".join(f"<@{r['discord_id']}> → **{r['server_nickname']}**" for r in rows) or "No pending applications.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

        @self.tree.command(name="application_approve", description="Staff: approve community membership")
        @app_commands.check(channel_check)
        async def application_approve(interaction, member: discord.Member):
            staff_check(interaction)
            record = await asyncio.to_thread(self.community.pending_for, str(member.id))
            # Persist the authoritative SiN decision before attempting any
            # Discord presentation work. Discord hierarchy is not an auth
            # transaction and must never roll back membership approval.
            await asyncio.to_thread(self.community.approve, str(member.id), str(interaction.user.id))
            warnings = []
            nickname_synced = False
            role_synced = False
            try:
                await member.edit(nick=record['server_nickname'], reason="SiN community approval")
                nickname_synced = True
            except (discord.Forbidden, discord.HTTPException):
                warnings.append("JiN could not update the Discord nickname due to Discord hierarchy or permissions.")
            role = interaction.guild.get_role(self.sin_member_role_id) if self.sin_member_role_id else None
            if role is None:
                warnings.append("JiN could not assign the SiN Member role; manual Discord role correction is required.")
            else:
                try:
                    await member.add_roles(role, reason="SiN community approval")
                    role_synced = True
                except (discord.Forbidden, discord.HTTPException):
                    warnings.append("JiN could not assign the SiN Member role; manual Discord role correction is required.")
            message = f"Application approved for **{record['server_nickname']}**."
            if not warnings:
                message += " Discord nickname and SiN Member role synchronized."
            else:
                message += " Warning: " + " ".join(warnings)
            await interaction.response.send_message(message, ephemeral=True)

        @self.tree.command(name="application_deny", description="Staff: deny community membership")
        @app_commands.check(channel_check)
        async def application_deny(interaction, member: discord.Member, reason: str):
            staff_check(interaction)
            await asyncio.to_thread(self.community.deny, str(member.id), str(interaction.user.id), reason)
            await interaction.response.send_message("Community application denied.", ephemeral=True)

        @self.tree.command(name="register", description="Link your observed FS25 identity to SiN")
        @app_commands.check(channel_check)
        async def register(interaction, code: str):
            if interaction.guild_id != self.guild.id:
                raise ValueError("Use this command in the configured Discord server")
            identity = await asyncio.to_thread(self.authorization.register_identity, str(interaction.user.id), code)
            await interaction.response.send_message(
                "Registration complete. Your Discord account is now linked to your Farming Simulator identity.",
                ephemeral=True)

        @self.tree.command(name="server_register", description="Staff: register a SiN FS25 server")
        @app_commands.check(channel_check)
        async def server_register(interaction, server_key: str, name: str, activity_channel: discord.TextChannel):
            staff_check(interaction)
            if activity_channel.guild.id != self.guild.id:
                raise ValueError("Activity channel must belong to the configured Discord server")
            record, pairing = await asyncio.to_thread(self.server_registry.register, server_key, name,
                self.guild.id, activity_channel.id)
            message = f"Server `{record['server_key']}` is configured for {activity_channel.mention}."
            if pairing:
                message += f" Pairing code (one-time, expires in 30 minutes): `{pairing}`"
            else:
                message += " Existing pairing was preserved."
            await interaction.response.send_message(message, ephemeral=True)

        @self.tree.command(name="server_info", description="Staff: view SiN server configuration")
        @app_commands.check(channel_check)
        async def server_info(interaction, server_key: str):
            staff_check(interaction)
            record = await asyncio.to_thread(self.server_registry.info, server_key)
            if not record: raise ValueError("Unknown SiN server")
            channel = self.guild.get_channel(int(record["discord_activity_channel_id"]))
            await interaction.response.send_message(
                f"Server: {record['display_name']}\nKey: {record['server_key']}\n"
                f"Activity Channel: {channel.mention if channel else record['discord_activity_channel_id']}\n"
                f"Enabled: {'Yes' if record.get('enabled') else 'No'}\nPaired: {'Yes' if record.get('credential_hash') else 'No'}",
                ephemeral=True)

        async def observed_players(interaction: discord.Interaction, current: str):
            server = getattr(interaction.namespace, "server", None)
            if not server or server not in self.servers:
                return []
            try:
                snapshot = await asyncio.to_thread(ServerScraper(self.servers).snapshot, server)
            except ValueError:
                return []
            member = getattr(interaction.namespace, "member", None)
            if member:
                try:
                    await asyncio.to_thread(auth_for(server).pending_request_for_user, member, server, self.servers[server]["save_id"])
                except ValueError:
                    return []
            assigned = set()
            if hasattr(auth_for(server).db.game_identities, "find"):
                assigned = {r.get("game_player_id") for r in auth_for(server).db.game_identities.find({"server_id": server, "save_id": self.servers[server]["save_id"]})}
            choices = []
            for player_id, name in snapshot["players"].items():
                if player_id in assigned:
                    continue
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
                records = await asyncio.to_thread(self.farm_lifecycle.requests, server, self.servers[server]["save_id"])
            except ValueError:
                return []
            if not isinstance(records, list) or not records:
                # Keep autocomplete compatible with legacy test/local records
                # while new requests use the canonical server_key/save_key.
                legacy_records = await asyncio.to_thread(auth_for(server).requests, server, self.servers[server]["save_id"])
                if isinstance(legacy_records, list) and legacy_records:
                    records = legacy_records
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

        async def server_choices(interaction: discord.Interaction, current: str):
            if interaction.guild_id != self.guild.id:
                return []
            choices = []
            for server_key, config in self.servers.items():
                label = config.get("display_name") or server_key
                if current.lower() in label.lower() or current.lower() in server_key.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=server_key))
            return choices[:25]

        @self.tree.command(name="farm_request", description="Request a farm and starting field for staff review")
        @app_commands.check(channel_check)
        async def farm_request(interaction: discord.Interaction, server: str, starting_field: str):
            config = server_config(interaction, server)
            await interaction.response.defer(ephemeral=True)
            record = await asyncio.to_thread(self.farm_lifecycle.request_farm, str(interaction.user.id), server,
                                            config["save_id"], starting_field)
            await interaction.followup.send(
                f"Your request for **{record['farm_name']}** at **{record['starting_field']}** is pending staff review. "
                "Staff will create the farm and confirm your in-game identity. Repeated submissions keep this request.",
                ephemeral=True)

        @farm_request.autocomplete("server")
        async def farm_request_server_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current)

        @self.tree.command(name="farm_requests", description="Staff: list pending farm requests")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def farm_requests(interaction: discord.Interaction, server: str):
            staff_check(interaction)
            config = server_config(interaction, server)
            await interaction.response.defer(ephemeral=True)
            records = await asyncio.to_thread(self.farm_lifecycle.requests, server, config["save_id"])
            rows = [f"<@{r['discord_id']}> requested **{r['farm_name']}**; starting field: **{r['starting_field']}**. "
                    "Use `/farm_approve` and select this member." for r in records]
            for row in rows or ["No pending requests."]:
                await interaction.followup.send(row, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

        @self.tree.command(name="farm_roster", description="Staff: inspect players and farms reported by the local mod")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def farm_roster(interaction: discord.Interaction, server: str):
            staff_check(interaction)
            config = server_config(interaction, server)
            await interaction.response.defer(ephemeral=True)
            snapshot = await asyncio.to_thread(ServerScraper(self.servers).snapshot, server)
            await asyncio.to_thread(auth_for(server).observe_players, server, config["save_id"], snapshot)
            rows = [f"Farm {farm_id}: {name or '(unnamed; cannot approve)'}" for farm_id, name in snapshot["farms"].items()]
            rows += [f"Player ID: {player_id} | name: {name}" for player_id, name in snapshot["players"].items()]
            for row in rows or ["No farms or players reported."]:
                await interaction.followup.send(row[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

        @self.tree.command(name="farm_approve", description="Staff: approve a request and queue automatic farm provisioning")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def farm_approve(interaction: discord.Interaction, server: str, member: str):
            staff_check(interaction)
            config = server_config(interaction, server)
            await interaction.response.defer(ephemeral=True)
            request = await asyncio.to_thread(self.farm_lifecycle.request_status, member)
            if not request or request.get("server_key") != server or request.get("save_key") != config["save_id"]:
                raise ValueError("That member has no pending farm request for this server")
            operation = await asyncio.to_thread(self.farm_lifecycle.approve_request, request["_id"], server,
                                                config["save_id"], str(interaction.user.id))
            await interaction.followup.send(
                f"Approved requester `{member}` for **{request['farm_name']}**. "
                f"Farm provisioning is pending (operation `{operation}`). Manager authority is withheld until the game confirms the exact farm and field.",
                ephemeral=True)

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
            request = await asyncio.to_thread(self.farm_lifecycle.request_status, member)
            if not request or request.get("server_key") != server or request.get("save_key") != config["save_id"]:
                raise ValueError("That member has no pending farm request for this server")
            await asyncio.to_thread(self.farm_lifecycle.reject_request, request["_id"], server, config["save_id"], str(interaction.user.id), reason)
            await interaction.followup.send(f"Farm request for requester `{member}` rejected.", ephemeral=True)

        @farm_reject.autocomplete("member")
        async def farm_reject_member_autocomplete(interaction: discord.Interaction, current: str):
            return await pending_requesters(interaction, current)

        @self.tree.command(name="farm_status", description="View your farm assignment and permission sync state")
        @app_commands.check(channel_check)
        async def farm_status(interaction: discord.Interaction):
            if interaction.guild_id != self.guild.id:
                raise ValueError("Use this command in the configured Discord server")
            await interaction.response.defer(ephemeral=True)
            request = await asyncio.to_thread(self.farm_lifecycle.request_status, str(interaction.user.id))
            message = "You have not requested a farm. Use /farm_request."
            if request:
                server_name = self.servers.get(request.get("server_key"), {}).get("display_name", request.get("server_key"))
                message = (f"Farm: {request.get('farm_name')}\nServer: {server_name}\n"
                           f"Starting field: {request.get('starting_field')}\nStatus: {request.get('state')}")
                if request.get("farm_id"):
                    message += f"\nFS25 Farm ID: {request['farm_id']}"
            await interaction.followup.send(message, ephemeral=True)

        @self.tree.command(name="server_reconcile", description="Staff: reconcile required SiN server resources")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def server_reconcile(interaction: discord.Interaction, server: str):
            staff_check(interaction)
            server_config(interaction, server)
            await interaction.response.defer(ephemeral=True)
            results = await asyncio.to_thread(self.farm_lifecycle.ensure_for_server, server)
            statuses = {item.get("status") for item in results}
            if "reconciliation_required" in statuses:
                status = "reconciliation required; no farm was guessed or recreated"
            elif results and statuses == {"active"}:
                status = "active"
            else:
                status = "pending; the Agent must submit the current FS25 snapshot"
            await interaction.followup.send(
                f"Server reconciliation {status}. Required system farm: **{SYSTEM_FARM_NAME}**.",
                ephemeral=True)

        @server_reconcile.autocomplete("server")
        async def server_reconcile_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current)

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
        self.activity_publisher.start()

    async def close(self):
        await self.activity_publisher.stop()
        await super().close()


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
    try:
        sin_member_role_id = int(discord_config.get("roles", {}).get("sin_member", ""))
        for name in ("sin_apply", "sin_applications"):
            int(channels[name])
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit("Configure numeric channels.sin_apply, channels.sin_applications, and roles.sin_member IDs in discord.json") from error
    with open(os.environ.get("FS25_SERVERS_FILE", PROJECT_ROOT / "servers.json"), encoding="utf-8") as stream:
        servers = json.load(stream)
    # Central services share the configured environment database. Per-server
    # config must not redirect JiN into a legacy local-test database.
    database = Database()
    database.initialize()
    logging.info("[SiN DB] environment=bot database=%s", database.name)
    registry = ServerRegistry(database)
    for server_key, config in servers.items():
        if config.get("fs25_save_id") is not None and config.get("save_id"):
            registry.configure_save(server_key, config["save_id"], config["fs25_save_id"])
        record = registry.info(server_key)
        if record and record.get("display_name"):
            config.setdefault("display_name", record["display_name"])
    authorizations = {}
    for server_id, config in servers.items():
        if config.get("development"):
            if config.get("withdrawals_enabled"):
                raise SystemExit("Withdrawals must remain disabled for development servers")
            authorizations[server_id] = AuthorizationManager(database)
            logging.info("[SiN DB] environment=%s community=%s authorization=%s banking=%s",
                         server_id, database.name, database.name, database.name)
    try:
        NetworkBot(BankingEngine(database), servers, int(guild_id), operator_role_ids, channels, authorizations, sin_member_role_id).run(token, log_handler=None)
    except DiscordSetupError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
