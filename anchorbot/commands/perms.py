import json
import logging
import math
import secrets
import time

import hikari

from ..command_access import ADMIN_ONLY, channel_state, grants, is_admin
from ..style import SAFE_MENTIONS, embed

log = logging.getLogger(__name__)
CHANNEL_TYPES = (hikari.ChannelType.GUILD_TEXT, hikari.ChannelType.GUILD_NEWS,
                 hikari.ChannelType.GUILD_PUBLIC_THREAD, hikari.ChannelType.GUILD_PRIVATE_THREAD,
                 hikari.ChannelType.GUILD_NEWS_THREAD)
EXPIRED = "This confirmation has expired or was already used. Run `/perms clear` again."


def option(name, description, kind, required=True, **kwargs):
    return hikari.CommandOption(type=kind, name=name, description=description, is_required=required, **kwargs)


def channel_option(required=True):
    return option("channel", "Exact channel to configure; grants do not apply to other channels or threads.",
                  hikari.OptionType.CHANNEL, required, channel_types=CHANNEL_TYPES)


def scope_option(required=True):
    return option("scope", "Apply to the whole server or one exact channel.", hikari.OptionType.STRING,
                  required, choices=[hikari.CommandChoice(name="Server", value="server"),
                                     hikari.CommandChoice(name="Channel", value="channel")])


class PermissionCommands:
    def __init__(self, app):
        self.app = app
        builder = hikari.impl.SlashCommandBuilder(
            "perms", "Manage server and channel Anchor command access.",
            default_member_permissions=hikari.Permissions.ADMINISTRATOR,
            context_types=[hikari.ApplicationContextType.GUILD],
        )
        for action in ("add", "remove"):
            children = []
            for target, kind in (("user", hikari.OptionType.USER), ("role", hikari.OptionType.ROLE)):
                description = (f"Allow a {target} to use pin commands in the selected scope." if action == "add"
                               else f"Remove a {target}'s direct grant in the selected scope.")
                children.append(option(target, description,
                    hikari.OptionType.SUB_COMMAND, False, options=[
                        option(target, f"The {target} to {action}.", kind), scope_option(), channel_option(False),
                    ]))
            builder.add_option(option(action, f"{action.title()} an Anchor grant.",
                                      hikari.OptionType.SUB_COMMAND_GROUP, False, options=children))
        builder.add_option(option("list", "List server and channel grants.", hikari.OptionType.SUB_COMMAND,
            False, options=[scope_option(False), channel_option(False), option("page", "Page of results.", hikari.OptionType.INTEGER,
                                                        False, min_value=1)]))
        builder.add_option(option("check", "Explain a member's Anchor command access.",
            hikari.OptionType.SUB_COMMAND, False, options=[
                option("user", "Member to check.", hikari.OptionType.USER), channel_option()]))
        builder.add_option(option("clear", "Clear grants in one scope after confirmation.",
            hikari.OptionType.SUB_COMMAND, False, options=[scope_option(), channel_option(False)]))
        app.router.builders.append(builder)
        app.router.handlers["perms"] = self.handle

    async def target_channel(self, ctx, raw):
        channel = await self.app.bot.rest.fetch_channel(int(raw))
        if getattr(channel, "guild_id", None) != ctx.i.guild_id or channel.type not in CHANNEL_TYPES:
            raise ValueError("Choose a text channel or thread in this server.")
        return channel

    async def handle(self, ctx):
        if not is_admin(ctx.i):
            await ctx.reply(ADMIN_ONLY, ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        path = ctx.command_name.split()
        action = path[1] if len(path) >= 2 else None
        target = path[2] if len(path) == 3 else None
        if action not in {"add", "remove", "list", "check", "clear"}:
            raise ValueError("Choose a valid `/perms` subcommand.")
        if action in {"add", "remove"}:
            if target not in {"user", "role"}:
                raise ValueError("Choose a user or role.")
        elif len(path) != 2:
            raise ValueError("Choose a valid `/perms` subcommand.")
        values = ctx.options
        try:
            channel = await self.target_channel(ctx, values["channel"]) if "channel" in values else None
            scope = values.get("scope", "all" if action == "list" else "channel")
            if scope not in {"server", "channel", "all"} or (scope == "all" and action != "list"):
                raise ValueError("Choose server or channel scope.")
            if scope == "server" and channel is not None:
                raise ValueError("Do not choose a channel for server-wide permissions.")
            if (scope == "channel" and action != "list" or action == "check") and channel is None:
                raise ValueError("Choose a channel for channel permissions.")
            if action in {"add", "remove", "clear"} and "scope" not in values:
                raise ValueError("Choose server or channel scope.")
            if action in {"add", "remove"}:
                subject = str(int(values[target]))
                if target == "user":
                    member = await self.app.bot.rest.fetch_member(ctx.i.guild_id, int(subject))
                    if member.guild_id != ctx.i.guild_id:
                        raise ValueError("Choose a member of this server.")
                else:
                    roles = await self.app.bot.rest.fetch_roles(ctx.i.guild_id)
                    if subject == str(ctx.i.guild_id):
                        raise ValueError("The @everyone role cannot receive an Anchor grant. Choose a specific role.")
                    if not any(str(role.id) == subject and role.guild_id == ctx.i.guild_id for role in roles):
                        raise ValueError("Choose a role in this server.")
                await self.change(ctx, action, target, subject, channel)
            elif action == "list":
                await self.list(ctx, channel, int(values.get("page", 1)), scope)
            elif action == "check":
                await self.check(ctx, str(int(values["user"])), channel)
            else:
                await self.clear(ctx, channel)
        except (hikari.NotFoundError, hikari.ForbiddenError):
            await ctx.reply("Unable to verify that member, role, or channel in this server. No permissions were changed.",
                            ephemeral=True)

    def current(self, guild, channel):
        return (grants(channel_state(self.app.db, guild, channel.id)) if channel else
                grants({"command_grants": self.app.db.server_grants(guild)}))

    def save(self, guild, channel, current):
        if channel:
            state = self.app.pins.state(channel.id, guild)
            state["command_grants"] = current
            self.app.db.save_channel(state)
        else:
            if str(guild) in self.app.pins.departed_guilds:
                raise ValueError("Anchor is no longer in this server.")
            self.app.db.save_server_grants(guild, current)

    def lock_key(self, guild, channel):
        return str(channel.id) if channel else f"guild:{guild}"

    async def change(self, ctx, action, target, subject, channel):
        async with self.app.pins.locks[self.lock_key(ctx.i.guild_id, channel)]:
            current = self.current(ctx.i.guild_id, channel)
            key = "users" if target == "user" else "roles"
            exists = subject in current[key]
            mention = f"<@{subject}>" if target == "user" else f"<@&{subject}>"
            if action == "add" and not exists:
                if sum(map(len, current.values())) >= 100:
                    raise ValueError("This scope already has 100 grants. Remove a grant first.")
                current[key].append(subject)
            elif action == "remove" and exists:
                current[key].remove(subject)
            changed = (action == "add") != exists
            if changed:
                self.save(ctx.i.guild_id, channel, current)
                log.info("command_grant_%s guild=%s scope=%s actor=%s target=%s:%s",
                         action, ctx.i.guild_id, self.lock_key(ctx.i.guild_id, channel),
                         ctx.i.user.id, target, subject)
        scope = f"<#{channel.id}>" if channel else "the whole server"
        text = (f"{'Added' if action == 'add' else 'Removed'} {mention}'s {target} grant for {scope}."
                if changed else f"{mention}'s {target} grant for {scope} is already {'present' if exists else 'absent'}.")
        if action == "remove":
            text += " Other user, role, or server grants may still provide access."
        await ctx.reply(embed=embed("Anchor permissions", text), ephemeral=True)

    async def list(self, ctx, channel, page, scope="all"):
        lines = []
        states = []
        if scope in {"server", "all"} and channel is None:
            states.append(("Server", self.current(ctx.i.guild_id, None)))
        if scope in {"channel", "all"}:
            states.extend((f"<#{state['id']}>", grants(state)) for state in
                          sorted(self.app.db.channels(), key=lambda s: s["id"])
                          if state["guild_id"] == str(ctx.i.guild_id)
                          and (channel is None or state["id"] == str(channel.id)))
        for label, current in states:
            for key in ("users", "roles"):
                for subject in current[key]:
                    mention = f"<@{subject}>" if key == "users" else f"<@&{subject}>"
                    lines.append(f"{label} · {mention} · {'User' if key == 'users' else 'Role'}")
        pages = max(1, math.ceil(len(lines) / 20))
        if not 1 <= page <= pages:
            raise ValueError(f"Choose a page from 1 to {pages}.")
        result = embed("Anchor permissions",
                       "\n".join(lines[(page - 1) * 20:page * 20]) or "No grants configured in this scope.")
        result.set_footer(f"Page {page}/{pages} · {len(lines)} grants")
        await ctx.reply(embed=result, ephemeral=True)

    async def check(self, ctx, subject, channel):
        member = await self.app.bot.rest.fetch_member(ctx.i.guild_id, int(subject))
        if member.guild_id != ctx.i.guild_id:
            raise ValueError("Choose a member of this server.")
        roles = await self.app.bot.rest.fetch_roles(ctx.i.guild_id)
        guild = await self.app.bot.rest.fetch_guild(ctx.i.guild_id)
        admin = member.id == guild.owner_id or any(
            r.permissions & hikari.Permissions.ADMINISTRATOR for r in roles if r.id in member.role_ids)
        details = []
        granted = False
        for label, current in (("Server", self.current(ctx.i.guild_id, None)),
                               ("Channel", self.current(ctx.i.guild_id, channel))):
            direct = subject in current["users"]
            roles_matched = [role for role in current["roles"] if int(role) in member.role_ids]
            granted |= direct or bool(roles_matched)
            role_text = ", ".join(f"<@&{role}>" for role in roles_matched) or "N/A"
            details.append((label, f"User grant: {direct}\nRole grants: {role_text}"))
        allowed = admin or (member.communication_disabled_until() is None and granted)
        result = embed("Anchor access check",
                       f"Member: <@{subject}>\nChannel: <#{channel.id}>\n\n"
                       f"Anchor grant: {'Allowed' if allowed else 'Denied'}\nAdministrator: {admin}\n"
                       "\nDiscord channel visibility and posting permissions are also required.")
        for label, text in details:
            for offset in range(0, len(text), 1000):
                result.add_field(f"{label} grants", text[offset:offset + 1000])
        await ctx.reply(embed=result, ephemeral=True)

    async def clear(self, ctx, channel):
        async with self.app.pins.locks[self.lock_key(ctx.i.guild_id, channel)]:
            current = self.current(ctx.i.guild_id, channel)
            count = sum(map(len, current.values()))
            if not count:
                await ctx.reply(embed=embed("Permissions unchanged", "This scope has no grants to clear."), ephemeral=True)
                return
            token = secrets.token_urlsafe(24)
            now = time.time()
            expires = int(now + 60)
            with self.app.db.db:
                self.app.db.db.execute("DELETE FROM permission_confirmations WHERE expires<?", (now,))
                self.app.db.db.execute("INSERT INTO permission_confirmations VALUES (?,?,?,?,?,?)",
                    (token, str(ctx.i.guild_id), self.lock_key(ctx.i.guild_id, channel), str(ctx.i.user.id), expires,
                     json.dumps(current, sort_keys=True)))
        row = hikari.impl.MessageActionRowBuilder()
        row.add_interactive_button(hikari.ButtonStyle.DANGER, f"perms:confirm:{token}", label="Clear grants")
        row.add_interactive_button(hikari.ButtonStyle.SECONDARY, f"perms:cancel:{token}", label="Cancel")
        scope = f"<#{channel.id}>" if channel else "the whole server"
        result = embed("Clear Anchor permissions?",
                       f"Remove **{count}** {'grant' if count == 1 else 'grants'} from {scope}?\n\n"
                       f"Expires <t:{expires}:R>")
        await ctx.reply(embed=result, components=[row], ephemeral=True)

    async def component(self, interaction):
        # Component interactions carry a fresh member permission payload from Discord.
        from .router import Context

        ctx = Context(self.app, interaction)
        if not is_admin(interaction):
            await ctx.reply(ADMIN_ONLY, ephemeral=True)
            return
        parts = interaction.custom_id.split(":")
        if len(parts) != 3 or parts[1] not in {"confirm", "cancel"}:
            await ctx.reply(EXPIRED, ephemeral=True)
            return
        row = self.app.db.db.execute("SELECT * FROM permission_confirmations WHERE token=?", (parts[2],)).fetchone()
        if not row or row["expires"] < time.time():
            await ctx.reply(EXPIRED, ephemeral=True)
            return
        if row["guild"] != str(interaction.guild_id) or row["actor"] != str(interaction.user.id):
            await ctx.reply("Only the administrator who requested this confirmation can use it.", ephemeral=True)
            return
        await interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_UPDATE)
        async with self.app.pins.locks[row["channel"]]:
            # Consume atomically: repeated clicks and confirmations from other processes cannot replay it.
            with self.app.db.db:
                taken = self.app.db.db.execute("DELETE FROM permission_confirmations WHERE token=? AND expires>=? RETURNING token",
                                              (parts[2], time.time())).fetchone()
            if not taken:
                result = embed("Confirmation unavailable", EXPIRED)
            elif parts[1] == "cancel":
                result = embed("Permission clear cancelled", "No grants were changed.")
            else:
                server = row["channel"] == f"guild:{interaction.guild_id}"
                state = None if server else self.app.pins.states.get(row["channel"])
                current = (self.current(interaction.guild_id, None) if server else
                           grants(channel_state(self.app.db, interaction.guild_id, row["channel"])))
                valid = (str(interaction.guild_id) not in self.app.pins.departed_guilds if server
                         else state is not None and state["guild_id"] == row["guild"])
                if not valid or json.dumps(current, sort_keys=True) != row["snapshot"]:
                    result = embed("Permissions changed", "This scope changed. Run `/perms clear` again.")
                else:
                    if server:
                        self.app.db.save_server_grants(interaction.guild_id, {"users": [], "roles": []})
                    else:
                        state.pop("command_grants", None)
                        self.app.db.save_channel(state)
                    log.info("command_grants_cleared guild=%s scope=%s actor=%s",
                             row["guild"], row["channel"], row["actor"])
                    scope = "the whole server" if server else f"<#{row['channel']}>"
                    result = embed("Anchor permissions cleared", f"Removed all direct grants from {scope}.")
        await interaction.edit_initial_response(embed=result, components=[], **SAFE_MENTIONS)
