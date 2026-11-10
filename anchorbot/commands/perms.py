"""Administrator-only channel grants and single-use clear confirmations."""
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


class PermissionCommands:
    def __init__(self, app):
        self.app = app
        builder = hikari.impl.SlashCommandBuilder(
            "perms", "Manage channel-specific Anchor command access.",
            default_member_permissions=hikari.Permissions.ADMINISTRATOR,
            context_types=[hikari.ApplicationContextType.GUILD],
        )
        for action in ("add", "remove"):
            children = []
            for target, kind in (("user", hikari.OptionType.USER), ("role", hikari.OptionType.ROLE)):
                description = (f"Allow a {target} to use pin commands in one channel." if action == "add"
                               else f"Remove a {target}'s direct grant in one channel.")
                children.append(option(target, description,
                    hikari.OptionType.SUB_COMMAND, False, options=[
                        option(target, f"The {target} to {action}.", kind), channel_option(),
                    ]))
            builder.add_option(option(action, f"{action.title()} a channel grant.",
                                      hikari.OptionType.SUB_COMMAND_GROUP, False, options=children))
        builder.add_option(option("list", "List channel grants in this server.", hikari.OptionType.SUB_COMMAND,
            False, options=[channel_option(False), option("page", "Page of results.", hikari.OptionType.INTEGER,
                                                        False, min_value=1)]))
        builder.add_option(option("check", "Explain a member's Anchor command access.",
            hikari.OptionType.SUB_COMMAND, False, options=[
                option("user", "Member to check.", hikari.OptionType.USER), channel_option()]))
        builder.add_option(option("clear", "Remove every grant from one channel after confirmation.",
            hikari.OptionType.SUB_COMMAND, False, options=[channel_option()]))
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
        options = ctx.i.options or []
        if len(options) != 1:
            raise ValueError("Choose a valid `/perms` subcommand.")
        branch = options[0]
        action, target = branch.name, None
        if action in {"add", "remove"}:
            if not branch.options or len(branch.options) != 1:
                raise ValueError("Choose a user or role.")
            branch = branch.options[0]
            target = branch.name
            if target not in {"user", "role"}:
                raise ValueError("Choose a user or role.")
        values = {o.name: o.value for o in branch.options or []}
        if action not in {"add", "remove", "list", "check", "clear"}:
            raise ValueError("Choose a valid `/perms` subcommand.")
        try:
            channel = await self.target_channel(ctx, values["channel"]) if "channel" in values else None
            if action != "list" and channel is None:
                raise ValueError("Choose a channel.")
            if action in {"add", "remove"}:
                subject = str(int(values[target]))
                if target == "user":
                    member = await self.app.bot.rest.fetch_member(ctx.i.guild_id, int(subject))
                    if member.guild_id != ctx.i.guild_id:
                        raise ValueError("Choose a member of this server.")
                else:
                    roles = await self.app.bot.rest.fetch_roles(ctx.i.guild_id)
                    if subject == str(ctx.i.guild_id):
                        raise ValueError("The @everyone role cannot receive a channel grant. Choose a specific role.")
                    if not any(str(role.id) == subject and role.guild_id == ctx.i.guild_id for role in roles):
                        raise ValueError("Choose a role in this server.")
                await self.change(ctx, action, target, subject, channel)
            elif action == "list":
                await self.list(ctx, channel, int(values.get("page", 1)))
            elif action == "check":
                await self.check(ctx, str(int(values["user"])), channel)
            else:
                await self.clear(ctx, channel)
        except (hikari.NotFoundError, hikari.ForbiddenError):
            await ctx.reply("Unable to verify that member, role, or channel in this server. No permissions were changed.",
                            ephemeral=True)

    async def change(self, ctx, action, target, subject, channel):
        async with self.app.pins.locks[str(channel.id)]:
            state = self.app.pins.state(channel.id, ctx.i.guild_id)
            current = grants(state)
            key = "users" if target == "user" else "roles"
            exists = subject in current[key]
            mention = f"<@{subject}>" if target == "user" else f"<@&{subject}>"
            if action == "add" and not exists:
                if sum(map(len, current.values())) >= 100:
                    raise ValueError("This channel already has 100 grants. Remove a grant before adding another.")
                current[key].append(subject)
            elif action == "remove" and exists:
                current[key].remove(subject)
            changed = (action == "add") != exists
            if changed:
                state["command_grants"] = current
                self.app.db.save_channel(state)
                log.info("command_grant_%s guild=%s channel=%s actor=%s target=%s:%s",
                         action, ctx.i.guild_id, channel.id, ctx.i.user.id, target, subject)
        if not changed:
            text = (f"{mention} already has a {target} grant in <#{channel.id}>." if action == "add" else
                    f"{mention} has no direct {target} grant in <#{channel.id}>.")
            result = embed("Permissions unchanged", text)
        elif action == "add":
            result = embed("Channel access granted", f"{mention} can use Anchor pin commands in <#{channel.id}>.")
        else:
            result = embed("Channel grant removed", f"Removed {mention}'s {target} grant in <#{channel.id}>.")
        await ctx.reply(embed=result, ephemeral=True)

    async def list(self, ctx, channel, page):
        lines = []
        for state in sorted(self.app.db.channels(), key=lambda s: s["id"]):
            if state["guild_id"] != str(ctx.i.guild_id) or (channel and state["id"] != str(channel.id)):
                continue
            current = grants(state)
            for key in ("users", "roles"):
                for subject in current[key]:
                    mention = f"<@{subject}>" if key == "users" else f"<@&{subject}>"
                    lines.append(f"<#{state['id']}> · {mention} · {'User' if key == 'users' else 'Role'}")
        pages = max(1, math.ceil(len(lines) / 20))
        if not 1 <= page <= pages:
            raise ValueError(f"Choose a page from 1 to {pages}.")
        scope = f"Channel: <#{channel.id}>\n\n" if channel else ""
        result = embed("Anchor channel permissions", scope +
                       ("\n".join(lines[(page - 1) * 20:page * 20]) or "No channel grants configured."))
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
        current = grants(channel_state(self.app.db, ctx.i.guild_id, channel.id))
        user_grant = subject in current["users"]
        role_grants = [role for role in current["roles"] if int(role) in member.role_ids]
        allowed = admin or (member.communication_disabled_until() is None and (user_grant or bool(role_grants)))
        role_text = ", ".join(f"<@&{role}>" for role in role_grants) or "N/A"
        result = embed(
            "Anchor access check",
            f"Member: <@{subject}>\nChannel: <#{channel.id}>\n\n"
            f"**Anchor command access**\n{'Allowed' if allowed else 'Denied'}\n\n"
            f"User grant: {user_grant}\nRole grant: {role_text}",
        )
        await ctx.reply(embed=result, ephemeral=True)

    async def clear(self, ctx, channel):
        async with self.app.pins.locks[str(channel.id)]:
            current = grants(channel_state(self.app.db, ctx.i.guild_id, channel.id))
            count = sum(map(len, current.values()))
            if not count:
                await ctx.reply(embed=embed("Permissions unchanged", f"<#{channel.id}> has no channel grants to clear."), ephemeral=True)
                return
            token = secrets.token_urlsafe(24)
            now = time.time()
            expires = int(now + 60)
            with self.app.db.db:
                self.app.db.db.execute("DELETE FROM permission_confirmations WHERE expires<?", (now,))
                self.app.db.db.execute("INSERT INTO permission_confirmations VALUES (?,?,?,?,?,?)",
                    (token, str(ctx.i.guild_id), str(channel.id), str(ctx.i.user.id), expires,
                     json.dumps(current, sort_keys=True)))
        row = hikari.impl.MessageActionRowBuilder()
        row.add_interactive_button(hikari.ButtonStyle.DANGER, f"perms:confirm:{token}", label="Clear grants")
        row.add_interactive_button(hikari.ButtonStyle.SECONDARY, f"perms:cancel:{token}", label="Cancel")
        result = embed("Clear channel permissions?",
                       f"Remove **{count}** {'grant' if count == 1 else 'grants'} from <#{channel.id}>?\n\n"
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
                result = embed("Permission clear cancelled", "No channel grants were changed.")
            else:
                state = self.app.pins.states.get(row["channel"])
                current = grants(channel_state(self.app.db, interaction.guild_id, row["channel"]))
                if not state or state["guild_id"] != row["guild"] or json.dumps(current, sort_keys=True) != row["snapshot"]:
                    result = embed("Permissions changed", "The channel or its grants changed after this confirmation was created. Run `/perms clear` again.")
                else:
                    state.pop("command_grants", None)
                    self.app.db.save_channel(state)
                    log.info("command_grants_cleared guild=%s channel=%s actor=%s", row["guild"], row["channel"], row["actor"])
                    result = embed("Channel permissions cleared", f"Removed all grants from <#{row['channel']}>.")
        await interaction.edit_initial_response(embed=result, components=[], **SAFE_MENTIONS)
