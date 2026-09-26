import logging
import time

log = logging.getLogger(__name__)


async def post_stats(config, db, session):
    workers = db.workers()
    ready = {
        shard["id"]
        for worker in workers
        if time.time() - worker["heartbeat"] <= config["worker_timeout_seconds"]
        for shard in worker.get("shards", [])
        if shard["status"] == "CONNECTED"
    }
    if ready != set(range(config["shard_count"])):
        log.debug("topgg_stats_waiting_for_shards")
        return False
    token = config["topgg_token"].strip()
    if not token.lower().startswith("bearer "):
        token = "Bearer " + token
    payload = {
        "server_count": sum(worker.get("guilds", 0) for worker in workers),
        "shard_count": config["shard_count"],
    }
    async with session.patch(
        "https://top.gg/api/v1/projects/@me/metrics",
        headers={"Authorization": token},
        json=payload,
    ) as response:
        response.raise_for_status()
    log.info("topgg_stats_posted servers=%s shards=%s", payload["server_count"], payload["shard_count"])
    return True
