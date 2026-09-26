import time
from datetime import UTC, datetime

import hikari

from ..style import SAFE_MENTIONS, embed, uptime

FEATURES = '-Unlimited pinned messages.\n-Use Custom Embeds as pins.\n-Create slower posting pins.\n-Removes "Pinned Message:" header.\n-All features are free to use.\n-More to come!'


class UtilityCommands:
    def __init__(self, app):
        self.app = app
        descriptions = {
            "help": "Show AnchorBot commands.",
            "commands": "Show AnchorBot commands.",
            "ping": "Show gateway ping.",
            "about": "Show AnchorBot information.",
            "serverinfo": "Show server information.",
            "uptime": "Show AnchorBot uptime.",
            "features": "Show AnchorBot features.",
            "anchorbotfeatures": "Show AnchorBot features.",
            "permcheck": "Check AnchorBot channel permissions.",
            "shard": "Show shard info.",
            "shards": "Show shard info.",
            "shardping": "Show shard ping info.",
            "donate": "Support AnchorBot on Ko-fi.",
            "tos": "View AnchorBot's Terms of Service.",
        }
        for name, description in descriptions.items():
            app.router.add(name, description, self.handle)
        for name, description, key, option_description, limit in (
            ("poll", "Create a yes/no poll.", "question", "Poll question.", 4000),
            ("apoll", "Create a multiple-choice poll.", "poll", "Question, option1, option2...", 1000),
            ("advancedpoll", "Create a multiple-choice poll.", "poll", "Question, option1, option2...", 1000),
            ("embed", "Send your text as an embed.", "message", "Embed text.", 1000),
            ("getshard", "Get the shard for a server ID.", "server_id", "Server ID.", 20),
        ):
            app.router.add(
                name,
                description,
                self.handle,
                (key, option_description, hikari.OptionType.STRING, True),
                maximum=limit,
            )
        app.router.add(
            "userinfo",
            "Show information for a member.",
            self.userinfo,
            ("user", "Member.", hikari.OptionType.USER, False),
        )

    def shard_rows(self):
        return [shard for worker in self.app.db.workers() for shard in worker.get("shards", [])]

    async def handle(self, ctx):
        name = ctx.i.command_name
        shard_id = (int(ctx.i.guild_id) >> 22) % self.app.count
        rows = self.shard_rows()
        pings = [s["latency_ms"] for s in rows if s.get("latency_ms") is not None]
        average = round(sum(pings) / len(pings)) if pings else "unavailable"
        if name in {"help", "commands"}:
            result = embed("**-AnchorBot Commands-**", "Use slash commands to manage AnchorBot.")
            result.add_field(
                "Pin Commands:",
                "`/stick <message>`\n`/stickembed <message>`\n`/stickslow <message>`\n`/stickstop`\n`/getstickies`",
            )
            result.add_field(
                "Utility Commands:",
                "`/poll`, `/apoll`, `/userinfo`, `/serverinfo`, `/embed`, `/permcheck`, `/features`, `/tos`",
            )
            result.add_field("Settings:", "`/setimage`, `/setbigimage`, `/setwebhook`")
            button = hikari.impl.MessageActionRowBuilder().add_link_button(
                self.app.config["kofi_url"], label="Donate to keep this project free"
            )
            await ctx.reply(embed=result, components=[button], ephemeral=True)
        elif name == "donate":
            button = hikari.impl.MessageActionRowBuilder().add_link_button(
                self.app.config["kofi_url"], label="Donate on Ko-fi"
            )
            await ctx.reply(
                embed=embed("-AnchorBot-", "Donate to keep this project free."),
                components=[button],
            )
        elif name == "tos":
            url = self.app.config.get("tos_url", "")
            if not url:
                await ctx.reply("The Terms of Service link has not been configured yet.", ephemeral=True)
                return
            button = hikari.impl.MessageActionRowBuilder().add_link_button(url, label="Terms of Service")
            await ctx.reply(
                embed=embed("-AnchorBot Terms of Service-", f"[Read the Terms of Service]({url})"),
                components=[button],
            )
        elif name in {"features", "anchorbotfeatures"}:
            await ctx.reply(embed=embed("-AnchorBot-").add_field("Features:", FEATURES))
        elif name == "uptime":
            await ctx.reply(f"Uptime: ``{uptime(time.time() - self.app.started)}``")
        elif name == "ping":
            current = next((s.get("latency_ms") for s in rows if s["id"] == shard_id), None)
            ping = f"{round(current)}ms" if current is not None else "unavailable"
            await ctx.reply(
                f">>> **Pong!**\nThis Shard: (`{shard_id}`) Ping: `{ping}`.\nAll Shards Average Ping: `{average}`ms."
            )
        elif name in {"shard", "shards"}:
            await ctx.reply(
                embed=embed("-Shard Info-").add_field(
                    "Shards:", f"Total Shards: {self.app.count}\nThis Guilds Shard: {shard_id}"
                )
            )
        elif name == "getshard":
            server = ctx.value("server_id")
            if not server.isdecimal() or not 0 < int(server) < 2**64:
                raise ValueError("Something went wrong. Use a numeric server ID.")
            await ctx.reply(
                f"`{server}` is on shard: **{(int(server) >> 22) % self.app.count}**", ephemeral=True
            )
        elif name == "shardping":
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
            result = embed("**-AnchorBot Information-**")
            result.add_field("Developed By:", f"NotDotRae\n(`{self.app.config['owner_id']}`)")
            result.add_field("Ping:", f"{average}ms")
            result.add_field("Uptime:", f"``{uptime(time.time() - self.app.started)}``", inline=True)
            result.add_field("Shards:", f"Shard **{shard_id} of {self.app.count}**")
            result.add_field("Guilds:", f"AnchorBot is in **{guilds:,}** Guilds")
            result.add_field("Included Features:", "All pin and embed features are free to use.")
            result.set_footer("AnchorBot is Made with Python & Hikari", icon=self.app.avatar)
            await ctx.reply(embed=result)
        elif name in {"poll", "apoll", "advancedpoll", "embed"}:
            await self.create(ctx)
        elif name == "permcheck":
            permissions = ctx.i.app_permissions
            result = "**AnchorBot has the following permissions in this CHANNEL:**\n\n"
            for label, permission in (
                ("Message History", hikari.Permissions.READ_MESSAGE_HISTORY),
                ("Manage Messages", hikari.Permissions.MANAGE_MESSAGES),
                ("Embed Links", hikari.Permissions.EMBED_LINKS),
                ("Add Message Reactions", hikari.Permissions.ADD_REACTIONS),
            ):
                allowed = bool(permissions & (permission | hikari.Permissions.ADMINISTRATOR))
                result += f"```{'java' if allowed else 'c'}\n{label}: {str(allowed).lower()}```"
            await ctx.reply(result, ephemeral=True)
        elif name == "serverinfo":
            await self.serverinfo(ctx)

    async def create(self, ctx):
        name = ctx.i.command_name
        emojis = []
        if name == "embed":
            message = ctx.value("message")
            if len(message) > 1000:
                raise ValueError("Text is too long; it must be under 1000 characters.")
            result = embed(description=message).set_footer(f"Embed By: {ctx.i.user.username}")
        else:
            result = embed().set_footer(f"Poll by: {ctx.i.user}", icon=ctx.i.user.make_avatar_url())
            if name == "poll":
                result.description = ctx.value("question")
                emojis = ["👍", "👎", "🤷"]
            else:
                parts = [p.strip() for p in ctx.value("poll").split(",")]
                if len(parts) < 3:
                    raise ValueError("Use this format: `Question, Option1, Option2`.")
                if len(parts) > 8:
                    raise ValueError("A poll supports a maximum of seven options.")
                result.title, result.description = "POLL:", parts[0]
                emojis = ["🇦", "🇧", "🇨", "🇩", "🇪", "🇫", "🇬"][: min(len(parts) - 1, 7)]
                result.add_field(
                    "Options:",
                    "".join(f"{emoji}**:** {option}\n" for emoji, option in zip(emojis, parts[1:])),
                )
        await ctx.defer(ephemeral=True)
        message = await self.app.bot.rest.create_message(ctx.i.channel_id, embed=result, **SAFE_MENTIONS)
        for emoji in emojis:
            await self.app.bot.rest.add_reaction(ctx.i.channel_id, message.id, emoji)
        await ctx.reply("Embed sent." if name == "embed" else "Poll created.")

    async def userinfo(self, ctx):
        await ctx.defer()
        user_id = ctx.value("user", ctx.i.user.id)
        try:
            member = await self.app.bot.rest.fetch_member(ctx.i.guild_id, user_id)
        except hikari.NotFoundError:
            await ctx.reply("That user is not in this server.")
            return
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
        await ctx.reply(embed=result)

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
