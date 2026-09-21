"""Run with python -m fs25_network_core.bot_frontend."""
import asyncio
import io
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
from .activity_telemetry import ActivityTelemetryProcessor
from .business_workflows import (ChatService, ContractService, InvoiceService,
                                  CommunityEventService, TransferService)
from .farm_lifecycle import FarmLifecycle, SYSTEM_FARM_NAME
from .map_service import MapService, MapStore, MapValidationError


class DiscordSetupError(RuntimeError):
    """An actionable installation error safe to display without credentials."""


def player_choice_label(player_id, nickname):
    return f"Nickname: {nickname or 'Unnamed player'} | FS25 player ID: {player_id}"


def approved_nickname(player_name, farm_name):
    return f"{player_name or 'Player'} | {farm_name}"[:32]


def discord_timestamp(value):
    """Render an aware event time in Discord's per-user timezone format."""
    if hasattr(value, "timestamp"):
        stamp = int(value.timestamp())
        return f"<t:{stamp}:F> (<t:{stamp}:R>)"
    return str(value or "time unavailable")


class ContractView(discord.ui.View):
    """Persistent marketplace action backed by the durable contract state."""

    def __init__(self, bot, contract_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.contract_id = str(contract_id)
        # Persistent views need a distinct routing key per contract card.
        self.children[0].custom_id = f"sin:contract:accept:{self.contract_id}"

    @discord.ui.button(label="Accept contract", style=discord.ButtonStyle.success,
                       custom_id="sin:contract:accept")
    async def accept_contract(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            record = await asyncio.to_thread(self.bot.contracts.accept, self.contract_id,
                                             str(interaction.user.id))
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        button.disabled = True
        button.label = "Accepted"
        await interaction.response.edit_message(content=self.bot.contract_card_text(record), view=self)


class CommunityEventView(discord.ui.View):
    """Persistent RSVP controls for a scheduled community event."""

    def __init__(self, bot, event_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.event_id = str(event_id)
        self.children[0].custom_id = f"sin:event:join:{self.event_id}"
        self.children[1].custom_id = f"sin:event:leave:{self.event_id}"

    @discord.ui.button(label="Join event", style=discord.ButtonStyle.success, custom_id="sin:event:join")
    async def join_event(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            record = await asyncio.to_thread(self.bot.community_events.join, self.event_id,
                                             str(interaction.user.id))
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.edit_message(content=self.bot.event_card_text(record), view=self)

    @discord.ui.button(label="Leave event", style=discord.ButtonStyle.secondary, custom_id="sin:event:leave")
    async def leave_event(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            record = await asyncio.to_thread(self.bot.community_events.leave, self.event_id,
                                             str(interaction.user.id))
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.edit_message(content=self.bot.event_card_text(record), view=self)


class NetworkBot(discord.Client):
    def __init__(self, bank, servers, guild_id, operator_role_ids=(), channels=None, authorizations=None,
                 sin_member_role_id=None, community_timezone="UTC", map_service=None):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents)
        self.bank, self.servers = bank, servers
        self.guild = discord.Object(id=guild_id)
        self.channels = {name: int(value) for name, value in (channels or {}).items()}
        self.community_timezone = str(community_timezone or "UTC")
        self.tree = app_commands.CommandTree(self)
        self._command_sync_lock = asyncio.Lock()
        self.authorization = AuthorizationManager(bank.database)
        self.authorizations = authorizations or {}
        self.community = CommunityApplications(bank.database)
        self.server_registry = ServerRegistry(bank.database)
        self.farm_lifecycle = FarmLifecycle(bank.database, self.authorization)
        self.activity_publisher = ActivityPublisher(self, bank.database)
        self.telemetry = ActivityTelemetryProcessor(bank.database)
        self.chat = ChatService(bank.database)
        self.contracts = ContractService(bank.database)
        self.invoices = InvoiceService(bank.database, bank)
        self.community_events = CommunityEventService(bank.database)
        self.transfers = TransferService(bank.database, self.authorization)
        # Map rendering is presentation-only.  An empty service is safe until
        # a validated map exporter registers geometry and an overview image.
        self.map_service = map_service or MapService()
        self._contract_views_restored = False
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

        def server_config(interaction, server, purpose="reconcile"):
            if interaction.guild_id != self.guild.id:
                raise ValueError("Use this command in the configured Discord server")
            try:
                return self.server_registry.eligible_server(server, purpose)
            except ValueError:
                # Explicitly supplied local/test rosters remain usable for
                # local development.  Production startup no longer loads
                # servers.json by default, so this cannot leak local-dev into
                # production choices or bypass the central registry there.
                legacy = self.servers.get(server)
                if not legacy or not legacy.get("development"):
                    raise
                save_key = legacy.get("save_id")
                if not save_key:
                    raise ValueError("Unknown or ineligible game server")
                return {
                    "server_key": server,
                    "display_name": legacy.get("display_name") or server,
                    "enabled": True,
                    "paired": True,
                    "saves": [{"save_key": save_key, "fs25_save_id": legacy.get("fs25_save_id")}],
                    **legacy,
                }

        def selected_save(config):
            saves = config.get("saves") or []
            if len(saves) != 1:
                raise ValueError("The selected server must have exactly one configured save")
            return saves[0]["save_key"]

        def auth_for(server):
            return self.authorizations.get(server, self.authorization)

        def staff_check(interaction):
            require_operator(interaction.guild_id, self.guild.id,
                             [item.id for item in getattr(interaction.user, "roles", [])], self.operator_role_ids)

        async def server_choices(interaction: discord.Interaction, current: str, purpose="reconcile"):
            if interaction.guild_id != self.guild.id:
                return []
            try:
                records = await asyncio.to_thread(self.server_registry.eligible_servers, purpose)
            except (ValueError, OSError, asyncio.TimeoutError) as error:
                logging.warning("Dynamic server autocomplete unavailable purpose=%s error=%s", purpose, error)
                return []
            except Exception:
                logging.exception("Dynamic server autocomplete failed purpose=%s", purpose)
                return []
            query = (current or "").lower()
            choices = []
            for record in records:
                server_key = record["server_key"]
                label = record.get("display_name") or server_key
                if query in label.lower() or query in server_key.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=server_key))
            return choices[:25]

        async def contract_choices(interaction: discord.Interaction, current: str):
            records = await asyncio.to_thread(self.contracts.open)
            query = (current or "").lower()
            choices = []
            for record in records:
                label = f"{record.get('work_type', 'general').title()} — Fields {record.get('fields') or 'unspecified'} — {record.get('title', '')}"
                if query in label.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=record["contract_id"]))
            return choices[:25]

        async def invoice_choices(interaction: discord.Interaction, current: str):
            records = await asyncio.to_thread(self.invoices.list_for, str(interaction.user.id))
            query = (current or "").lower()
            choices = []
            for record in records:
                label = f"{record.get('amount', 0):,} — {record.get('description', '')} — {record.get('status', '')}"
                if query in label.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=record["invoice_id"]))
            return choices[:25]

        async def event_choices(interaction: discord.Interaction, current: str):
            records = await asyncio.to_thread(self.community_events.list)
            query = (current or "").lower()
            choices = []
            for record in records:
                label = f"{record.get('name', '')} — {record.get('status', '')}"
                if query in label.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=record["event_id"]))
            return choices[:25]

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

        @server_info.autocomplete("server_key")
        async def server_info_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current, "info")

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
            if not server:
                return []
            try:
                config = await asyncio.to_thread(server_config, interaction, server)
                save_key = selected_save(config)
            except ValueError:
                return []
            try:
                records = await asyncio.to_thread(self.farm_lifecycle.requests, server, save_key)
            except ValueError:
                return []
            if not isinstance(records, list) or not records:
                # Keep autocomplete compatible with legacy test/local records
                # while new requests use the canonical server_key/save_key.
                legacy_records = await asyncio.to_thread(auth_for(server).requests, server, save_key)
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

        async def land_pending_requesters(interaction: discord.Interaction, current: str):
            server = getattr(interaction.namespace, "server", None)
            if not server:
                return []
            try:
                config = await asyncio.to_thread(server_config, interaction, server)
                records = await asyncio.to_thread(
                    self.farm_lifecycle.land_pending_requests, server, selected_save(config))
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
                label = f"Requester: {display_name} | Farm: {record['farm_name']} | FS25 farm {record.get('farm_id')}"
                if current.lower() in label.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=record["discord_id"]))
            return choices[:25]

        @self.tree.command(name="farm_request", description="Request a farm and starting field for staff review")
        @app_commands.check(channel_check)
        async def farm_request(interaction: discord.Interaction, server: str, starting_field: str):
            config = server_config(interaction, server, "farm_request")
            await interaction.response.defer(ephemeral=True)
            record = await asyncio.to_thread(self.farm_lifecycle.request_farm, str(interaction.user.id), server,
                                            selected_save(config), starting_field)
            await interaction.followup.send(
                f"Your request for **{record['farm_name']}** at **{record['starting_field']}** is pending staff review. "
                "Staff will create the farm and confirm your in-game identity. Repeated submissions keep this request.",
                ephemeral=True)

        @farm_request.autocomplete("server")
        async def farm_request_server_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current, "farm_request")

        @farm_request.autocomplete("starting_field")
        async def farm_request_field_autocomplete(interaction: discord.Interaction, current: str):
            server = getattr(interaction.namespace, "server", None)
            if not server:
                return []
            try:
                config = await asyncio.to_thread(server_config, interaction, server, "farm_request")
                save = (config.get("saves") or [])[0]
                fields = save.get("available_fields") or []
            except (ValueError, IndexError, TypeError):
                return []
            query = (current or "").strip()
            return [app_commands.Choice(name=str(field), value=str(field))
                    for field in fields if not query or query in str(field)][:25]

        @self.tree.command(name="farm_requests", description="Staff: list pending farm requests")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def farm_requests(interaction: discord.Interaction, server: str):
            staff_check(interaction)
            config = server_config(interaction, server)
            await interaction.response.defer(ephemeral=True)
            records = await asyncio.to_thread(self.farm_lifecycle.requests, server, selected_save(config))
            rows = [f"<@{r['discord_id']}> requested **{r['farm_name']}**; starting field: **{r['starting_field']}**. "
                    "Use `/farm_approve` and select this member." for r in records]
            for row in rows or ["No pending requests."]:
                await interaction.followup.send(row, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

        @farm_requests.autocomplete("server")
        async def farm_requests_server_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current, "reconcile")

        @self.tree.command(name="farm_roster", description="Staff: inspect players and farms reported by the local mod")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def farm_roster(interaction: discord.Interaction, server: str):
            staff_check(interaction)
            config = server_config(interaction, server)
            await interaction.response.defer(ephemeral=True)
            save_key = selected_save(config)
            snapshot = await asyncio.to_thread(self.farm_lifecycle.latest_snapshot, server, save_key)
            if not snapshot and server in self.servers:
                snapshot = await asyncio.to_thread(ServerScraper(self.servers).snapshot, server)
            if not snapshot:
                raise ValueError("No current game snapshot is available")
            await asyncio.to_thread(auth_for(server).observe_players, server, save_key, snapshot)
            rows = [f"Farm {farm_id}: {name or '(unnamed; cannot approve)'}" for farm_id, name in snapshot["farms"].items()]
            rows += [f"Player ID: {player_id} | name: {name}" for player_id, name in snapshot["players"].items()]
            for row in rows or ["No farms or players reported."]:
                await interaction.followup.send(row[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

        @farm_roster.autocomplete("server")
        async def farm_roster_server_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current, "reconcile")

        @self.tree.command(name="farm_approve", description="Staff: approve a request and queue farm provisioning")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def farm_approve(interaction: discord.Interaction, server: str, member: str):
            staff_check(interaction)
            config = server_config(interaction, server)
            save_key = selected_save(config)
            await interaction.response.defer(ephemeral=True)
            request = await asyncio.to_thread(self.farm_lifecycle.request_status, member)
            if not request or request.get("server_key") != server or request.get("save_key") != save_key:
                raise ValueError("That member has no pending farm request for this server")
            operation = await asyncio.to_thread(self.farm_lifecycle.approve_request, request["_id"], server,
                                                save_key, str(interaction.user.id))
            await interaction.followup.send(
                f"Approved requester `{member}` for **{request['farm_name']}**. "
                f"Farm provisioning is pending (operation `{operation}`). After FS25 confirms the farm, use `/farmland_assign` to make one explicit land decision; manager authority remains withheld until its owner read-back succeeds.",
                ephemeral=True)

        @farm_approve.autocomplete("member")
        async def farm_approve_member_autocomplete(interaction: discord.Interaction, current: str):
            return await pending_requesters(interaction, current)

        @farm_approve.autocomplete("server")
        async def farm_approve_server_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current, "reconcile")

        @self.tree.command(name="farmland_assign", description="Staff: assign an unowned farmland to a land-pending farm")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def farmland_assign(interaction: discord.Interaction, server: str, member: str,
                                  farmland_id: app_commands.Range[int, 1]):
            staff_check(interaction)
            config = server_config(interaction, server)
            save_key = selected_save(config)
            await interaction.response.defer(ephemeral=True)
            request = await asyncio.to_thread(self.farm_lifecycle.request_status, member)
            if not request or request.get("server_key") != server or request.get("save_key") != save_key:
                raise ValueError("That member has no land-pending farm request for this server")
            operation = await asyncio.to_thread(
                self.farm_lifecycle.assign_farmland, request["_id"], server, save_key,
                int(farmland_id), str(interaction.user.id))
            await interaction.followup.send(
                f"Queued farmland **{farmland_id}** → FS25 farm **{request.get('farm_id')}** "
                f"for **{request['farm_name']}** (operation `{operation}`). FS25 must report that owner back before SiN grants manager authority.",
                ephemeral=True)

        @farmland_assign.autocomplete("member")
        async def farmland_assign_member_autocomplete(interaction: discord.Interaction, current: str):
            return await land_pending_requesters(interaction, current)

        @farmland_assign.autocomplete("server")
        async def farmland_assign_server_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current, "reconcile")

        @self.tree.command(name="farmland_status", description="Staff: inspect current authoritative farmland ownership")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def farmland_status(interaction: discord.Interaction, server: str,
                                  farmland_id: app_commands.Range[int, 1]):
            staff_check(interaction)
            config = server_config(interaction, server)
            save_key = selected_save(config)
            await interaction.response.defer(ephemeral=True)
            fields = await asyncio.to_thread(self.farm_lifecycle.available_fields, server, save_key)
            if int(farmland_id) not in fields:
                raise ValueError("Farmland ID is not present in the current authoritative FS25 snapshot")
            snapshot = await asyncio.to_thread(self.farm_lifecycle.latest_snapshot, server, save_key)
            owner = fields[int(farmland_id)]
            received_at = (snapshot or {}).get("received_at")
            await interaction.followup.send(
                f"Farmland **{farmland_id}** on `{server}` / `{save_key}`: authoritative snapshot owner "
                f"**FS25 farm {owner}** (snapshot `{received_at}`). Assignment receipts additionally record pre/post owner read-back.",
                ephemeral=True)

        @farmland_status.autocomplete("server")
        async def farmland_status_server_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current, "reconcile")

        @self.tree.command(name="farm_reject", description="Staff: reject a pending farm request with a reason")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def farm_reject(interaction: discord.Interaction, server: str, member: str, reason: str):
            staff_check(interaction)
            config = server_config(interaction, server)
            save_key = selected_save(config)
            await interaction.response.defer(ephemeral=True)
            request = await asyncio.to_thread(self.farm_lifecycle.request_status, member)
            if not request or request.get("server_key") != server or request.get("save_key") != save_key:
                raise ValueError("That member has no pending farm request for this server")
            await asyncio.to_thread(self.farm_lifecycle.reject_request, request["_id"], server, save_key, str(interaction.user.id), reason)
            await interaction.followup.send(f"Farm request for requester `{member}` rejected.", ephemeral=True)

        @farm_reject.autocomplete("member")
        async def farm_reject_member_autocomplete(interaction: discord.Interaction, current: str):
            return await pending_requesters(interaction, current)

        @farm_reject.autocomplete("server")
        async def farm_reject_server_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current, "reconcile")

        @self.tree.command(name="farm_status", description="View your farm assignment and permission sync state")
        @app_commands.check(channel_check)
        async def farm_status(interaction: discord.Interaction):
            if interaction.guild_id != self.guild.id:
                raise ValueError("Use this command in the configured Discord server")
            await interaction.response.defer(ephemeral=True)
            request = await asyncio.to_thread(self.farm_lifecycle.request_status, str(interaction.user.id))
            message = "You have not requested a farm. Use /farm_request."
            if request:
                server_record = await asyncio.to_thread(self.server_registry.info, request.get("server_key"))
                server_name = (server_record or {}).get("display_name", request.get("server_key"))
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
            return await server_choices(interaction, current, "reconcile")

        @self.tree.command(name="farm_assign", description="Staff: assign or revoke a verified player's farm role")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        @app_commands.choices(role=[app_commands.Choice(name=value, value=value) for value in sorted(ROLES)])
        async def farm_assign(interaction: discord.Interaction, member: discord.Member, server: str,
                              farm_id: app_commands.Range[int, 1], role: app_commands.Choice[str]):
            config = server_config(interaction, server)
            save_key = selected_save(config)
            require_operator(interaction.guild_id, self.guild.id,
                             [item.id for item in getattr(interaction.user, "roles", [])], self.operator_role_ids)
            await interaction.response.defer(ephemeral=True)
            snapshot = await asyncio.to_thread(self.farm_lifecycle.latest_snapshot, server, save_key)
            if not snapshot and server in self.servers:
                farms = await asyncio.to_thread(ServerScraper(self.servers).farms, server)
            else:
                farms = (snapshot or {}).get("farms", {})
            operation = await asyncio.to_thread(auth_for(server).assign, str(member.id), server,
                save_key, farm_id, role.value, farms, str(interaction.user.id))
            await interaction.followup.send(f"Permission operation `{operation}` queued. It becomes active after the server mod confirms it.", ephemeral=True)

        @farm_assign.autocomplete("server")
        async def farm_assign_server_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current, "reconcile")

        @self.tree.command(name="balance", description="View your central available balance")
        @app_commands.check(channel_check)
        async def balance(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            summary = await asyncio.to_thread(self.bank.account_summary, str(interaction.user.id))
            game_balance = ("unavailable (the current FS25 snapshot does not expose authoritative farm money)"
                            if summary["game_balance"] is None else f"${summary['game_balance']:,}")
            await interaction.followup.send(
                f"Game balance: {game_balance}\n"
                f"SiN bank balance: ${summary['available_balance']:,}\n"
                f"Pending deposits: ${summary['pending_deposits']:,}\n"
                f"Pending withdrawals: ${summary['pending_withdrawals']:,}\n"
                f"Available balance: ${summary['available_balance']:,}", ephemeral=True)

        @self.tree.command(name="deposit", description="Queue a game-to-SiN bank deposit")
        @app_commands.check(channel_check)
        async def deposit(interaction: discord.Interaction,
                          amount: app_commands.Range[int, 1, 1_000_000_000]):
            context = await asyncio.to_thread(self.resolve_identity_context, str(interaction.user.id), None, "reconcile")
            server_key, save_key = context["server_key"], context["save_key"]
            state = await asyncio.to_thread(self.bank.request_deposit, str(interaction.id),
                                            str(interaction.user.id), server_key, save_key, amount)
            await interaction.response.send_message(
                f"Deposit is {state} for the selected game context. The game-side debit must be confirmed before your balance changes.",
                ephemeral=True)

        @self.tree.command(name="withdraw", description="Reserve funds for delivery to your approved farm")
        @app_commands.check(channel_check)
        async def withdraw(interaction: discord.Interaction,
                           amount: app_commands.Range[int, 1, 1_000_000_000]):
            context = await asyncio.to_thread(self.resolve_identity_context, str(interaction.user.id), None, "reconcile")
            server_key, save_key = context["server_key"], context["save_key"]
            config = None
            if not self.withdrawals_enabled(server_key, config):
                await interaction.response.send_message(
                    "Withdrawals are disabled because no verified FS25 money-delivery adapter is enabled for this server.",
                    ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            state = await asyncio.to_thread(self.bank.request_withdrawal, str(interaction.id),
                                           str(interaction.user.id), server_key, save_key, amount)
            await interaction.followup.send(f"Withdrawal is {state}. Pending funds are reserved until delivery is confirmed.", ephemeral=True)

        @self.tree.command(name="chat_send", description="Staff: send a message to an FS25 server chat")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def chat_send(interaction: discord.Interaction, server: str, message: str):
            staff_check(interaction)
            if not self.chat.fs25_injection_supported():
                await interaction.response.send_message(
                    "Discord-to-FS25 chat injection is currently unavailable: no verified GIANTS runtime adapter is enabled. No message was sent.",
                    ephemeral=True)
                return
            save_key = selected_save(server_config(interaction, server, "reconcile"))
            operation_id = await asyncio.to_thread(self.chat.queue_to_fs25, server, save_key,
                                                   str(interaction.user.id), message)
            await interaction.response.send_message(
                f"Game chat operation `{operation_id}` queued for the selected server.", ephemeral=True)

        @chat_send.autocomplete("server")
        async def chat_send_server_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current, "reconcile")

        @self.tree.command(name="contract_create", description="Create a structured farm-work contract")
        @app_commands.check(channel_check)
        @app_commands.choices(
            work_type=[app_commands.Choice(name=value.title(), value=value) for value in sorted(ContractService.WORK_TYPES)],
            compensation=[app_commands.Choice(name="Fixed price", value="fixed"),
                          app_commands.Choice(name="Hourly", value="hourly")],
        )
        async def contract_create(interaction: discord.Interaction, work_type: app_commands.Choice[str], fields: str,
                                  compensation: app_commands.Choice[str], rate: app_commands.Range[int, 1, 1_000_000_000],
                                  description: str = "", server: str = ""):
            try:
                # Explicit selection must still be resolved through the
                # caller's durable identity contexts. Registry eligibility
                # alone is not authorization to create work for an unrelated
                # server/save. Missing and ambiguous contexts fail closed.
                context = await asyncio.to_thread(
                    self.resolve_identity_context, str(interaction.user.id), server or None, "reconcile")
            except ValueError as error:
                if "multiple game contexts" in str(error).lower():
                    raise ValueError("Select an eligible server with the server option; no server was chosen") from None
                raise
            record = await asyncio.to_thread(self.contracts.create, str(interaction.user.id), "", description,
                                             work_type=work_type.value, fields=fields,
                                             compensation_type=compensation.value, rate=rate,
                                             server_key=context.get("server_key"), save_key=context.get("save_key"),
                                             server_name=context.get("server_name"))
            published = await self.publish_contract_card(record)
            await interaction.response.send_message(
                f"**{record['title']}** is open. {compensation.name}: {rate:,}."
                + (" Posted in the jobs channel." if published else
                   " The jobs channel is not configured, so use `/contract_list` to find it."), ephemeral=True)

        @contract_create.autocomplete("server")
        async def contract_create_server_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current, "reconcile")

        @self.tree.command(name="contract_list", description="List open SiN contracts")
        @app_commands.check(channel_check)
        async def contract_list(interaction: discord.Interaction):
            records = await asyncio.to_thread(self.contracts.open)
            text = "\n".join(f"**{r['title']}** — {r.get('compensation_type', 'fixed').title()}: {r.get('rate', r.get('value', 0)):,}" for r in records)
            await interaction.response.send_message(text or "No open contracts.", ephemeral=True)

        @self.tree.command(name="contract_view", description="View a SiN contract")
        @app_commands.check(channel_check)
        async def contract_view(interaction: discord.Interaction, contract_id: str):
            record = await asyncio.to_thread(self.contracts.get, contract_id)
            if not record:
                raise ValueError("Unknown contract")
            await interaction.response.send_message(
                f"**{record['title']}**\n{record['description']}\nStatus: {record['status']}\n"
                f"Work: {record.get('work_type', 'general').title()}\nFields: {record.get('fields') or 'unspecified'}\n"
                f"Compensation: {record.get('compensation_type', 'fixed').title()} {record.get('rate', record['value']):,}", ephemeral=True)

        @contract_view.autocomplete("contract_id")
        async def contract_view_autocomplete(interaction: discord.Interaction, current: str):
            return await contract_choices(interaction, current)

        @self.tree.command(name="contract_accept", description="Accept an open SiN contract")
        @app_commands.check(channel_check)
        async def contract_accept(interaction: discord.Interaction, contract_id: str):
            record = await asyncio.to_thread(self.contracts.accept, contract_id, str(interaction.user.id))
            await interaction.response.send_message(f"Contract `{record['contract_id']}` accepted.", ephemeral=True)

        @contract_accept.autocomplete("contract_id")
        async def contract_accept_autocomplete(interaction: discord.Interaction, current: str):
            return await contract_choices(interaction, current)

        @self.tree.command(name="contract_cancel", description="Cancel your SiN contract")
        @app_commands.check(channel_check)
        async def contract_cancel(interaction: discord.Interaction, contract_id: str, reason: str):
            record = await asyncio.to_thread(self.contracts.cancel, contract_id, str(interaction.user.id), reason)
            await interaction.response.send_message(f"Contract `{record['contract_id']}` cancelled.", ephemeral=True)

        @contract_cancel.autocomplete("contract_id")
        async def contract_cancel_autocomplete(interaction: discord.Interaction, current: str):
            return await contract_choices(interaction, current)

        @self.tree.command(name="contract_complete", description="Complete a SiN contract")
        @app_commands.check(channel_check)
        async def contract_complete(interaction: discord.Interaction, contract_id: str, note: str = ""):
            record = await asyncio.to_thread(self.contracts.complete, contract_id, str(interaction.user.id), note)
            await interaction.response.send_message(f"Contract `{record['contract_id']}` completed.", ephemeral=True)

        @contract_complete.autocomplete("contract_id")
        async def contract_complete_autocomplete(interaction: discord.Interaction, current: str):
            return await contract_choices(interaction, current)

        @self.tree.command(name="invoice_create", description="Issue a SiN invoice")
        @app_commands.check(channel_check)
        async def invoice_create(interaction: discord.Interaction, recipient: discord.Member,
                                 amount: app_commands.Range[int, 1, 1_000_000_000], description: str):
            record = await asyncio.to_thread(self.invoices.create, str(interaction.user.id),
                                             str(recipient.id), amount, description)
            await interaction.response.send_message(
                f"Invoice issued to {recipient.mention} for ${record['amount']:,}.", ephemeral=True)

        @self.tree.command(name="invoice_list", description="List your SiN invoices")
        @app_commands.check(channel_check)
        async def invoice_list(interaction: discord.Interaction):
            records = await asyncio.to_thread(self.invoices.list_for, str(interaction.user.id))
            text = "\n".join(f"{r['status'].title()} — ${r['amount']:,} — {r['description']}" for r in records)
            await interaction.response.send_message(text or "No invoices.", ephemeral=True)

        @self.tree.command(name="invoice_view", description="View a SiN invoice")
        @app_commands.check(channel_check)
        async def invoice_view(interaction: discord.Interaction, invoice_id: str):
            record = await asyncio.to_thread(self.invoices.get, invoice_id)
            if not record or str(interaction.user.id) not in {record.get('issuer_discord_id'), record.get('recipient_discord_id')}:
                raise ValueError("Unknown invoice")
            await interaction.response.send_message(
                f"Invoice `{record['invoice_id']}`\nAmount: {record['amount']:,}\n"
                f"Status: {record['status']}\n{record['description']}", ephemeral=True)

        @invoice_view.autocomplete("invoice_id")
        async def invoice_view_autocomplete(interaction: discord.Interaction, current: str):
            return await invoice_choices(interaction, current)

        @self.tree.command(name="invoice_pay", description="Pay a SiN invoice")
        @app_commands.check(channel_check)
        async def invoice_pay(interaction: discord.Interaction, invoice_id: str):
            record = await asyncio.to_thread(self.invoices.pay, invoice_id, str(interaction.user.id))
            await interaction.response.send_message(f"Invoice `{record['invoice_id']}` is {record['status']}.", ephemeral=True)

        @invoice_pay.autocomplete("invoice_id")
        async def invoice_pay_autocomplete(interaction: discord.Interaction, current: str):
            return await invoice_choices(interaction, current)

        @self.tree.command(name="invoice_cancel", description="Cancel an issued SiN invoice")
        @app_commands.check(channel_check)
        async def invoice_cancel(interaction: discord.Interaction, invoice_id: str):
            record = await asyncio.to_thread(self.invoices.cancel, invoice_id, str(interaction.user.id))
            await interaction.response.send_message(f"Invoice `{record['invoice_id']}` cancelled.", ephemeral=True)

        @invoice_cancel.autocomplete("invoice_id")
        async def invoice_cancel_autocomplete(interaction: discord.Interaction, current: str):
            return await invoice_choices(interaction, current)

        @self.tree.command(name="event_create", description="Staff: create a SiN community event")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def event_create(interaction: discord.Interaction, name: str, description: str, scheduled_start: str):
            staff_check(interaction)
            record = await asyncio.to_thread(self.community_events.create, str(interaction.user.id), name,
                                             description, scheduled_start,
                                             default_timezone=self.community_timezone)
            published = await self.publish_event_card(record)
            await interaction.response.send_message(
                f"Community event **{record['name']}** scheduled for {discord_timestamp(record['scheduled_start'])}."
                + (" Posted in the events channel." if published else
                   " The events channel is not configured, so use `/event_list` to find it."),
                ephemeral=True)

        @self.tree.command(name="event_list", description="List SiN community events")
        @app_commands.check(channel_check)
        async def event_list(interaction: discord.Interaction):
            records = await asyncio.to_thread(self.community_events.list)
            text = "\n".join(f"**{r['name']}** — {r['status']} — {discord_timestamp(r['scheduled_start'])}" for r in records)
            await interaction.response.send_message(text or "No community events.", ephemeral=True)

        @self.tree.command(name="event_view", description="View a SiN community event")
        @app_commands.check(channel_check)
        async def event_view(interaction: discord.Interaction, event_id: str):
            record = await asyncio.to_thread(self.community_events.get, event_id)
            if not record:
                raise ValueError("Unknown community event")
            await interaction.response.send_message(
                f"**{record['name']}**\n{record['description']}\nStatus: {record['status']}\n"
                f"When: {discord_timestamp(record['scheduled_start'])}\n"
                f"Participants: {len(record.get('participants', []))}", ephemeral=True)

        @event_view.autocomplete("event_id")
        async def event_view_autocomplete(interaction: discord.Interaction, current: str):
            return await event_choices(interaction, current)

        @self.tree.command(name="event_join", description="Join a SiN community event")
        @app_commands.check(channel_check)
        async def event_join(interaction: discord.Interaction, event_id: str):
            record = await asyncio.to_thread(self.community_events.join, event_id, str(interaction.user.id))
            await interaction.response.send_message(f"Joined **{record['name']}**.", ephemeral=True)

        @event_join.autocomplete("event_id")
        async def event_join_autocomplete(interaction: discord.Interaction, current: str):
            return await event_choices(interaction, current)

        @self.tree.command(name="event_leave", description="Leave a SiN community event")
        @app_commands.check(channel_check)
        async def event_leave(interaction: discord.Interaction, event_id: str):
            record = await asyncio.to_thread(self.community_events.leave, event_id, str(interaction.user.id))
            await interaction.response.send_message(f"Left **{record['name']}**.", ephemeral=True)

        @event_leave.autocomplete("event_id")
        async def event_leave_autocomplete(interaction: discord.Interaction, current: str):
            return await event_choices(interaction, current)

        @self.tree.command(name="event_cancel", description="Staff: cancel a SiN community event")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def event_cancel(interaction: discord.Interaction, event_id: str):
            staff_check(interaction)
            record = await asyncio.to_thread(self.community_events.finish, event_id, str(interaction.user.id), "cancelled", True)
            await interaction.response.send_message(f"Event `{record['event_id']}` cancelled.", ephemeral=True)

        @event_cancel.autocomplete("event_id")
        async def event_cancel_autocomplete(interaction: discord.Interaction, current: str):
            return await event_choices(interaction, current)

        @self.tree.command(name="event_complete", description="Staff: complete a SiN community event")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def event_complete(interaction: discord.Interaction, event_id: str):
            staff_check(interaction)
            record = await asyncio.to_thread(self.community_events.finish, event_id, str(interaction.user.id), "completed", True)
            await interaction.response.send_message(f"Event `{record['event_id']}` completed.", ephemeral=True)

        @event_complete.autocomplete("event_id")
        async def event_complete_autocomplete(interaction: discord.Interaction, current: str):
            return await event_choices(interaction, current)

        @self.tree.command(name="transfer_request", description="Staff: request a durable farm transfer")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        @app_commands.choices(kind=[app_commands.Choice(name="Vehicle", value="vehicle"),
                                    app_commands.Choice(name="Product", value="product")])
        async def transfer_request(interaction: discord.Interaction, kind: app_commands.Choice[str], server: str,
                                   source_farm_id: app_commands.Range[int, 1], destination_farm_id: app_commands.Range[int, 1],
                                   item: str, quantity: float):
            staff_check(interaction)
            save_key = selected_save(server_config(interaction, server, "reconcile"))
            record = await asyncio.to_thread(self.transfers.create, kind.value, str(interaction.user.id), server,
                                             save_key, source_farm_id, destination_farm_id, item, quantity)
            await interaction.response.send_message(f"Transfer `{record['transfer_id']}` requested.", ephemeral=True)

        @transfer_request.autocomplete("server")
        async def transfer_request_server_autocomplete(interaction: discord.Interaction, current: str):
            return await server_choices(interaction, current, "reconcile")

        @self.tree.command(name="transfer_list", description="Staff: list durable farm transfers")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def transfer_list(interaction: discord.Interaction):
            staff_check(interaction)
            records = await asyncio.to_thread(lambda: list(self.transfers.db.transfers.find({}).sort("created_at", -1).limit(50)))
            text = "\n".join(f"`{r['transfer_id']}` {r['kind']} {r['item']} — {r['status']}" for r in records)
            await interaction.response.send_message(text or "No transfers.", ephemeral=True)

        @self.tree.command(name="transfer_accept", description="Staff: accept a durable farm transfer")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def transfer_accept(interaction: discord.Interaction, transfer_id: str):
            staff_check(interaction)
            record = await asyncio.to_thread(self.transfers.accept, transfer_id, str(interaction.user.id))
            await interaction.response.send_message(f"Transfer `{record['transfer_id']}` accepted.", ephemeral=True)

        @self.tree.command(name="transfer_dispatch", description="Staff: queue an accepted transfer for FS25")
        @app_commands.check(channel_check)
        @app_commands.default_permissions(administrator=True)
        async def transfer_dispatch(interaction: discord.Interaction, transfer_id: str):
            staff_check(interaction)
            operation_id = await asyncio.to_thread(self.transfers.queue_game_operation, transfer_id, str(interaction.user.id))
            await interaction.response.send_message(f"Transfer operation `{operation_id}` queued; receipt confirmation is required.", ephemeral=True)

        @self.tree.command(name="activity_status", description="View your SiN activity telemetry")
        async def activity_status(interaction: discord.Interaction, member: discord.Member = None):
            if interaction.guild_id != self.guild.id:
                raise ValueError("Use this command in the configured Discord server")
            if member is not None:
                staff_check(interaction)
                require_command_channel(interaction.command.name, interaction.guild_id,
                                        self.guild.id, interaction.channel_id, self.channels)
            target = member or interaction.user
            await interaction.response.defer(ephemeral=True)
            context = await asyncio.to_thread(self.resolve_identity_context, str(target.id), None, "reconcile")
            status = await asyncio.to_thread(self.telemetry.status, context["server_key"], context["save_key"], context["unique_user_id"])
            aggregate = status.get("aggregate") or {}
            sessions = status.get("sessions") or []
            if not aggregate and not sessions:
                await interaction.followup.send("No activity telemetry is recorded for this registered identity.", ephemeral=True)
                return
            display = getattr(target, "display_name", None) or getattr(target, "name", None) or "Player"
            message = (f"Player: {display}\nServer: {context['server_name']}\n"
                       f"Connected: {aggregate.get('connected_minutes', 0)} min\n"
                       f"Active: {aggregate.get('active_minutes', 0)} min\n"
                       f"Idle: {aggregate.get('idle_minutes', 0)} min\n"
                       f"AFK: {aggregate.get('afk_minutes', 0)} min\n"
                       f"Current: {aggregate.get('current_state', 'unknown')}\n"
                       f"Inactivity streak: {aggregate.get('current_inactive_minutes', 0)} min\n"
                       f"Sessions observed: {len(sessions)}")
            await interaction.followup.send(message, ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())

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

    def resolve_identity_context(self, discord_id, server_key=None, purpose="reconcile"):
        """Resolve a registered Discord identity to one eligible server/save.

        Current connected observations win when several historical identity
        scopes exist. If there is still more than one eligible context, fail
        closed so a bank/game operation cannot be sent to an arbitrary save.
        """
        database = self.bank.database.db
        rows = list(database.game_identities.find({"discord_id": str(discord_id)}).limit(25))
        if not rows:
            raise ValueError("Your Discord account is not linked to a Farming Simulator identity")
        candidates = []
        for identity in rows:
            key = identity.get("server_id")
            if server_key is not None and key != server_key:
                continue
            try:
                config = self.server_registry.eligible_server(key, purpose)
            except ValueError:
                continue
            save_key = identity.get("save_id")
            if not save_key or not any(save.get("save_key") == save_key for save in config.get("saves", [])):
                continue
            observation = database.observed_fs25_identities.find_one({
                "server_id": key, "save_id": save_key,
                "fs25_unique_user_id": identity.get("fs25_unique_user_id"),
                "currently_connected": True,
            })
            request = self.farm_lifecycle.request_status(str(discord_id))
            candidates.append({
                "server_key": key, "save_key": save_key,
                "server_name": config.get("display_name") or key,
                "unique_user_id": identity.get("fs25_unique_user_id") or identity.get("game_player_id"),
                "farm_id": request.get("farm_id") if isinstance(request, dict)
                    and request.get("server_key") == key and request.get("save_key") == save_key else None,
                "connected": bool(observation),
            })
        connected = [candidate for candidate in candidates if candidate["connected"]]
        if len(connected) == 1:
            return connected[0]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ValueError("No eligible registered server/save context is available for your identity")
        raise ValueError("Your identity is associated with multiple game contexts; select a server")

    def withdrawals_enabled(self, server_key, config=None):
        record = self.server_registry.info(server_key) or {}
        if "withdrawals_enabled" in record:
            return bool(record.get("withdrawals_enabled"))
        return bool((config or self.servers.get(server_key) or {}).get("withdrawals_enabled", False))

    @staticmethod
    def contract_card_text(record):
        compensation = record.get("compensation_type", "fixed").title()
        rate = record.get("rate", record.get("value", 0))
        fields = record.get("fields") or "unspecified"
        creator = record.get("creator_discord_id")
        creator_text = f"<@{creator}>" if creator else "SiN member"
        status = str(record.get("status", "open")).replace("_", " ").title()
        server = record.get("server_name") or record.get("server_key")
        server_line = f"Server: {server}\n" if server else ""
        return (f"**{record.get('title', 'Farm work')}**\n"
                f"{record.get('description', '')}\n"
                f"Work: {str(record.get('work_type', 'general')).title()} | Fields: {fields}\n"
                f"{server_line}"
                f"Compensation: {compensation} {rate:,}\n"
                f"Posted by: {creator_text} | Status: {status}")

    @staticmethod
    def event_card_text(record):
        participants = record.get("participants", [])
        return (f"**{record.get('name', 'SiN event')}**\n{record.get('description', '')}\n"
                f"When: {discord_timestamp(record.get('scheduled_start'))}\n"
                f"Status: {str(record.get('status', 'scheduled')).title()} | "
                f"Participants: {len(participants)}"
                + (f"/{record['max_participants']}" if record.get("max_participants") else ""))

    def ensure_registered_map(self, server_key, save_key):
        """Load a validated runtime map projection before rendering a card.

        The Agent posts geometry to the server API process, while JiN runs in
        a separate central process.  Mongo is therefore the small durable
        handoff between those processes; no Discord request can provide a
        filesystem path or unvalidated map bytes.
        """
        if not server_key or not save_key:
            return False
        try:
            return self.map_service.load_persisted(MapStore(self.bank.database), server_key, save_key)
        except (MapValidationError, ValueError):
            logging.warning("Stored runtime map geometry is invalid; using text-only contract card")
            return False

    async def publish_contract_card(self, record):
        channel_id = self.channels.get("jobs")
        if not channel_id:
            return False
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.DiscordException, discord.HTTPException):
                logging.warning("Jobs channel is configured but unavailable; contract remains durable")
                return False
        try:
            attachment = None
            try:
                await asyncio.to_thread(self.ensure_registered_map, record.get("server_key"),
                                        record.get("save_key"))
                image = await asyncio.to_thread(
                    self.map_service.render_contract_map, record.get("server_key"),
                    record.get("save_key"), record.get("fields"))
                attachment = discord.File(io.BytesIO(image), filename="sin-map.png")
            except Exception as error:
                logging.info("Contract map rendering unavailable; posting text-only card: %s", error)
            send_kwargs = {"content": self.contract_card_text(record),
                           "view": ContractView(self, record["contract_id"])}
            if attachment is not None:
                send_kwargs["file"] = attachment
            message = await channel.send(**send_kwargs)
        except discord.DiscordException:
            logging.warning("Jobs channel publish failed; contract remains durable")
            return False
        await asyncio.to_thread(self.contracts.set_marketplace_message, record["contract_id"],
                                channel_id, message.id)
        return True

    async def publish_event_card(self, record):
        channel_id = self.channels.get("events")
        if not channel_id:
            return False
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.DiscordException:
                logging.warning("Events channel is configured but unavailable; event remains durable")
                return False
        try:
            message = await channel.send(content=self.event_card_text(record),
                                         view=CommunityEventView(self, record["event_id"]))
        except discord.DiscordException:
            logging.warning("Events channel publish failed; event remains durable")
            return False
        await asyncio.to_thread(self.community_events.set_board_message, record["event_id"], channel_id, message.id)
        return True

    async def restore_contract_views(self):
        if self._contract_views_restored:
            return
        self._contract_views_restored = True
        records = await asyncio.to_thread(self.contracts.open)
        for record in records:
            channel_id = record.get("marketplace_channel_id")
            message_id = record.get("marketplace_message_id")
            if channel_id and message_id:
                self.add_view(ContractView(self, record["contract_id"]), message_id=int(message_id))

    async def restore_event_views(self):
        records = await asyncio.to_thread(self.community_events.list)
        for record in records:
            if record.get("status") not in {"scheduled", "active"}:
                continue
            channel_id = record.get("board_channel_id")
            message_id = record.get("board_message_id")
            if channel_id and message_id:
                self.add_view(CommunityEventView(self, record["event_id"]), message_id=int(message_id))

    async def setup_hook(self):
        self.tree.copy_global_to(guild=self.guild)
        if not await self._sync_command_registry("setup"):
            raise DiscordSetupError("Discord command registry could not be synchronized; inspect JiN logs and retry.")
        await self.restore_contract_views()
        await self.restore_event_views()

    async def _sync_command_registry(self, reason):
        async with self._command_sync_lock:
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
            except Exception:
                logging.exception("Discord command registry sync failed reason=%s", reason)
                return False
        logging.info("Registered %s commands in Discord server %s reason=%s", len(commands), self.guild.id, reason)
        return True

    async def on_ready(self):
        logging.info("Discord bot connected as %s (ID %s)", self.user, self.user.id)
        self.activity_publisher.start()

    async def on_resumed(self):
        logging.info("Discord gateway resumed; refreshing command registry")
        await self._sync_command_registry("gateway_resumed")

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
    channels = dict(channels)
    # Deployment may keep channel IDs outside the repository config.  These
    # explicit overrides are non-secret and avoid embedding a live guild ID in
    # business logic; discord.json remains the normal source of truth.
    for env_name, channel_name in (("DISCORD_JOBS_CHANNEL_ID", "jobs"),
                                   ("DISCORD_EVENTS_CHANNEL_ID", "events")):
        configured = os.environ.get(env_name)
        if configured:
            channels[channel_name] = configured
    try:
        sin_member_role_id = int(discord_config.get("roles", {}).get("sin_member", ""))
        for name in ("sin_apply", "sin_applications"):
            int(channels[name])
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit("Configure numeric channels.sin_apply, channels.sin_applications, and roles.sin_member IDs in discord.json") from error
    # ``servers.json`` is a legacy local-development adapter.  Production JiN
    # discovery comes from sin_servers/sin_saves in the configured database;
    # loading the file by default would reintroduce local-dev into Discord and
    # could also bootstrap stale save mappings.  Opt into it explicitly for
    # local testing with FS25_SERVERS_FILE=... .
    servers = {}
    servers_file = os.environ.get("FS25_SERVERS_FILE")
    if servers_file:
        with open(servers_file, encoding="utf-8") as stream:
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
        NetworkBot(BankingEngine(database), servers, int(guild_id), operator_role_ids, channels, authorizations,
                   sin_member_role_id, os.environ.get("DISCORD_TIMEZONE", "UTC")).run(token, log_handler=None)
    except DiscordSetupError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
