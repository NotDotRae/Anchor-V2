import json
import logging
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger(__name__)


class UnsupportedCloudState(RuntimeError):
    pass


class Cloud:
    def __init__(self, config, db, session):
        self.config, self.db, self.session = config, db, session

    async def request(self, path, body=None):
        url = self.config["convex_site_url"].rstrip("/") + "/" + path
        async with self.session.request(
            "POST" if body is not None else "GET",
            url,
            json=body,
            headers={"Authorization": "Bearer " + self.config["convex_key"]},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Convex {path.split('?')[0]} returned HTTP {response.status}")
            return await response.json()

    async def flush(self):
        for _ in range(100):
            rows = self.db.pending()
            if not rows:
                return
            await self.request(
                "batch", {"changes": [{"kind": "channel", "id": r["id"], "value": r["value"]} for r in rows]}
            )
            self.db.acknowledge(rows)
            log.debug("convex_batch_saved records=%s", len(rows))

    async def read_snapshot(self):
        rows, cursor = [], None
        while True:
            result = await self.request("state" + ("?" + urlencode({"cursor": cursor}) if cursor else ""))
            if not isinstance(result, dict) or "page" not in result:
                raise UnsupportedCloudState(
                    "Deploy the current Convex backend and run migrate.convex.bat before starting the bot."
                )
            rows.extend(result["page"])
            if result["isDone"]:
                break
            cursor = result["continueCursor"]
        if any(r["kind"] != "channel" for r in rows):
            raise UnsupportedCloudState("Unsupported cloud records. Stop the bot and run migrate.convex.bat.")
        states = {}
        for row in rows:
            state = json.loads(row["value"])
            if "migration_deferred" in state:
                log.warning("cloud_channel_deferred channel=%s rerun_migration_after_access_is_restored", row["id"])
                continue
            states[row["id"]] = state
        return states

    async def bootstrap(self):
        states = await self.read_snapshot()
        if self.db.pending():
            await self.flush()
            states = await self.read_snapshot()
        self.db.install_snapshot(states.values())
        self.db.put("snapshot_loaded", True)
        log.info("cloud_snapshot_loaded channels=%s", len(states))
