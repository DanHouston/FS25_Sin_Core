"""Command channel boundaries, independent of Discord command visibility."""

COMMAND_CHANNELS = {
    "apply": "sin_apply",
    "application_pending": "sin_applications",
    "application_approve": "sin_applications",
    "application_deny": "sin_applications",
    "register": "link_account",
    "server_register": "farm_approvals",
    "server_info": "farm_approvals",
    "farm_request": "link_account",
    "farm_requests": "farm_approvals",
    "farm_roster": "farm_approvals",
    "farm_approve": "farm_approvals",
    "farm_reject": "farm_approvals",
    "farm_status": "link_account",
    "balance": "bank",
    "deposit": "bank",
    "withdraw": "bank",
    "farm_assign": "farm_approvals",
    "server_reconcile": "farm_approvals",
    "activity_status": "farm_approvals",
    "chat_send": "farm_approvals",
    "contract_create": "link_account",
    "contract_list": "link_account",
    "contract_view": "link_account",
    "contract_accept": "link_account",
    "contract_cancel": "link_account",
    "contract_complete": "link_account",
    "invoice_create": "link_account",
    "invoice_list": "link_account",
    "invoice_view": "link_account",
    "invoice_pay": "link_account",
    "invoice_cancel": "link_account",
    "event_create": "farm_approvals",
    "event_list": "link_account",
    "event_view": "link_account",
    "event_join": "link_account",
    "event_leave": "link_account",
    "event_cancel": "farm_approvals",
    "event_complete": "farm_approvals",
    "transfer_request": "farm_approvals",
    "transfer_list": "farm_approvals",
    "transfer_accept": "farm_approvals",
    "transfer_dispatch": "farm_approvals",
}

# Member self-service commands still require the configured guild, but their
# channel is an organization/noise concern rather than an authorization
# boundary. Staff/operator workflows remain channel-scoped below.
UNRESTRICTED_MEMBER_COMMANDS = {
    "apply", "register", "farm_request", "farm_status", "balance", "deposit", "withdraw",
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
