"""Command channel boundaries, independent of Discord command visibility."""

COMMAND_CHANNELS = {
    "apply": "sin_apply",
    "application_pending": "staff", "application_approve": "staff", "application_deny": "staff",
    "server_register": "staff", "server_info": "staff", "farm_requests": "staff",
    "farm_roster": "staff", "farm_status_staff": "staff", "farm_approve": "staff", "farmland_status": "staff",
    "farm_reject": "staff", "farm_assign": "staff", "event_create": "staff",
    "event_cancel": "staff", "event_complete": "staff", "transfer_request": "staff",
    "transfer_list": "staff", "transfer_accept": "staff", "transfer_dispatch": "staff",
    "server_reconcile": "operations", "chat_send": "operations", "activity_status": "operations",
}

# Member self-service commands still require the configured guild, but their
# channel is an organization/noise concern rather than an authorization
# boundary. Staff/operator workflows remain channel-scoped below.
UNRESTRICTED_MEMBER_COMMANDS = {
    "register", "farm_request", "farm_status", "balance", "deposit", "withdraw",
    "contract_create", "contract_list", "contract_view", "contract_accept", "contract_cancel",
    "contract_complete", "invoice_create", "invoice_list", "invoice_view", "invoice_pay",
    "invoice_cancel", "event_list", "event_view", "event_join", "event_leave",
}


def require_command_channel(command, guild_id, expected_guild_id, channel_id, channels):
    if guild_id != expected_guild_id:
        raise ValueError("Use this command in the configured Discord server")
    if command in UNRESTRICTED_MEMBER_COMMANDS:
        return
    channel_key = COMMAND_CHANNELS.get(command)
    if channel_key is None:
        raise ValueError("This command has no configured channel policy")
    expected_channel = channels.get(channel_key)
    if not expected_channel:
        raise ValueError("Staff must configure the channel for this command")
    if channel_id != int(expected_channel):
        raise ValueError(f"Use this command in <#{int(expected_channel)}>.")
