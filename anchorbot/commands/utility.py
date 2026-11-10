import time
from datetime import UTC, datetime

import hikari

from ..command_access import grants, is_admin
from ..permissions import readable_channels
from ..style import SAFE_MENTIONS, embed, uptime


class UtilityCommands:
    def __init__(self, app):
        self.app = app
        descriptions = {
            "help": "Show Anchor commands.",
            "commands": "Show Anchor commands.",
            "ping": "Show gateway ping.",
            "about": "Show Anchor information.",
            "server info": "Show server information.",
            "uptime": "Show Anchor uptime.",
            "bot perms": "Check Anchor channel permissions.",
            "shard info": "Show shard info.",
            "shard ping": "Show shard ping info.",
            "donate": "Support Anchor on Ko-fi.",
            "tos": "View Anchor's Terms of Service.",
            "support": "Get the Anchor support server link.",
        }
        for name, description in descriptions.items():
            app.router.add(name, description, self.handle)
        app.router.add(
            "shard get",
            "Get the shard for a server ID.",
            self.handle,
            ("server_id", "Server ID.", hikari.OptionType.STRING, True),
            maximum=20,
        )
        app.router.add(
            "user info",
            "Show member information and granted channel access.",
            self.userinfo,
            ("user", "Member.", hikari.OptionType.USER, False),
        )

    def shard_rows(self):
        return [shard for worker in self.app.db.workers() for shard in worker.get("shards", [])]

    async def handle(self, ctx):
        name = ctx.command_name
        shard_id = (int(ctx.i.guild_id) >> 22) % self.app.count
        rows = self.shard_rows()
        pings = [s["latency_ms"] for s in rows if s.get("latency_ms") is not None]
        average = round(sum(pings) / len(pings)) if pings else "unavailable"
        if name in {"help", "commands"}:
            result = embed("**-Anchor Commands-**", "Use slash commands to manage Anchor.")
            result.add_field(
                "Pin Commands:",
                "`/stick start <message>`\n`/stick embed <message>`\n`/stick slow <message>`\n`/stick webhook <message>`\n`/stick stop`\n`/stick list`",
            )
            result.add_field(
                "Utility Commands:",
                "`/user info`, `/server info`, `/bot perms`, `/tos`, `/support`",
            )
            result.add_field("Settings:", "`/image set`, `/image get`, `/image remove`, `/image big set`, `/image big get`, `/image big remove`, `/webhook set`, `/webhook stop`")
            if is_admin(ctx.i):
                result.add_field("Administrator permissions:",
                    "`/perms add user`, `/perms add role`, `/perms remove user`, `/perms remove role`, "
                    "`/perms list`, `/perms check`, `/perms clear`")
            button = hikari.impl.MessageActionRowBuilder().add_link_button(
                self.app.config["kofi_url"], label="Donate to keep this project free"
            )
            await ctx.reply(embed=result, components=[button], ephemeral=True)
        elif name == "donate":
            button = hikari.impl.MessageActionRowBuilder().add_link_button(
                self.app.config["kofi_url"], label="Donate on Ko-fi"
            )
            await ctx.reply(
                embed=embed("-Anchor-", "Donate to keep this project free."),
                components=[button],
            )
        elif name == "support":
            url = self.app.config.get("support_url", "")
            if not url:
                await ctx.reply("The support server link has not been configured yet.", ephemeral=True)
                return
            await ctx.reply(
                embed=embed("Anchor Support", f"[Join the support server]({url})"),
            )
        elif name == "tos":
            url = self.app.config.get("tos_url", "")
            if not url:
                await ctx.reply("The Terms of Service link has not been configured yet.", ephemeral=True)
                return
            button = hikari.impl.MessageActionRowBuilder().add_link_button(url, label="Terms of Service")
            await ctx.reply(
                embed=embed("-Anchor Terms of Service-", f"[Read the Terms of Service]({url})"),
                components=[button],
            )
        elif name == "uptime":
            await ctx.reply(f"Uptime: ``{uptime(time.time() - self.app.started)}``")
        elif name == "ping":
            current = next((s.get("latency_ms") for s in rows if s["id"] == shard_id), None)
            ping = f"{round(current)}ms" if current is not None else "unavailable"
            await ctx.reply(
                f">>> **Pong!**\nThis Shard: (`{shard_id}`) Ping: `{ping}`.\nAll Shards Average Ping: `{average}`ms."
            )
        elif name == "shard info":
            await ctx.reply(
                embed=embed("-Shard Info-").add_field(
                    "Shards:", f"Total Shards: {self.app.count}\nThis Guilds Shard: {shard_id}"
                )
            )
        elif name == "shard get":
            server = ctx.value("server_id")
            if not server.isdecimal() or not 0 < int(server) < 2**64:
                raise ValueError("Something went wrong. Use a numeric server ID.")
            await ctx.reply(
                f"`{server}` is on shard: **{(int(server) >> 22) % self.app.count}**", ephemeral=True
            )
        elif name == "shard ping":
            if not await ctx.can_post():
                return
            lines = [f"__**Shard Pings:**__\n`Average: {average}`\n"]
            lines += [
                f"**Shard:** {s['id']} / {self.app.count} **Ping:** {round(s['latency_ms']) if s.get('latency_ms') is not None else 'unavailable'}ms. **Status:** {s['status']}\n"
                for s in rows
            ]
            chunks, chunk = [], ""
            for line in lines:
                if len(chunk) + len(line) > 1900:
                    chunks.append(chunk)
                    chunk = ""
                chunk += line
            chunks.append(chunk)
            await ctx.reply(chunks[0])
            for chunk in chunks[1:]:
                await ctx.i.execute(chunk, **SAFE_MENTIONS)
        elif name == "about":
            guilds = sum(w.get("guilds", 0) for w in self.app.db.workers())
            result = embed("**-Anchor Information-**")
            result.add_field("Developed By:", f"NotDotRae\n(`{self.app.config['owner_id']}`)")
            result.add_field("Ping:", f"{average}ms")
            result.add_field("Uptime:", f"``{uptime(time.time() - self.app.started)}``", inline=True)
            result.add_field("Shards:", f"Shard **{shard_id} of {self.app.count}**")
            result.add_field("Guilds:", f"Anchor is in **{guilds:,}** Guilds")
            result.add_field("Included Features:", "All pin and embed features are free to use.")
            result.set_footer("Anchor is Made with Python & Hikari", icon=self.app.avatar)
            await ctx.reply(embed=result)
        elif name == "bot perms":
            permissions = ctx.i.app_permissions
            send = (hikari.Permissions.SEND_MESSAGES_IN_THREADS if ctx.i.channel.type in {
                hikari.ChannelType.GUILD_PUBLIC_THREAD, hikari.ChannelType.GUILD_PRIVATE_THREAD,
                hikari.ChannelType.GUILD_NEWS_THREAD,
            } else hikari.Permissions.SEND_MESSAGES)
            lines = []
            for label, permission in (
                ("View Channel", hikari.Permissions.VIEW_CHANNEL),
                ("Send Messages", send),
                ("Read Message History", hikari.Permissions.READ_MESSAGE_HISTORY),
                ("Manage Messages", hikari.Permissions.MANAGE_MESSAGES),
                ("Embed Links", hikari.Permissions.EMBED_LINKS),
                ("Add Reactions", hikari.Permissions.ADD_REACTIONS),
                ("Manage Webhooks", hikari.Permissions.MANAGE_WEBHOOKS),
            ):
                allowed = bool(permissions & (permission | hikari.Permissions.ADMINISTRATOR))
                lines.append(f"**{label}:** {allowed}")
            await ctx.reply(embed=embed("Anchor bot permissions", f"Channel: <#{ctx.i.channel_id}>\n\n" + "\n".join(lines)), ephemeral=True)
        elif name == "server info":
            await self.serverinfo(ctx)

    async def userinfo(self, ctx):
        await ctx.defer(ephemeral=True)
        user_id = ctx.value("user", ctx.i.user.id)
        try:
            member = await self.app.bot.rest.fetch_member(ctx.i.guild_id, user_id)
        except hikari.NotFoundError:
            await ctx.reply("That user is not in this server.")
            return
        if member.guild_id != ctx.i.guild_id:
            raise ValueError("That user is not in this server.")
        user = member.user
        now = datetime.now(UTC)

        def days(date):
            return (now - date).days

        boost = (
            f"Boosting since: {member.premium_since.strftime('%B').upper()} {member.premium_since.day}, {member.premium_since.year} *({days(member.premium_since)} days ago)*"
            if member.premium_since
            else "Member not boosting."
        )
        roles = (
            " ".join(f"<@&{role}>" for role in reversed(member.role_ids) if role != ctx.i.guild_id) or "None"
        )
        role_count = sum(role != ctx.i.guild_id for role in member.role_ids)
        joined = (
            f"<t:{int(member.joined_at.timestamp())}:R>, *{days(member.joined_at):,}* days"
            if member.joined_at
            else "Unknown"
        )
        value = (
            f"**User ID:** ``{user.id}``\n**Nickname:** {member.display_name}\n"
            f"**Join Date:** {joined}\n**Creation Date:** <t:{int(user.created_at.timestamp())}:R>, *{days(user.created_at):,}* days\n"
            f"**Tag:** <@{user.id}>\n**Nitro Boosting:** {boost}\n**Number of Roles:** {role_count}"
        )
        result = embed("**-User Info-**").set_thumbnail(user.display_avatar_url)
        result.add_field(f"Info for {user.global_name or user.username}", value)
        result.add_field(
            "**Roles:**",
            roles if len(roles) <= 1000 else "Reached Max Embed Length. *(Too many roles to display)*",
        )
        result.set_footer(user.username, icon=user.display_avatar_url)
        pages = [result]
        try:
            readable = await readable_channels(ctx)
        except (hikari.ForbiddenError, hikari.NotFoundError):
            result.add_field("Channel grants", "Unavailable")
        else:
            for state in sorted(self.app.db.channels(), key=lambda s: s["id"]):
                if state["guild_id"] != str(ctx.i.guild_id) or state["id"] not in readable:
                    continue
                current = grants(state)
                direct = str(user.id) in current["users"]
                roles = [role for role in current["roles"] if int(role) in member.role_ids]
                if not direct and not roles:
                    continue
                text = f"User grant: {direct}\nRole grant: " + (", ".join(f"<@&{role}>" for role in roles) or "N/A")
                while text:
                    cut = len(text) if len(text) <= 1000 else text.rfind(" ", 0, 1000)
                    chunk, text = text[:cut], text[cut:].lstrip()
                    current_page = pages[-1]
                    if len(current_page.fields) >= 20 or current_page.total_length() + len(chunk) + 50 > 5500:
                        pages.append(embed("User channel grants").set_footer(user.username))
                    pages[-1].add_field(f"Channel: <#{state['id']}>", chunk)
        await ctx.reply(embed=pages[0], ephemeral=True)
        for page in pages[1:]:
            await ctx.i.execute(embed=page, flags=hikari.MessageFlag.EPHEMERAL, **SAFE_MENTIONS)

    async def serverinfo(self, ctx):
        await ctx.defer()
        guild = await self.app.bot.rest.fetch_guild(ctx.i.guild_id)
        channels = await self.app.bot.rest.fetch_guild_channels(ctx.i.guild_id)
        created = guild.created_at
        date = f"{created.strftime('%B').upper()} {created.day}, {created.year}"
        boosts = (
            f"TIER_{int(guild.premium_tier)}, {guild.premium_subscription_count} Boosts."
            if guild.premium_subscription_count
            else "Tier 0"
        )
        value = (
            f"**Server ID:** ``{guild.id}``\n**Creation Date:** {date} *({(datetime.now(UTC) - created).days} days ago)*\n"
            f"**Members:** {guild.approximate_member_count or 0:,}\n**Owner:** <@{guild.owner_id}>\n**Locale:** {guild.preferred_locale}\n"
            f"**Nitro Boosting:** {boosts}\n**Number of Roles:** {len(guild.roles)}\n"
            f"**Text Channels:** {sum(c.type == hikari.ChannelType.GUILD_TEXT for c in channels)}\n"
            f"**Voice Channels:** {sum(c.type == hikari.ChannelType.GUILD_VOICE for c in channels)}"
        )
        icon = guild.make_icon_url()
        result = embed("**-Server Info-**").set_thumbnail(icon)
        result.add_field("Info for " + guild.name, value).set_footer(guild.name, icon=icon)
        await ctx.reply(embed=result)
