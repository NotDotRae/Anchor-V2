import logging

import hikari

from ..command_access import DENIED, access_reason, can_publish
from ..discord_limits import guild_context
from ..style import SAFE_MENTIONS

log = logging.getLogger(__name__)


class Context:
    def __init__(self, app, interaction):
        self.app, self.i = app, interaction
        self.command_name = getattr(interaction, "command_name", "")
        options = getattr(interaction, "options", None) or []
        while len(options) == 1 and getattr(options[0], "type", None) in {
            hikari.OptionType.SUB_COMMAND, hikari.OptionType.SUB_COMMAND_GROUP,
        }:
            branch = options[0]
            self.command_name += " " + branch.name
            options = branch.options or []
        self.options = {o.name: o.value for o in options}
        self.responded = False

    def value(self, name, default=""):
        from ..style import clean

        value = self.options.get(name, default)
        return clean(value) if isinstance(value, str) else value

    async def defer(self, ephemeral=False):
        ephemeral = ephemeral or not can_publish(self.i)
        await self.i.create_initial_response(
            hikari.ResponseType.DEFERRED_MESSAGE_CREATE,
            flags=hikari.MessageFlag.EPHEMERAL if ephemeral else hikari.UNDEFINED,
        )
        self.responded = True

    async def reply(
        self,
        content=hikari.UNDEFINED,
        *,
        embed=hikari.UNDEFINED,
        embeds=hikari.UNDEFINED,
        ephemeral=False,
        components=hikari.UNDEFINED,
    ):
        ephemeral = ephemeral or not can_publish(self.i)
        kwargs = dict(content=content, embed=embed, embeds=embeds, components=components, **SAFE_MENTIONS)
        if self.responded:
            return await self.i.edit_initial_response(**kwargs)
        result = await self.i.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            flags=hikari.MessageFlag.EPHEMERAL if ephemeral else hikari.UNDEFINED,
            **kwargs,
        )
        self.responded = True
        return result

    async def manage(self):
        if self.authorized():
            return True
        await self.reply(DENIED, ephemeral=True)
        return False

    def authorized(self):
        return bool(access_reason(self.app.db, self.i))

    def require_access(self):
        if not self.authorized():
            raise ValueError(DENIED)

    def require_publish(self):
        self.require_access()
        if not can_publish(self.i):
            raise ValueError("You need permission to send messages and embed links in this channel.")

    async def can_post(self):
        if can_publish(self.i):
            return True
        await self.reply("You need permission to send messages and embed links in this channel.", ephemeral=True)
        return False

    async def owner(self):
        if int(self.i.user.id) == int(self.app.config["owner_id"]):
            return True
        await self.reply("Only configured bot owners can use this command.", ephemeral=True)
        return False


class Router:
    def __init__(self, app):
        self.app = app
        self.handlers = {}
        self.builders = []

    def add(self, name, description, handler, option=None, *, maximum=4000, subcommand=None):
        path = name.split() + ([subcommand] if subcommand else [])
        if not 1 <= len(path) <= 3:
            raise ValueError("Slash commands support a root, optional group, and subcommand")
        leaf_options = []
        if option:
            key, desc, kind, required = option
            leaf_options.append(hikari.CommandOption(
                type=kind, name=key, description=desc, is_required=required,
                max_length=maximum if kind == hikari.OptionType.STRING else None,
            ))
        builder = next((b for b in self.builders if b.name == path[0]), None)
        if builder is None:
            builder = hikari.impl.SlashCommandBuilder(
                path[0], description if len(path) == 1 else f"{path[0].title()} commands.",
                context_types=[hikari.ApplicationContextType.GUILD],
            )
            self.builders.append(builder)
        route = " ".join(path)
        if route in self.handlers:
            raise ValueError(f"Duplicate command: {route}")
        if len(path) == 1:
            for value in leaf_options:
                builder.add_option(value)
        else:
            leaf = hikari.CommandOption(type=hikari.OptionType.SUB_COMMAND,
                                       name=path[-1], description=description, options=leaf_options)
            if len(path) == 2:
                builder.add_option(leaf)
            else:
                group = next((o for o in builder.options if o.name == path[1]), None)
                if group is None:
                    group = hikari.CommandOption(type=hikari.OptionType.SUB_COMMAND_GROUP,
                        name=path[1], description=f"{path[1].title()} commands.", options=[])
                    builder.add_option(group)
                group.options.append(leaf)
        self.handlers[route] = handler

    async def dispatch(self, event):
        interaction = event.interaction
        if not isinstance(interaction, (hikari.CommandInteraction, hikari.ComponentInteraction)):
            return
        mode = self.app.db.interaction_mode(interaction.id)
        if mode is None:
            return
        if mode == "restarting":
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                "The bot is currently restarting",
                flags=hikari.MessageFlag.EPHEMERAL,
                **SAFE_MENTIONS,
            )
            return
        if isinstance(interaction, hikari.ComponentInteraction):
            if interaction.custom_id.startswith("perms:"):
                try:
                    await self.app.perms.component(interaction)
                except Exception:
                    log.exception("permission_component_failed")
                    try:
                        await interaction.execute("Unable to update permissions. Run `/perms list` to verify the current grants.",
                                                  flags=hikari.MessageFlag.EPHEMERAL, **SAFE_MENTIONS)
                    except hikari.HikariError:
                        log.exception("permission_component_error_response_failed")
            else:
                await self.app.admin.component(interaction)
            return
        context = Context(self.app, interaction)
        if interaction.guild_id is None:
            await context.reply("Anchor commands can only be used in a server.", ephemeral=True)
            return
        token = guild_context.set(str(interaction.guild_id))
        try:
            handler = self.handlers.get(context.command_name) or self.handlers.get(interaction.command_name)
            if handler:
                log.debug(
                    "command name=%s guild=%s user=%s",
                    interaction.command_name,
                    interaction.guild_id,
                    interaction.user.id,
                )
                await handler(context)
            else:
                await context.reply("Unknown command.", ephemeral=True)
        except ValueError as error:
            await context.reply(str(error), ephemeral=True)
        except hikari.ForbiddenError as error:
            if error.code in {50001, 50013}:
                await self.app.pins.remove_channel(interaction.channel_id)
                message = "Anchor lost channel access or permissions. Saved data for this channel has been removed."
            else:
                message = "Discord denied this request. Check Anchor's permissions and try again."
            log.warning(
                "command_forbidden name=%s channel=%s code=%s",
                interaction.command_name, interaction.channel_id, error.code,
            )
            try:
                await context.reply(message, ephemeral=True)
            except hikari.HikariError:
                log.exception("error_response_failed")
        except Exception:
            log.exception("command_failed name=%s guild=%s", interaction.command_name, interaction.guild_id)
            try:
                await context.reply(
                    "Something went wrong. Check Anchor's permissions and try again.", ephemeral=True
                )
            except hikari.HikariError:
                log.exception("error_response_failed")
        finally:
            guild_context.reset(token)
