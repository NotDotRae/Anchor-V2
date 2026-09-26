import logging

import hikari

from .commands import builders
from .rest_policy import install

log = logging.getLogger(__name__)


async def reconcile(rest, application_id, commands):
    application = await rest.fetch_application()
    if int(application.id) != application_id:
        raise ValueError("application_id does not match the application belonging to bot_token")
    if not commands:
        raise ValueError("The bot command registry is empty")

    await rest.set_application_commands(application_id, commands)
    guild_count = removed = 0
    async for guild in rest.fetch_my_guilds():
        existing = await rest.fetch_application_commands(application_id, guild=guild.id)
        if existing:
            await rest.set_application_commands(application_id, [], guild=guild.id)
            removed += len(existing)
            log.info("guild_commands_cleared guild=%s count=%s", guild.id, len(existing))
        guild_count += 1
    log.info(
        "startup_commands_synchronized global=%s guilds_checked=%s guild_commands_removed=%s",
        len(commands),
        guild_count,
        removed,
    )


async def synchronize(config):
    install()
    app = hikari.RESTApp()
    await app.start()
    try:
        async with app.acquire(config["bot_token"], hikari.TokenType.BOT) as rest:
            await reconcile(rest, int(config["application_id"]), builders())
    finally:
        await app.close()
