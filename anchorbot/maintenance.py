import asyncio
import logging
import sqlite3
import time

import hikari

from .discord_limits import install
from .lifecycle import run_gateway
from .metrics import Metrics
from .storage import LocalDB
from .style import SAFE_MENTIONS

log = logging.getLogger(__name__)


class MaintenanceWorker:
    def __init__(self, config, args):
        self.config, self.args, self.count = config, args, args.shard_count
        self.db = LocalDB(config["database_path"])
        self.metrics = Metrics()
        self.stop = asyncio.Event()
        install(self.db, self.metrics)
        self.bot = hikari.GatewayBot(
            config["bot_token"],
            intents=hikari.Intents.GUILDS,
            auto_chunk_members=False,
            banner=None,
            logs=None,
            suppress_optimization_warning=True,
            cache_settings=hikari.impl.CacheSettings(components=hikari.api.CacheComponents.NONE),
        )
        self.bot.subscribe(hikari.InteractionCreateEvent, self.interaction)

    async def interaction(self, event):
        interaction = event.interaction
        if not isinstance(interaction, (hikari.CommandInteraction, hikari.ComponentInteraction)):
            return
        if self.db.interaction_mode(interaction.id, maintenance=True) != "restarting":
            return
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            "The bot is currently restarting",
            flags=hikari.MessageFlag.EPHEMERAL,
            **SAFE_MENTIONS,
        )
        log.debug("restart_notice_sent interaction=%s", interaction.id)

    async def heartbeat(self):
        while not self.stop.is_set():
            try:
                if self.db.get("stop_maintenance", False):
                    self.stop.set()
                    return
                value, _ = self.metrics.snapshot([])
                value.update(
                    heartbeat=time.time(),
                    ready=len(self.bot.shards) == self.count
                    and all(s.is_connected for s in self.bot.shards.values()),
                    shard_count=self.count,
                )
                self.db.put("maintenance_worker", value)
            except sqlite3.OperationalError:
                log.exception("maintenance_heartbeat_database_retry")
            try:
                await asyncio.wait_for(self.stop.wait(), self.config["heartbeat_interval_seconds"])
            except TimeoutError:
                pass

    async def run(self):
        try:
            await run_gateway(self, self.heartbeat)
        finally:
            self.db.close()
