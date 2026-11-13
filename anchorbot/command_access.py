import json

import hikari

DENIED = "You do not have permission to use Anchor commands in this channel. Ask a server administrator for access."
ADMIN_ONLY = "Only server administrators can manage Anchor permissions."


def can_publish(interaction):
    """A bot grant must never override Discord's channel posting restrictions."""
    member = interaction.member
    if (not member or interaction.guild_id is None or member.guild_id != interaction.guild_id
            or member.communication_disabled_until() is not None):
        return False
    send = (hikari.Permissions.SEND_MESSAGES_IN_THREADS if interaction.channel.type in {
        hikari.ChannelType.GUILD_PUBLIC_THREAD, hikari.ChannelType.GUILD_PRIVATE_THREAD,
        hikari.ChannelType.GUILD_NEWS_THREAD,
    } else hikari.Permissions.SEND_MESSAGES)
    required = hikari.Permissions.VIEW_CHANNEL | send | hikari.Permissions.EMBED_LINKS
    return bool(member.permissions & hikari.Permissions.ADMINISTRATOR
                or member.permissions & required == required)


def is_admin(interaction):
    member = interaction.member
    return bool(member and interaction.guild_id is not None
                and getattr(member, "guild_id", None) == interaction.guild_id
                and member.permissions & hikari.Permissions.ADMINISTRATOR)


def grants(state):
    value = (state or {}).get("command_grants", {})
    if not isinstance(value, dict):
        return {"users": [], "roles": []}
    return {key: sorted({v for v in value.get(key, []) if isinstance(v, str) and v.isdecimal()})
            if isinstance(value.get(key, []), list) else [] for key in ("users", "roles")}


def channel_state(db, guild, channel):
    row = db.db.execute("SELECT value FROM channels WHERE id=? AND guild=?",
                        (str(channel), str(guild))).fetchone()
    return json.loads(row[0]) if row else None


def access_reason(db, interaction):
    member = interaction.member
    if not member or interaction.guild_id is None or member.guild_id != interaction.guild_id:
        return None
    if is_admin(interaction):
        return "Administrator"
    if not member.permissions & hikari.Permissions.VIEW_CHANNEL:
        return None
    if member.communication_disabled_until() is not None:
        return None
    for scope, current in (
        ("Server", grants({"command_grants": db.server_grants(interaction.guild_id)})),
        ("Channel", grants(channel_state(db, interaction.guild_id, interaction.channel_id))),
    ):
        if str(interaction.user.id) in current["users"]:
            return f"{scope} user grant"
        matched = sorted(set(map(str, member.role_ids)) & set(current["roles"]))
        if matched:
            return f"{scope} role grant: " + ", ".join(f"<@&{role}>" for role in matched)
    return None
