import asyncio
import logging
import math
import sqlite3
import time

import psutil

from .lifecycle import run_gateway
from .metrics import Metrics
from .storage import LocalDB

log = logging.getLogger(__name__)


async def simulate(config, args):
    db, metrics = LocalDB(config["database_path"]), Metrics()
    while psutil.pid_exists(args.parent_pid) and not db.get(f"stop_worker_{args.worker_id}", False):
        value, loads = metrics.snapshot([])
        value.update(
            guilds=0,
            pins=0,
            shards=[
                {"id": s, "started": metrics.started, "guilds": 0, "status": "SIMULATED", "latency_ms": 0}
                for s in args.shards
            ],
        )
        db.heartbeat(args.worker_id, value, loads)
        await asyncio.sleep(config["heartbeat_interval_seconds"])
    db.close()


class Worker:
    def __init__(self, config, args):
        import hikari

        from .commands import configure
        from .discord_limits import install
        from .events import GuildEvents
        from .pins import Pins

        self.config, self.args, self.count = config, args, args.shard_count
        self.started = time.time()
        self.db, self.metrics = LocalDB(config["database_path"]), Metrics()
        install(self.db, self.metrics)
        self.bot = hikari.GatewayBot(
            config["bot_token"],
            intents=hikari.Intents.GUILDS | hikari.Intents.GUILD_MESSAGES,
            auto_chunk_members=False,
            banner=None,
            logs=None,
            suppress_optimization_warning=True,
            cache_settings=hikari.impl.CacheSettings(
                components=hikari.api.CacheComponents.GUILDS | hikari.api.CacheComponents.ME
            ),
        )
        self.pins = Pins(self.bot, self.db, args.shards, self.count, self.metrics)
        configure(self)
        self.events = GuildEvents(self)
        self.shard_started = {}
        self.disconnected = {}
        self.bot.subscribe(hikari.InteractionCreateEvent, self.router.dispatch)
        self.bot.subscribe(hikari.GuildMessageCreateEvent, self.pins.activity)
        self.bot.subscribe(hikari.GuildChannelDeleteEvent, self.pins.channel_deleted)
        self.bot.subscribe(hikari.ShardReadyEvent, self.ready)
        self.bot.subscribe(hikari.ShardResumedEvent, self.resumed)
        self.bot.subscribe(hikari.ShardDisconnectedEvent, self.disconnect)
        self.stop = asyncio.Event()

    @property
    def avatar(self):
        me = self.bot.get_me()
        return me.display_avatar_url if me else None

    async def ready(self, event):
        self.shard_started[event.shard.id] = time.time()
        self.disconnected.pop(event.shard.id, None)
        log.info("shard_ready shard=%s", event.shard.id)

    async def resumed(self, event):
        self.disconnected.pop(event.shard.id, None)
        log.info("shard_resumed shard=%s", event.shard.id)

    async def disconnect(self, event):
        if self.stop.is_set():
            log.info("shard_disconnected shard=%s reason=shutdown", event.shard.id)
            return
        self.disconnected.setdefault(event.shard.id, time.monotonic())
        log.warning("shard_disconnected shard=%s", event.shard.id)

    async def heartbeat(self):
        while not self.stop.is_set():
            try:
                await self.health_snapshot()
            except sqlite3.OperationalError:
                log.exception("worker_heartbeat_database_retry worker=%s", self.args.worker_id)
            if self.stop.is_set():
                return
            try:
                await asyncio.wait_for(self.stop.wait(), self.config["heartbeat_interval_seconds"])
            except TimeoutError:
                pass

    async def health_snapshot(self):
        if not psutil.pid_exists(self.args.parent_pid) or self.db.get(
            f"stop_worker_{self.args.worker_id}", False
        ):
            self.stop.set()
            return
        for sid, shard in self.bot.shards.items():
            if shard.is_connected:
                self.disconnected.pop(sid, None)
        if any(
            time.monotonic() - when > self.config["worker_timeout_seconds"]
            for when in self.disconnected.values()
        ):
            log.error("shard_reconnect_timeout")
            self.stop.set()
            return
        guilds = self.bot.cache.get_guilds_view()
        value, loads = self.metrics.snapshot(guilds)
        shards = []
        for sid in self.args.shards:
            shard = self.bot.shards.get(sid)
            latency = shard.heartbeat_latency if shard else math.nan
            shards.append(
                {
                    "id": sid,
                    "started": self.shard_started.get(sid, self.started),
                    "guilds": sum((int(g) >> 22) % self.count == sid for g in guilds),
                    "status": "CONNECTED" if shard and shard.is_connected else "CONNECTING",
                    "latency_ms": round(latency * 1000, 2) if math.isfinite(latency) else None,
                }
            )
        value.update(
            guilds=len(guilds), pins=sum(len(s["pins"]) for s in self.pins.states.values()), shards=shards
        )
        self.db.heartbeat(self.args.worker_id, value, loads)
        log.debug("worker_heartbeat worker=%s guilds=%s", self.args.worker_id, len(guilds))

    async def run(self):
        try:
            await run_gateway(self, self.heartbeat, self.pins.close)
        finally:
            self.db.close()
