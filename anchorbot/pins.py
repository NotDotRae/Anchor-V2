import asyncio
import logging
import secrets
import time
from collections import defaultdict
from contextlib import AsyncExitStack, asynccontextmanager

import hikari

from .discord_limits import guild_context
from .expiry import MAX_EXPIRY_SECONDS
from .style import SAFE_MENTIONS, SILENT, clean, pin_embed, webhook_parts

log = logging.getLogger(__name__)
MIN_INTERVAL = 5
MAX_INTERVAL = 120

THRESHOLDS = {"classic": (5, 15), "slow": (13, 35), "embed": (5, 15), "webhook": (5, 15)}


class Pins:
    def __init__(self, bot, db, shards, count, metrics):
        self.bot, self.db, self.metrics = bot, db, metrics
        self.states = {s["id"]: s for s in db.channels(set(shards), count)}
        self.locks = defaultdict(asyncio.Lock)
        self.positions = defaultdict(int)
        self.tasks = {}
        self.wakeups = defaultdict(asyncio.Event)
        self.recovered = set()
        self.retry_after = {}
        self.closing = False
        self.departed_guilds = set()

    def state(self, channel, guild):
        if str(guild) in self.departed_guilds:
            raise ValueError("Anchor is no longer in this server.")
        state = self.states.setdefault(str(channel), {"id": str(channel), "guild_id": str(guild), "pins": {}})
        if state["guild_id"] != str(guild):
            raise ValueError("Channel does not belong to this server.")
        return state

    async def verified_webhook(self, state):
        # Webhooks can be moved after configuration. Never trust a saved URL's scope.
        hook, token = webhook_parts(state["webhook_url"])
        channel = await self.bot.rest.fetch_channel(int(state["id"]))
        webhook = await self.bot.rest.fetch_webhook(hook, token=token)
        if (str(getattr(channel, "guild_id", None)) != state["guild_id"]
                or str(webhook.channel_id) != state["id"]
                or str(webhook.guild_id) != state["guild_id"]):
            raise ValueError("The saved webhook no longer belongs to this channel. Set it again.")
        return hook, token

    def discard_channel(self, channel):
        """Called while holding the channel lock to prevent stale writes after deletion."""
        self.db.remove_channel(channel)
        self.forget_channel(channel)

    def forget_channel(self, channel):
        self.states.pop(channel, None)
        self.recovered.discard(channel)
        self.retry_after.pop(channel, None)
        wakeup = self.wakeups.get(channel)
        if wakeup:
            wakeup.set()
        if channel not in self.tasks:
            self.wakeups.pop(channel, None)
        for kind in THRESHOLDS:
            self.positions.pop((channel, kind), None)

    @asynccontextmanager
    async def channel_lock(self, channel):
        channel = str(channel)
        async with self.locks[channel]:
            try:
                yield
            except hikari.ForbiddenError as error:
                if error.code in {50001, 50013}:  # Missing Access / Missing Permissions
                    self.discard_channel(channel)
                    log.warning("channel_permissions_lost channel=%s cleanup_queued=true", channel)
                raise

    async def remove_channel(self, channel):
        channel = str(channel)
        async with self.locks[channel]:
            self.discard_channel(channel)

    async def remove_guild(self, guild):
        guild = str(guild)
        self.departed_guilds.add(guild)
        channels = [s["id"] for s in self.states.values() if s["guild_id"] == guild]
        async with AsyncExitStack() as stack:
            for channel in sorted(channels):
                await stack.enter_async_context(self.locks[channel])
            self.db.remove_guild(guild)
            for channel in channels:
                self.forget_channel(channel)
        log.info("guild_data_removed guild=%s channels=%s cleanup_queued=true", guild, len(channels))

    async def reconcile(self, state):
        channel = state["id"]
        if channel in self.recovered:
            return
        if not state["pins"]:
            self.recovered.add(channel)
            return
        history = await self.bot.rest.fetch_messages(int(channel)).limit(50)
        own_id = int(self.bot.get_me().id)
        for kind, pin in state["pins"].items():
            hook_id = webhook_parts(state["webhook_url"])[0] if kind == "webhook" else None
            matches = []
            for message in history:
                owned = (
                    message.webhook_id == hook_id
                    if hook_id
                    else (int(message.author.id) == own_id and message.webhook_id is None)
                )
                if not owned:
                    continue
                if (
                    hasattr(message, "flags")
                    and not (message.flags & SILENT)
                    and str(message.id) != pin.get("message_id")
                ):
                    continue
                if kind in {"classic", "slow"}:
                    same = message.content == clean(pin["text"])
                else:
                    same = str(message.id) == pin.get("message_id") or bool(
                        message.embeds and message.embeds[0].description == pin["text"]
                    )
                if same:
                    matches.append(message)
            if matches:
                latest = matches[0]
                pin["message_id"] = str(latest.id)
                pin["posted_at"] = latest.timestamp.timestamp()
                pin.pop("nonce", None)
                pin["retired_ids"] = list(set(pin.get("retired_ids", []) + [str(m.id) for m in matches[1:]]))
                self.positions[(channel, kind)] = sum(m.id > latest.id for m in history)
            self.db.save_channel(state)
            await self.cleanup(state, kind, pin)
        if len(state["pins"]) > 1 and not state.get("preserve_legacy_pins"):
            keep = max(state["pins"], key=lambda kind: state["pins"][kind].get("posted_at", 0))
            for kind in list(state["pins"]):
                if kind != keep:
                    if state["pins"][kind].get("message_id") == state["pins"][keep].get("message_id"):
                        state["pins"][kind]["message_id"] = None
                    await self.retire(state, kind)
        self.recovered.add(channel)

    async def cleanup(self, state, kind, pin):
        for message in list(pin.get("retired_ids", [])):
            try:
                if kind == "webhook":
                    hook, token = await self.verified_webhook(state)
                    await self.bot.rest.delete_webhook_message(hook, token, int(message))
                else:
                    await self.bot.rest.delete_message(int(state["id"]), int(message))
            except hikari.NotFoundError:
                pass
            except hikari.ForbiddenError:
                log.warning(
                    "retired_delete_forbidden channel=%s kind=%s message=%s", state["id"], kind, message
                )
                raise
            pin["retired_ids"].remove(message)
            self.db.save_channel(state)

    def expiry_due(self, state):
        expiry = state.get("expiry")
        return bool(expiry and (time.time() >= expiry["expires_at"]
                    or self.db.expiry_ready(state["id"], expiry["token"])))

    def clear_expiry(self, state):
        state.pop("expiry", None)
        self.db.forget_expiry(state["id"])
        self.db.save_channel(state)
        self.db.put("cloud_sync_requested", True)

    async def set_expiry(self, channel, guild, seconds, *, authorize=None):
        if seconds is not None and (type(seconds) is not int or not 1 <= seconds <= MAX_EXPIRY_SECONDS):
            raise ValueError("Choose an expiry from 1 second to 365 days, or `off`.")
        async with self.channel_lock(channel):
            if authorize:
                authorize()
            state = self.states.get(str(channel))
            if not state or state["guild_id"] != str(guild) or not state["pins"]:
                raise ValueError("There is no pin in this channel. Create one with `/anchor start`.")
            if seconds is None:
                self.clear_expiry(state)
                expiry = None
            else:
                expiry = {"token": secrets.token_hex(16), "expires_at": time.time() + seconds}
                state["expiry"] = expiry
                self.db.forget_expiry(state["id"])
                self.db.save_channel(state)
                self.db.put("cloud_sync_requested", True)
        # Re-evaluate a waiting repost when its deadline changes or is canceled.
        self.schedule(channel)
        return expiry

    def poll_expiries(self):
        for row in self.db.db.execute("SELECT channel,token FROM expiry_events"):
            state = self.states.get(row["channel"])
            if (state and state.get("expiry", {}).get("token") == row["token"]
                    and time.time() >= self.retry_after.get(row["channel"], 0)):
                self.schedule(row["channel"])

    async def expire_current(self, state):
        expiry = state.get("expiry")
        if not expiry:
            return False
        if self.db.expiry_ready(state["id"], expiry["token"]):
            # This also recovers a message posted just before a crash, then deletes it.
            await self.reconcile(state)
            for kind in list(state["pins"]):
                await self.retire(state, kind)
            if state.get("expiry"):
                self.clear_expiry(state)
            log.info("pin_expired channel=%s token=%s", state["id"], expiry["token"])
            return True
        # Never repost past a known deadline while waiting for the Convex job delivery.
        return time.time() >= expiry["expires_at"]

    async def repost(self, state, kind):
        if self.expiry_due(state):
            return
        if kind == "webhook":
            await self.verified_webhook(state)
        pin = state["pins"][kind]
        if pin.get("message_id"):
            pin.setdefault("retired_ids", []).append(pin["message_id"])
            pin["message_id"] = None
            self.db.save_channel(state)
        await self.cleanup(state, kind, pin)
        if kind != "webhook":
            pin.setdefault("nonce", secrets.token_hex(12))
            self.db.save_channel(state)
        if self.expiry_due(state):
            return
        if kind == "webhook":
            hook, token = await self.verified_webhook(state)
            message = await self.bot.rest.execute_webhook(
                hook, token, embed=pin_embed(state, pin["text"]), flags=SILENT, **SAFE_MENTIONS
            )
        elif kind in {"classic", "slow"}:
            message = await self.bot.rest.create_message(
                int(state["id"]), clean(pin["text"]), flags=SILENT, nonce=pin["nonce"], **SAFE_MENTIONS
            )
        else:
            message = await self.bot.rest.create_message(
                int(state["id"]),
                embed=pin_embed(state, pin["text"]),
                flags=SILENT,
                nonce=pin["nonce"],
                **SAFE_MENTIONS,
            )
        pin["message_id"], pin["posted_at"] = str(message.id), message.timestamp.timestamp()
        pin.pop("nonce", None)
        self.positions[(state["id"], kind)] = 0
        self.db.save_channel(state)
        await self.cleanup(state, kind, pin)
        log.debug("pin_reposted channel=%s kind=%s message=%s", state["id"], kind, message.id)

    async def start(self, channel, guild, kind, text, *, authorize=None):
        async with self.channel_lock(channel):
            if authorize:
                authorize()
            state = self.state(channel, guild)
            await self.reconcile(state)
            for old_kind in list(state["pins"]):
                await self.retire(state, old_kind)
            state.pop("preserve_legacy_pins", None)
            state.pop("paused", None)
            state.pop("expiry", None)
            state["pins"][kind] = {"text": text, "message_id": None, "posted_at": 0, "retired_ids": []}
            self.db.save_channel(state)
            await self.repost(state, kind)

    def schedule(self, channel):
        channel = str(channel)
        self.wakeups[channel].set()
        if not self.closing and channel not in self.tasks:
            self.tasks[channel] = asyncio.create_task(self.process(channel))

    async def configure(self, channel, guild, *, paused=None, interval=None, authorize=None):
        if interval is not None and (type(interval) is not int or not MIN_INTERVAL <= interval <= MAX_INTERVAL):
            raise ValueError(f"Choose a frequency from {MIN_INTERVAL} to {MAX_INTERVAL} seconds.")
        async with self.channel_lock(channel):
            if authorize:
                authorize()
            state = self.state(channel, guild)
            if paused is not None:
                if not state["pins"]:
                    raise ValueError("There is no pin in this channel. Create one with `/anchor start`.")
                state["paused"] = paused
            if interval is not None:
                state["interval_seconds"] = interval
            self.db.save_channel(state)
        self.schedule(channel)

    async def retire(self, state, kind):
        pin = state["pins"][kind]
        if pin.get("message_id"):
            pin.setdefault("retired_ids", []).append(pin["message_id"])
            pin["message_id"] = None
        pin["stopping"] = True
        self.db.save_channel(state)
        await self.cleanup(state, kind, pin)
        del state["pins"][kind]
        self.positions.pop((state["id"], kind), None)
        self.db.save_channel(state)
        if not state["pins"] and state.get("expiry"):
            self.clear_expiry(state)

    async def stop(self, channel, kinds=None, *, authorize=None):
        channel = str(channel)
        async with self.channel_lock(channel):
            if authorize:
                authorize()
            state = self.states.get(channel)
            if not state:
                return
            await self.reconcile(state)
            for kind in list(state["pins"]):
                if kinds is not None and kind not in kinds:
                    continue
                await self.retire(state, kind)
        self.wakeups[channel].set()

    async def activity(self, event):
        start = time.process_time()
        channel = str(event.channel_id)
        state = self.states.get(channel)
        if state:
            for kind, pin in state["pins"].items():
                if str(event.message_id) != pin.get("message_id"):
                    self.positions[(channel, kind)] += 1
            if not event.is_bot and not self.closing and time.time() >= self.retry_after.get(channel, 0):
                self.schedule(channel)
        self.metrics.event(event.guild_id, time.process_time() - start)
        log.debug("message_event guild=%s channel=%s message=%s", event.guild_id, channel, event.message_id)

    async def process(self, channel):
        try:
            while not self.closing:
                self.wakeups[channel].clear()
                delay = None
                async with self.channel_lock(channel):
                    state = self.states.get(channel)
                    if not state:
                        return
                    token = guild_context.set(state["guild_id"])
                    try:
                        if await self.expire_current(state):
                            return
                        if state.get("paused") and not any(p.get("stopping") for p in state["pins"].values()):
                            return
                        await self.reconcile(state)
                        for kind, pin in list(state["pins"].items()):
                            if pin.get("stopping"):
                                await self.retire(state, kind)
                                continue
                            if state.get("paused"):
                                continue
                            messages, age = THRESHOLDS[kind]
                            position = self.positions[(channel, kind)]
                            elapsed = time.time() - pin["posted_at"]
                            custom = state.get("interval_seconds")
                            # Custom timing cannot be bypassed by a busy channel.
                            interval = custom if custom is not None else (
                                MIN_INTERVAL if position >= messages else age)
                            if not pin.get("message_id") or position > 0:
                                remaining = max(MIN_INTERVAL, interval) - elapsed
                                if remaining <= 0:
                                    await self.repost(state, kind)
                                else:
                                    delay = remaining if delay is None else min(delay, remaining)
                    finally:
                        guild_context.reset(token)
                if delay is None:
                    return
                # Wait outside the channel lock so pause, stop, and settings stay responsive.
                try:
                    await asyncio.wait_for(self.wakeups[channel].wait(), timeout=delay)
                except TimeoutError:
                    pass
        except hikari.ForbiddenError as error:
            if error.code not in {50001, 50013}:
                self.retry_after[channel] = time.time() + 60
            log.exception("pin_activity_forbidden channel=%s", channel)
        except Exception:
            self.retry_after[channel] = time.time() + 60
            log.exception("pin_activity_failed channel=%s retry_seconds=60", channel)
        finally:
            self.tasks.pop(channel, None)
            if channel not in self.states:
                self.wakeups.pop(channel, None)

    async def channel_deleted(self, event):
        await self.remove_channel(event.channel_id)

    async def close(self):
        self.closing = True
        for wakeup in self.wakeups.values():
            wakeup.set()
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
