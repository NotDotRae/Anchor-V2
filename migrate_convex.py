import argparse
import contextlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

KINDS = {"sticky": "classic", "slowSticky": "slow", "embedSticky": "embed", "webhookMessage": "webhook"}
SETTINGS = {"embedImage": "image", "bigEmbedImage": "big_image", "webhookUrl": "webhook_url"}
REMOVED = {"prefix", "disabled"}
DISCORD = "https://discord.com/api/v10/"
HISTORY_LIMIT = 1000


class MigrationError(RuntimeError):
    pass


class HistoryUnavailable(MigrationError):
    pass


class RequestError(MigrationError):
    def __init__(self, service, status):
        self.status = status
        super().__init__(f"{service} returned HTTP {status}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(NoRedirect())
        self.discord_at = 0
        self.guilds = {}

    def request(self, service, path, body=None, *, method=None, webhook=False):
        base = DISCORD if service == "Discord" else self.config["convex_site_url"].rstrip("/") + "/"
        headers = {"User-Agent": "AnchorBotMigration/1.0", "Content-Type": "application/json"}
        if service == "Convex":
            headers["Authorization"] = "Bearer " + self.config["convex_key"]
        elif not webhook:
            headers["Authorization"] = "Bot " + self.config["bot_token"]
        request = Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method or ("POST" if body is not None else "GET"),
        )
        for attempt in range(8):
            if service == "Discord":
                time.sleep(max(0, self.discord_at - time.monotonic()))
                self.discord_at = time.monotonic() + 0.1
            try:
                with self.opener.open(request, timeout=30) as response:
                    if service == "Discord" and response.headers.get("X-RateLimit-Remaining") == "0":
                        self.discord_at = time.monotonic() + self.delay(
                            response.headers.get("X-RateLimit-Reset-After", 1)
                        )
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504}:
                    raise RequestError(service, error.code) from None
                delay = error.headers.get("Retry-After", min(30, 2**attempt))
                if error.code == 429:
                    try:
                        delay = json.loads(error.read()).get("retry_after", delay)
                    except (ValueError, AttributeError):
                        pass
                print(f"{service} HTTP {error.code}; retry {attempt + 1}/8", flush=True)
                time.sleep(self.delay(delay))
            except (URLError, TimeoutError, OSError):
                time.sleep(min(30, 2**attempt))
        raise MigrationError(f"{service} retries exhausted; rerun the migration")

    @staticmethod
    def delay(value):
        try:
            result = float(value)
            return max(0.1, result) if math.isfinite(result) else 5
        except (TypeError, ValueError):
            return 5

    def records(self):
        rows, cursor, seen = [], None, set()
        while True:
            result = self.request("Convex", "state" + ("?" + urlencode({"cursor": cursor}) if cursor else ""))
            if not isinstance(result, dict) or "page" not in result:
                raise MigrationError(
                    "Deploy the current backend with deploy.convex.bat first, then rerun migration"
                )
            rows.extend(result["page"])
            if result["isDone"]:
                break
            cursor = result["continueCursor"]
            if not cursor or cursor in seen:
                raise MigrationError("Convex pagination did not advance")
            seen.add(cursor)
        keys = [(r["kind"], r["id"]) for r in rows]
        if len(set(keys)) != len(keys):
            raise MigrationError("Duplicate Convex record keys found; records were preserved")
        return rows

    def require_history(self, channel, bot_id):
        guild = snowflake(channel["guild_id"])
        if guild not in self.guilds:
            member = self.request("Discord", f"guilds/{guild}/members/{bot_id}")
            roles = self.request("Discord", f"guilds/{guild}/roles")
            self.guilds[guild] = member, roles
        member, roles = self.guilds[guild]
        member_roles = {guild, *map(str, member["roles"])}
        permissions = 0
        for role in roles:
            if str(role["id"]) in member_roles:
                permissions |= int(role["permissions"])
        if permissions & 8:
            return
        overwrites = channel.get("permission_overwrites", [])
        for overwrite in overwrites:
            if str(overwrite["id"]) == guild:
                permissions = permissions & ~int(overwrite["deny"]) | int(overwrite["allow"])
        allow = deny = 0
        for overwrite in overwrites:
            if overwrite["type"] == 0 and str(overwrite["id"]) in member_roles - {guild}:
                allow |= int(overwrite["allow"])
                deny |= int(overwrite["deny"])
        permissions = permissions & ~deny | allow
        for overwrite in overwrites:
            if overwrite["type"] == 1 and str(overwrite["id"]) == bot_id:
                permissions = permissions & ~int(overwrite["deny"]) | int(overwrite["allow"])
        required = (1 << 10) | (1 << 16)
        if permissions & required != required:
            raise HistoryUnavailable(
                "The bot needs View Channel and Read Message History to recover existing message IDs"
            )


def snowflake(value):
    value = str(value)
    if not value.isascii() or not value.isdecimal() or not 0 < int(value) < 2**64:
        raise MigrationError("Invalid Discord ID in stored records")
    return value


def webhook_path(url):
    match = re.fullmatch(
        r"https://(?:discord(?:app)?\.com|canary\.discord\.com|ptb\.discord\.com)"
        r"/api(?:/v\d+)?/webhooks/(\d{17,20})/([A-Za-z0-9._-]+)",
        url.strip(),
    )
    if not match:
        raise MigrationError("Invalid stored webhook URL")
    return f"webhooks/{match[1]}/{match[2]}", match[1]


def clean(text):
    return re.sub(r" {2,}", " ", re.sub(r"<a?:[A-Za-z0-9_]{2,32}:\d{17,20}>", "", text)).strip()


def message_matches(message, kind, text, bot_id, hook_id):
    if message.get("type", 0) != 0 or message.get("attachments"):
        return False
    if kind == "webhook":
        if str(message.get("webhook_id")) != hook_id:
            return False
    elif message.get("webhook_id") or str(message.get("author", {}).get("id")) != bot_id:
        return False
    if kind in {"classic", "slow"}:
        return not message.get("embeds") and message.get("content") == clean(text)
    embeds = message.get("embeds", [])
    return (
        len(embeds) == 1
        and embeds[0].get("description") == text
        and not any(embeds[0].get(key) for key in ("title", "footer", "fields", "author"))
    )


def find_messages(client, channel, pins, bot_id, hook=None):
    """Find the newest matching sticky within the channel's latest 1,000 messages."""
    matches = {kind: [] for kind in pins}
    before, scanned = None, 0
    while scanned < HISTORY_LIMIT:
        query = {"limit": min(100, HISTORY_LIMIT - scanned)}
        if before:
            query["before"] = before
        page = client.request("Discord", f"channels/{channel}/messages?" + urlencode(query))
        if not isinstance(page, list):
            raise MigrationError("Unexpected Discord message response")
        if not page:
            print(f"Channel {channel}: no matching sticky after {scanned:,} messages", flush=True)
            return matches
        page.sort(key=lambda message: int(snowflake(message["id"])), reverse=True)
        if before and any(int(message["id"]) >= int(before) for message in page):
            raise MigrationError("Discord history pagination did not advance")
        for message in page:
            scanned += 1
            if hook and str(message.get("webhook_id")) == hook[1] and not message.get("embeds"):
                try:
                    message = client.request(
                        "Discord", f"{hook[0]}/messages/{snowflake(message['id'])}", webhook=True
                    )
                except RequestError as error:
                    if error.status == 404:
                        continue
                    raise
            for kind, text in pins.items():
                if message_matches(message, kind, text, bot_id, hook[1] if hook else None):
                    matches[kind].append(snowflake(message["id"]))
            # One message can match multiple saved kinds; retain each configuration.
            if any(matches.values()):
                print(
                    f"Channel {channel}: found newest sticky {message['id']} "
                    f"after {scanned:,} messages; moving on",
                    flush=True,
                )
                return matches
        if len(page) < 100:
            print(f"Channel {channel}: no matching sticky after {scanned:,} messages", flush=True)
            return matches
        before = str(page[-1]["id"])
    print(
        f"Channel {channel}: no matching sticky in the latest {scanned:,} messages; "
        "keeping saved sticky text for reposting; moving on",
        flush=True,
    )
    return matches


def deferred_channel(channel, rows, existing, reason):
    deferred = {"reason": reason, "rows": rows}
    if existing:
        current = json.loads(existing["value"])
        previous = current["migration_deferred"].get("previous_state") if "migration_deferred" in current else current
        if previous:
            deferred["previous_state"] = previous
    print(f"Skipped channel {channel}: {reason}; saved settings preserved for a later retry", flush=True)
    return {
        "channel": channel,
        "state": {"id": channel, "migration_deferred": deferred},
        "remove": [], "rows": rows, "action": "defer",
    }


def plan_channel(client, channel, rows, existing, bot_id):
    channel = snowflake(channel)
    try:
        info = client.request("Discord", f"channels/{channel}")
    except RequestError as error:
        if error.status == 404:
            print(f"Channel {channel}: HTTP 404; removing stale database records only", flush=True)
            return {"channel": channel, "state": None, "remove": [], "rows": rows, "action": "delete"}
        if error.status == 403:
            return deferred_channel(channel, rows, existing, "Discord HTTP 403")
        raise
    if not info.get("guild_id") or info.get("type") not in {0, 5}:
        return deferred_channel(channel, rows, existing, f"Unsupported channel type {info.get('type')}")
    state = {"id": channel, "guild_id": snowflake(info["guild_id"]), "pins": {}}
    pins = {}
    for row in rows:
        if not isinstance(row["value"], str):
            raise MigrationError("Legacy record value must be a string")
        if row["kind"] in KINDS:
            pins[KINDS[row["kind"]]] = row["value"]
        elif row["kind"] in SETTINGS:
            state[SETTINGS[row["kind"]]] = row["value"]
    if existing:
        current = json.loads(existing["value"])
        if "migration_deferred" in current:
            current = current["migration_deferred"].get("previous_state")
    else:
        current = None
    if current:
        if any(current.get(key) != value for key, value in state.items() if key != "pins"):
            raise MigrationError("Legacy settings conflict with an existing converted channel")
        if any(
            current.get("pins", {}).get(kind, {}).get("text") != text for kind, text in pins.items()
        ):
            raise MigrationError("Legacy sticky data conflicts with an existing converted channel")
        if len(current.get("pins", {})) > 1:
            current["preserve_legacy_pins"] = True
        return {"channel": channel, "state": current, "remove": [], "rows": rows}
    if any(
        not text or len(text) > (2000 if kind in {"classic", "slow"} else 4096) for kind, text in pins.items()
    ):
        raise MigrationError("Sticky text is empty or exceeds Discord's message limit")
    matches = {kind: [] for kind in pins}
    try:
        hook = None
        if pins:
            client.require_history(info, bot_id)
        if "webhook" in pins:
            hook = webhook_path(state.get("webhook_url", ""))
            hook_info = client.request("Discord", hook[0], webhook=True)
            if str(hook_info.get("channel_id")) != channel:
                raise MigrationError("Stored webhook belongs to another channel")
        if pins:
            matches = find_messages(client, channel, pins, bot_id, hook)
    except (RequestError, HistoryUnavailable) as error:
        if isinstance(error, RequestError) and error.status not in {403, 404}:
            raise
        print(f"Channel {channel}: message recovery skipped ({error}); saved stickies kept for reposting", flush=True)
    for kind, text in pins.items():
        latest = max(map(int, matches[kind]), default=0)
        message = str(latest) if latest else None
        state["pins"][kind] = {
            "text": text,
            "message_id": message,
            "posted_at": ((int(message) >> 22) + 1420070400000) / 1000 if message else 0,
            "retired_ids": [],
        }
    if len(pins) > 1:
        state["preserve_legacy_pins"] = True
    return {"channel": channel, "state": state, "remove": [], "rows": rows}


def signature(rows):
    return {(r["kind"], r["id"]): r["value"] for r in rows}


def write_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())


@contextlib.contextmanager
def local_database(config, root):
    path = (root / config.get("database_path", "data/anchorbot.sqlite3")).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+b") as lock:
        try:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MigrationError("Stop the running AnchorBot before migrating") from None
        db = sqlite3.connect(path) if path.exists() else None
        try:
            if db and db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]:
                raise MigrationError(
                    "The local database has unsynced changes; sync them with the bot before migrating"
                )
            yield db
        finally:
            if db:
                db.close()


def migrate(client, root, db, dry_run):
    rows = client.records()
    unknown = sorted({r["kind"] for r in rows} - set(KINDS) - set(SETTINGS) - REMOVED - {"channel"})
    if unknown:
        raise MigrationError("Unknown record kinds were preserved: " + ", ".join(unknown))
    backup = root / "migration-backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    write_json(backup / "records.json", rows)
    if db:
        with contextlib.closing(sqlite3.connect(backup / "local.sqlite3")) as destination:
            db.backup(destination)
    print(f"Backup: {backup}", flush=True)
    existing = {r["id"]: r for r in rows if r["kind"] == "channel"}
    grouped = defaultdict(list)
    for row in rows:
        if row["kind"] in KINDS or row["kind"] in SETTINGS:
            grouped[row["id"]].append(row)
    for channel, row in existing.items():
        deferred = json.loads(row["value"]).get("migration_deferred")
        if deferred:
            by_kind = {r["kind"]: r for r in deferred["rows"]}
            for source in grouped[channel]:
                if source["kind"] in by_kind and source["value"] != by_kind[source["kind"]]["value"]:
                    raise MigrationError(f"Deferred settings conflict with legacy records for channel {channel}")
                by_kind[source["kind"]] = source
            grouped[channel] = list(by_kind.values())
    plans, errors = [], []
    bot_id = snowflake(client.request("Discord", "users/@me")["id"]) if grouped else None
    if grouped and bot_id != str(client.config.get("application_id")):
        raise MigrationError("The bot token does not match application_id in config.json")
    for channel, source in sorted(grouped.items()):
        print(f"Checking channel {channel}", flush=True)
        try:
            plans.append(plan_channel(client, channel, source, existing.get(channel), bot_id))
        except (MigrationError, ValueError, KeyError) as error:
            errors.append({"channel": channel, "error": str(error)})
            print(f"Blocked channel {channel}: {error}", flush=True)
    write_json(backup / "plan.json", {"channels": plans, "errors": errors})
    if errors:
        raise MigrationError(
            "Preflight failed. No cloud records or Discord messages were changed; resolve reported channels and rerun"
        )
    retired = [r for r in rows if r["kind"] in REMOVED]
    deleted = sum(p.get("action") == "delete" for p in plans)
    deferred = sum(p.get("action") == "defer" for p in plans)
    print(
        f"Plan: {len(plans) - deleted - deferred} channels converted, "
        f"{deferred} deferred with settings preserved, {deleted} stale 404 channels removed, "
        f"{len(retired)} obsolete prefix/disabled records"
    )
    if dry_run:
        print("Dry run complete. No cloud records or Discord messages were changed.")
        return
    if signature(client.records()) != signature(rows):
        raise MigrationError("Cloud data changed during preflight. Stop both bots and rerun")
    for plan in plans:
        channel, state = plan["channel"], plan["state"]
        changes = [{
            "kind": "channel", "id": channel,
            "value": json.dumps(state, separators=(",", ":")) if state is not None else None,
        }]
        changes += [{"kind": r["kind"], "id": r["id"], "value": None} for r in plan["rows"]]
        client.request("Convex", "batch", {"changes": changes})
        action = {"delete": "Removed stale database records for", "defer": "Deferred"}.get(
            plan.get("action"), "Converted"
        )
        print(f"{action} channel {channel}", flush=True)
    expected = {r["id"]: json.loads(r["value"]) for r in rows if r["kind"] == "channel"}
    for plan in plans:
        if plan["state"] is None:
            expected.pop(plan["channel"], None)
        else:
            expected[plan["channel"]] = plan["state"]
    current = client.records()
    if {r["id"]: json.loads(r["value"]) for r in current if r["kind"] == "channel"} != expected:
        raise MigrationError("Converted channel verification failed; backup retained")
    for offset in range(0, len(retired), 100):
        client.request(
            "Convex",
            "batch",
            {
                "changes": [
                    {"kind": r["kind"], "id": r["id"], "value": None} for r in retired[offset : offset + 100]
                ]
            },
        )
    final = client.records()
    if (
        any(r["kind"] != "channel" for r in final)
        or {r["id"]: json.loads(r["value"]) for r in final} != expected
    ):
        raise MigrationError("Final verification failed; backup retained")
    if db:
        with db:
            db.execute("DELETE FROM channels")
            db.executemany(
                "INSERT INTO channels VALUES (?,?,?)",
                [(state["id"], state["guild_id"], json.dumps(state)) for state in expected.values()
                 if "migration_deferred" not in state],
            )
            db.execute("INSERT OR REPLACE INTO meta VALUES ('snapshot_loaded', 'true')")
    write_json(backup / "completed.json", {
        "channels": sum("migration_deferred" not in state for state in expected.values()),
        "deferred_channels": deferred, "stale_channels_removed": deleted, "duplicates_removed": 0,
    })
    print("Migration verified. Start only the new bot. Keep the backup private.", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Stop both bots and deploy the current Convex backend first. Converts legacy records and searches up to 1,000 recent messages per channel, stopping at the first match. Saved stickies without a match are kept for reposting. Older messages are left untouched. Uses only the Python standard library."
    )
    parser.add_argument("--config", default=str(Path(__file__).resolve().with_name("config.json")))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Back up and report planned changes without modifying Convex or Discord",
    )
    args = parser.parse_args()
    try:
        path = Path(args.config).resolve()
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        for key in ("bot_token", "application_id", "convex_site_url", "convex_key"):
            if not isinstance(config.get(key), str) or not config[key]:
                raise MigrationError(f"Set {key} in config.json")
        url = urlparse(config["convex_site_url"])
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise MigrationError("convex_site_url must be an HTTPS Convex site URL")
        for outbox in (path.parent / "convex-outbox.jsonl", path.parent / "old/convex-outbox.jsonl"):
            if outbox.exists() and outbox.read_text(encoding="utf-8-sig").strip():
                raise MigrationError(
                    "The old bot has queued writes in convex-outbox.jsonl. Flush them with the old bot before deploying/migrating"
                )
        print("Both old and new bots must remain stopped for the entire migration.", flush=True)
        with local_database(config, path.parent) as db:
            migrate(Client(config), path.parent, db, args.dry_run)
        return 0
    except (MigrationError, ValueError, OSError, sqlite3.Error) as error:
        print(f"Migration stopped: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Migration interrupted. Keep the bots stopped and rerun to finish.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
