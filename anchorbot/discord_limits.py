import asyncio
import json
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar

import hikari
from hikari.impl import buckets
from hikari.impl import shard as shard_impl
from hikari.internal import routes

from .rest_policy import install as install_rest_policy

guild_context = ContextVar("guild_context", default=None)


async def identify_gate(db, shard):
    while True:
        now = time.time()
        db.db.execute("BEGIN IMMEDIATE")
        try:
            budget = db.get("identify_budget")
            if not budget:
                raise RuntimeError("Supervisor has not supplied a Discord session budget")
            if now >= budget["reset_at"]:
                delay = 5
            else:
                key = f"identify_bucket_{shard % budget['concurrency']}"
                deadline = db.get(key, 0)
                delay = max(deadline - now, budget["reset_at"] - now if budget["remaining"] < 1 else 0)
            if delay <= 0:
                budget["remaining"] -= 1
                db.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(now + 5.2)))
                db.db.execute(
                    "INSERT OR REPLACE INTO meta VALUES (?,?)", ("identify_budget", json.dumps(budget))
                )
                return
        finally:
            db.db.commit()
        await asyncio.sleep(min(delay, 5))


def install(db, metrics):
    if hikari.__version__ != "2.6.0":
        raise RuntimeError("The cross-process gateway adapter requires Hikari 2.6.0")
    install_rest_policy()
    original_send = shard_impl.GatewayShardImpl._send_json
    original_request = hikari.impl.RESTClientImpl._request
    original_bucket = buckets.RESTBucketManager.acquire_bucket

    @asynccontextmanager
    async def acquire_bucket(self, *args, **kwargs):
        async with original_bucket(self, *args, **kwargs):
            while delay := db.reserve("discord_rest_gate", 1 / 40):
                await asyncio.sleep(delay)
            yield

    async def send(self, data, *, priority=False):
        if data.get("op") == 2:
            await identify_gate(db, self.id)
        return await original_send(self, data, priority=priority)

    async def request(self, compiled_route, **kwargs):
        if compiled_route.route == routes.GET_GATEWAY_BOT:
            cached = db.get("gateway_info", {})
            budget = db.get("identify_budget", {})
            now = time.time()
            age = time.monotonic() - cached.get("fetched_monotonic", -float("inf"))
            if 0 <= age < 300 and budget.get("reset_at", 0) > now:
                response = cached["value"]
                response["session_start_limit"] = {
                    "remaining": budget["remaining"],
                    "total": budget["total"],
                    "reset_after": (budget["reset_at"] - now) * 1000,
                    "max_concurrency": budget["concurrency"],
                }
                return response
        metrics.request(guild_context.get())
        return await original_request(self, compiled_route, **kwargs)

    shard_impl.GatewayShardImpl._send_json = send
    hikari.impl.RESTClientImpl._request = request
    buckets.RESTBucketManager.acquire_bucket = acquire_bucket
