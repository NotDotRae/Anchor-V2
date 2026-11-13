import time

import hikari

from ..command_access import grants, is_admin
from ..permissions import readable_channels
from ..style import SAFE_MENTIONS, embed, uptime


class UtilityCommands:
    def __init__(self, app):
        self.app = app
        descriptions = {
            "help": "Show Anchor commands.",
            "ping": "Show gateway ping.",
            "about": "Show Anchor information.",
            "bot perms": "Check Anchor channel permissions.",
        }
        for name, description in descriptions.items():
            app.router.add(name, description, self.handle)
        app.router.add(
            "user info",
            "Show a member's server and channel grants.",
            self.userinfo,
            ("user", "Member.", hikari.OptionType.USER, False),
        )

    async def handle(self, ctx):
        name = ctx.command_name
        if name in {"ping", "about"}:
            workers = self.app.db.workers()
            shard_id = (int(ctx.i.guild_id) >> 22) % self.app.count
            rows = [shard for worker in workers for shard in worker.get("shards", [])]
            pings = [s["latency_ms"] for s in rows if s.get("latency_ms") is not None]
            average = f"{round(sum(pings) / len(pings))}ms" if pings else "unavailable"
        if name == "help":
            result = embed("**-Anchor Commands-**", "Use slash commands to manage Anchor.")
            result.add_field(
                "Pin Commands:",
                "`/anchor start <message>`\n`/anchor embed <message>`\n`/anchor slow <message>`\n`/anchor webhook <message>`\n`/anchor stop`\n`/anchor list`\n`/pause`, `/resume`\n`/anchor frequency seconds:5–120`\n`/anchor expire after:2h` (or `off`)\n`/stick` commands work as aliases.",
            )
            result.add_field(
                "Utility Commands:",
                "`/user info`, `/bot perms`, `/ping`, `/about` (support, donation, and Terms links)",
            )
            result.add_field("Settings:", "`/image set`, `/image get`, `/image remove`, `/image big set`, `/image big remove`, `/webhook set`, `/webhook stop`")
            if is_admin(ctx.i):
                result.add_field("Administrator permissions:",
                    "`/perms add user`, `/perms add role`, `/perms remove user`, `/perms remove role`, "
                    "`/perms list`, `/perms check`, `/perms clear`\n"
                    "Choose `scope:server` or `scope:channel`. Channel scope also needs `channel`. "
                    "Server and channel grants add together; Discord permissions still apply.")
            await ctx.reply(embed=result, ephemeral=True)
        elif name == "ping":
            current = next((s.get("latency_ms") for s in rows if s["id"] == shard_id), None)
            ping = f"{round(current)}ms" if current is not None else "unavailable"
            await ctx.reply(
                f">>> **Pong!**\nThis Shard: (`{shard_id}`) Ping: `{ping}`.\nAll Shards Average Ping: `{average}`."
            )
        elif name == "about":
            guilds = sum(w.get("guilds", 0) for w in workers)
            result = embed("**-Anchor Information-**")
            result.add_field("Developed By:", f"NotDotRae\n(`{self.app.config['owner_id']}`)")
            result.add_field("Ping:", average)
            result.add_field("Uptime:", f"``{uptime(time.time() - self.app.started)}``", inline=True)
            result.add_field("Shards:", f"Shard **{shard_id} of {self.app.count}**")
            result.add_field("Guilds:", f"Anchor is in **{guilds:,}** Guilds")
            result.add_field("Included Features:", "All pin and embed features are free to use.")
            result.set_footer("Anchor is Made with Python & Hikari", icon=self.app.avatar)
            links = hikari.impl.MessageActionRowBuilder()
            for key, label in (("kofi_url", "Donate"), ("support_url", "Support"),
                               ("tos_url", "Terms of Service")):
                if url := self.app.config.get(key):
                    links.add_link_button(url, label=label)
            await ctx.reply(embed=result, components=[links] if links.components else [])
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
        result = embed("User Anchor grants", f"Member: <@{user.id}>")
        result.set_footer(user.username, icon=user.display_avatar_url)
        server = grants({"command_grants": self.app.db.server_grants(ctx.i.guild_id)})
        server_roles = [role for role in server["roles"] if int(role) in member.role_ids]
        server_text = (f"User grant: {str(user.id) in server['users']}\nRole grants: "
                       + (", ".join(f"<@&{role}>" for role in server_roles) or "N/A"))
        for offset in range(0, len(server_text), 1000):
            result.add_field("Server grants", server_text[offset:offset + 1000])
        server_fields = len(result.fields)
        pages = [result]
        found_channel = False
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
                found_channel = True
                text = f"User grant: {direct}\nRole grant: " + (", ".join(f"<@&{role}>" for role in roles) or "N/A")
                while text:
                    cut = len(text) if len(text) <= 1000 else text.rfind(" ", 0, 1000)
                    chunk, text = text[:cut], text[cut:].lstrip()
                    current_page = pages[-1]
                    if len(current_page.fields) >= 20 or current_page.total_length() + len(chunk) + 50 > 5500:
                        pages.append(embed("User channel grants").set_footer(user.username))
                    pages[-1].add_field(f"Channel: <#{state['id']}>", chunk)
        if not found_channel and len(result.fields) == server_fields:
            result.add_field("Channel grants", "No grants found in channels you can read.")
        await ctx.reply(embed=pages[0], ephemeral=True)
        for page in pages[1:]:
            await ctx.i.execute(embed=page, flags=hikari.MessageFlag.EPHEMERAL, **SAFE_MENTIONS)
