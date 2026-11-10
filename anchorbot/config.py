import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


def load(path: str | Path, *, credentials: bool = True) -> dict:
    path = Path(path).resolve()
    if os.name != "nt":
        path.chmod(0o600)
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    for key in ("tos_url", "support_url"):
        url = config.setdefault(key, "")
        if not isinstance(url, str):
            raise ValueError(f"{key} must be an HTTPS URL or an empty string")
        if url and (urlparse(url).scheme != "https" or not urlparse(url).hostname or len(url) > 512):
            raise ValueError(f"{key} must be an HTTPS URL of at most 512 characters")
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
    # A unique, owner-only temporary file avoids exposing secrets or following
    # a pre-created config.json.tmp symlink during an atomic rewrite.
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".anchorbot-config-")
    tmp = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(raw, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
