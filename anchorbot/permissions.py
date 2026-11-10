"""Permission checks for bot-mediated access to Discord content."""

import hikari


def channel_permissions(member, guild, roles, channel):
    """Resolve regular guild channels; callers must handle threads separately."""
    if member.guild_id != guild.id or channel.guild_id != guild.id:
        return hikari.Permissions.NONE
    if member.id == guild.owner_id:
        return hikari.Permissions.all_permissions()
    role_ids = {guild.id, *member.role_ids}
    permissions = hikari.Permissions.NONE
    for role in roles:
        if role.id in role_ids:
            permissions |= role.permissions
    if permissions & hikari.Permissions.ADMINISTRATOR:
        return hikari.Permissions.all_permissions()
    overwrites = channel.permission_overwrites
    everyone = overwrites.get(guild.id)
    if everyone:
        permissions = (permissions & ~everyone.deny) | everyone.allow
    allow = deny = hikari.Permissions.NONE
    for role_id in role_ids - {guild.id}:
        overwrite = overwrites.get(role_id)
        if overwrite:
            allow |= overwrite.allow
            deny |= overwrite.deny
    permissions = (permissions & ~deny) | allow
    overwrite = overwrites.get(member.id)
    if overwrite:
        permissions = (permissions & ~overwrite.deny) | overwrite.allow
    return permissions


async def readable_channels(ctx):
    """Use fresh permission data, never the bot's access, for pin listings.

    Guild channel enumeration excludes threads. Only the interaction's own
    thread may be listed, using the permissions supplied by Discord.
    """
    rest = ctx.app.bot.rest
    member = await rest.fetch_member(ctx.i.guild_id, ctx.i.user.id)
    guild = await rest.fetch_guild(ctx.i.guild_id)
    roles = await rest.fetch_roles(ctx.i.guild_id)
    channels = await rest.fetch_guild_channels(ctx.i.guild_id)
    required = hikari.Permissions.VIEW_CHANNEL | hikari.Permissions.READ_MESSAGE_HISTORY
    readable = {
        str(channel.id) for channel in channels
        if channel_permissions(member, guild, roles, channel) & required == required
    }
    current = ctx.i.member.permissions if ctx.i.member else hikari.Permissions.NONE
    if ctx.i.channel.type in {
        hikari.ChannelType.GUILD_PUBLIC_THREAD, hikari.ChannelType.GUILD_PRIVATE_THREAD,
        hikari.ChannelType.GUILD_NEWS_THREAD,
    } and (current & required == required or current & hikari.Permissions.ADMINISTRATOR):
        readable.add(str(ctx.i.channel_id))
    return readable
