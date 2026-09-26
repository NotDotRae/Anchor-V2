import logging

import hikari

from .style import SAFE_MENTIONS, embed

log = logging.getLogger(__name__)


class GuildEvents:
    def __init__(self, app):
        self.app = app
        app.bot.subscribe(hikari.GuildJoinEvent, self.join)
        app.bot.subscribe(hikari.GuildLeaveEvent, self.leave)

    async def notify_owner(self, guild_id, guild, *, joined):
        result = embed("AnchorBot Joined a New Server!" if joined else "AnchorBot Was Removed From a Server")
        result.color = 0x00FF00 if joined else 0xFF0000
        result.add_field("Server Name", guild.name if guild else "Unavailable")
        result.add_field("Server ID", str(guild_id))
        if guild:
            if guild.member_count is not None:
                result.add_field("Guild Members", f"{guild.member_count:,}")
            result.add_field("Guild Locale", str(guild.preferred_locale))
            result.add_field("Guild Owner Tag", f"<@{guild.owner_id}>")
            result.add_field("Guild Owner ID", str(guild.owner_id))
            result.set_thumbnail(guild.make_icon_url())
        try:
            channel = await self.app.bot.rest.create_dm_channel(int(self.app.config["owner_id"]))
            await self.app.bot.rest.create_message(channel.id, embed=result, **SAFE_MENTIONS)
        except hikari.HikariError:
            log.exception("owner_guild_dm_failed guild=%s event=%s", guild_id, "join" if joined else "leave")

    async def join(self, event):
        guild = event.guild
        log.info("guild_join guild=%s shard=%s", guild.id, event.shard.id)
        await self.notify_owner(guild.id, guild, joined=True)
        if self.app.config.get("welcome_dm"):
            result = embed(
                "**Thank You For Adding AnchorBot To Your Server!**",
                "Here are the basics to get you started:",
            )
            result.add_field(
                "Note:",
                "The pinned message is sent every 5 messages or 15 seconds to comply with discord TOS.",
            )
            result.add_field("**Commands:** ", "Use `/help` to see all commands.")
            result.add_field(
                "Issues?",
                "Make sure the bot has permission to send messages, delete messages, and bypass slow mode in pin channels.",
            )
            result.add_field(
                "Included Features: ",
                '-Unlimited Pinned Messages.\n-Use Custom Embeds as Pins.\n-Create a pin embed with a custom name and profile pic.\n-Removes "Pinned Message:" header.\n-Slower Posting Pins.\n-All features are free to use.\n-More to come!',
            )
            result.set_footer("AnchorBot", icon=self.app.avatar)
            try:
                channel = await self.app.bot.rest.create_dm_channel(guild.owner_id)
                await self.app.bot.rest.create_message(channel.id, embed=result)
            except hikari.HikariError:
                log.info("welcome_dm_unavailable guild=%s", guild.id)

    async def leave(self, event):
        log.info("guild_leave guild=%s shard=%s", event.guild_id, event.shard.id)
        with self.app.db.db:
            self.app.db.db.execute("DELETE FROM guild_load WHERE guild=?", (str(event.guild_id),))
        await self.notify_owner(event.guild_id, event.old_guild, joined=False)
