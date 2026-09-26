import asyncio
import logging
import time

import hikari

log = logging.getLogger(__name__)
INTERVAL = 300


class Presence:
    def __init__(self, app):
        self.app = app
        self.started = app.db.get("deployment", {}).get("started", time.time())

    def activity(self):
        if int(max(0, time.time() - self.started) // INTERVAL) % 2:
            text = f"Tracking {self.app.db.tracked_messages():,} messages"
        else:
            text = self.app.config["kofi_url"]
        return hikari.Activity(name="Custom Status", type=hikari.ActivityType.CUSTOM, state=text)

    async def run(self):
        while not self.app.stop.is_set():
            try:
                activity = self.activity()
                await self.app.bot.update_presence(activity=activity)
                log.debug("presence_updated worker=%s status=%s", self.app.args.worker_id, activity.state)
            except Exception:
                log.exception("presence_update_failed worker=%s", self.app.args.worker_id)
            delay = INTERVAL - (time.time() - self.started) % INTERVAL
            try:
                await asyncio.wait_for(self.app.stop.wait(), delay)
            except TimeoutError:
                pass
