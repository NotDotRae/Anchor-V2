import logging

import hikari

from ..discord_limits import guild_context
from ..style import SAFE_MENTIONS

log = logging.getLogger(__name__)


class Context:
    def __init__(self, app, interaction):
        self.app, self.i = app, interaction
        self.options = {o.name: o.value for o in (interaction.options or [])}
        self.responded = False

    def value(self, name, default=""):
        from ..style import clean

        value = self.options.get(name, default)
        return clean(value) if isinstance(value, str) else value

    async def defer(self, ephemeral=False):
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
        permissions = self.i.member.permissions if self.i.member else hikari.Permissions.NONE
        if permissions & (hikari.Permissions.MANAGE_MESSAGES | hikari.Permissions.ADMINISTRATOR):
            return True
        await self.reply("You need the `Manage Messages` permission to use this command.", ephemeral=True)
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
        builder = hikari.impl.SlashCommandBuilder(
            name, description, context_types=[hikari.ApplicationContextType.GUILD]
        )
        if option:
            key, desc, kind, required = option
            builder.add_option(
                hikari.CommandOption(
                    type=kind,
                    name=key,
                    description=desc,
                    is_required=required,
                    max_length=maximum if kind == hikari.OptionType.STRING else None,
                )
            )
        if subcommand:
            builder.add_option(
                hikari.CommandOption(
                    type=hikari.OptionType.SUB_COMMAND, name=subcommand, description=description
                )
            )
        self.builders.append(builder)
        self.handlers[name] = handler

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
            await self.app.admin.component(interaction)
            return
        context = Context(self.app, interaction)
        if interaction.guild_id is None:
            await context.reply("AnchorBot commands can only be used in a server.", ephemeral=True)
            return
        token = guild_context.set(str(interaction.guild_id))
        try:
            handler = self.handlers.get(interaction.command_name)
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
        except Exception:
            log.exception("command_failed name=%s guild=%s", interaction.command_name, interaction.guild_id)
            try:
                await context.reply(
                    "Something went wrong. Check AnchorBot's permissions and try again.", ephemeral=True
                )
            except hikari.HikariError:
                log.exception("error_response_failed")
        finally:
            guild_context.reset(token)
