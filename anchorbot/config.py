import json
import os
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


def load(path: str | Path, *, credentials: bool = True) -> dict:
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    tos = config.setdefault("tos_url", "")
    if not isinstance(tos, str):
        raise ValueError("tos_url must be an HTTPS URL or an empty string")
    if tos and (urlparse(tos).scheme != "https" or not urlparse(tos).hostname or len(tos) > 512):
        raise ValueError("tos_url must be an HTTPS URL of at most 512 characters")
    for key in (
        "shard_count",
        "worker_count",
        "heartbeat_interval_seconds",
        "worker_timeout_seconds",
        "shutdown_timeout_seconds",
        "convex_sync_interval_seconds",
        "rebalance_interval_seconds",
        "metrics_retention_days",
    ):
        if type(config.get(key)) is not int or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if config["worker_timeout_seconds"] < config["heartbeat_interval_seconds"] * 3:
        raise ValueError("worker_timeout_seconds must be at least three heartbeat intervals")
    if not 0 < config["rebalance_min_improvement"] < 1:
        raise ValueError("rebalance_min_improvement must be between 0 and 1")
    ZoneInfo(config["restart_timezone"])
    hour, minute = map(int, config["restart_time"].split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("restart_time must be HH:MM")
    if credentials:
        donation = urlparse(config.get("kofi_url", ""))
        if (
            donation.scheme != "https"
            or donation.hostname != "ko-fi.com"
            or donation.path in {"", "/", "/YOUR_PAGE"}
        ):
            raise ValueError("Set kofi_url to your https://ko-fi.com/ page")
        for key in ("bot_token", "convex_key", "convex_site_url"):
            if not config.get(key):
                raise ValueError(f"Set {key} in {path.name}")
        if int(config["owner_id"]) <= 0 or int(config["application_id"]) <= 0:
            raise ValueError("Set owner_id and application_id to Discord snowflake IDs")
        if not config["convex_site_url"].startswith("https://"):
            raise ValueError("convex_site_url must be the HTTPS Convex HTTP actions URL")
    config["_path"] = str(path)
    for key in ("database_path",):
        config[key] = str((path.parent / config[key]).resolve())
    return config


def set_shard_count(path: str | Path, count: int) -> None:
    if type(count) is not int or count < 1:
        raise ValueError("At least one shard is required")
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    raw["shard_count"] = count
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
