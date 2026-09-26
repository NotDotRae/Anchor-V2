import asyncio
import logging
import secrets
import time
from collections import defaultdict

import hikari

from .discord_limits import guild_context
from .style import SAFE_MENTIONS, SILENT, clean, pin_embed, webhook_parts

log = logging.getLogger(__name__)
THRESHOLDS = {"classic": (5, 15), "slow": (13, 35), "embed": (5, 15), "webhook": (5, 15)}


class Pins:
    def __init__(self, bot, db, shards, count, metrics):
        self.bot, self.db, self.metrics = bot, db, metrics
        self.states = {s["id"]: s for s in db.channels(set(shards), count)}
        self.locks = defaultdict(asyncio.Lock)
        self.positions = defaultdict(int)
        self.tasks = {}
        self.recovered = set()
        self.retry_after = {}
        self.closing = False

    def state(self, channel, guild):
        return self.states.setdefault(str(channel), {"id": str(channel), "guild_id": str(guild), "pins": {}})

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
                    hook, token = webhook_parts(state["webhook_url"])
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

    async def repost(self, state, kind):
        pin = state["pins"][kind]
        if pin.get("message_id"):
            pin.setdefault("retired_ids", []).append(pin["message_id"])
            pin["message_id"] = None
            self.db.save_channel(state)
        await self.cleanup(state, kind, pin)
        if kind != "webhook":
            pin.setdefault("nonce", secrets.token_hex(12))
            self.db.save_channel(state)
        if kind == "webhook":
            hook, token = webhook_parts(state["webhook_url"])
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

    async def start(self, channel, guild, kind, text):
        async with self.locks[str(channel)]:
            state = self.state(channel, guild)
            await self.reconcile(state)
            for old_kind in list(state["pins"]):
                await self.retire(state, old_kind)
            state.pop("preserve_legacy_pins", None)
            state["pins"][kind] = {"text": text, "message_id": None, "posted_at": 0, "retired_ids": []}
            self.db.save_channel(state)
            await self.repost(state, kind)

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

    async def stop(self, channel, kinds=None):
        channel = str(channel)
        async with self.locks[channel]:
            state = self.states.get(channel)
            if not state:
                return
            await self.reconcile(state)
            for kind in list(state["pins"]):
                if kinds is not None and kind not in kinds:
                    continue
                pin = state["pins"][kind]
                if pin.get("message_id"):
                    pin.setdefault("retired_ids", []).append(pin["message_id"])
                    pin["message_id"] = None
                pin["stopping"] = True
                self.db.save_channel(state)
                await self.cleanup(state, kind, pin)
                del state["pins"][kind]
                self.positions.pop((channel, kind), None)
            self.db.save_channel(state)

    async def activity(self, event):
        start = time.process_time()
        channel = str(event.channel_id)
        state = self.states.get(channel)
        if state:
            for kind, pin in state["pins"].items():
                if str(event.message_id) != pin.get("message_id"):
                    self.positions[(channel, kind)] += 1
            if not event.is_bot and not self.closing and time.time() >= self.retry_after.get(channel, 0):
                if channel not in self.tasks:
                    self.tasks[channel] = asyncio.create_task(self.process(channel))
        self.metrics.event(event.guild_id, time.process_time() - start)
        log.debug("message_event guild=%s channel=%s message=%s", event.guild_id, channel, event.message_id)

    async def process(self, channel):
        try:
            async with self.locks[channel]:
                state = self.states.get(channel)
                if not state:
                    return
                token = guild_context.set(state["guild_id"])
                try:
                    await self.reconcile(state)
                    for kind, pin in list(state["pins"].items()):
                        if pin.get("stopping"):
                            await self.cleanup(state, kind, pin)
                            del state["pins"][kind]
                            self.db.save_channel(state)
                            continue
                        messages, age = THRESHOLDS[kind]
                        if (
                            not pin.get("message_id")
                            or self.positions[(channel, kind)] >= messages
                            or time.time() - pin["posted_at"] >= age
                        ):
                            await self.repost(state, kind)
                finally:
                    guild_context.reset(token)
        except Exception:
            self.retry_after[channel] = time.time() + 60
            log.exception("pin_activity_failed channel=%s retry_seconds=60", channel)
        finally:
            self.tasks.pop(channel, None)

    async def channel_deleted(self, event):
        channel = str(event.channel_id)
        async with self.locks[channel]:
            self.states.pop(channel, None)
            self.recovered.discard(channel)
            self.db.remove_channel(channel)
            for kind in THRESHOLDS:
                self.positions.pop((channel, kind), None)
        self.locks.pop(channel, None)
        self.retry_after.pop(channel, None)

    async def close(self):
        self.closing = True
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
