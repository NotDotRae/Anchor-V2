import json
import sqlite3
import time
from pathlib import Path


class LocalDB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY, guild TEXT NOT NULL, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, value TEXT, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workers(id INTEGER PRIMARY KEY, heartbeat REAL, value TEXT);
            CREATE TABLE IF NOT EXISTS guild_load(guild TEXT PRIMARY KEY, event_rate REAL, busy_rate REAL,
                request_rate REAL, samples INTEGER, updated REAL);
            CREATE TABLE IF NOT EXISTS samples(time REAL, worker INTEGER, value TEXT);
            CREATE INDEX IF NOT EXISTS samples_time ON samples(time);
            CREATE TABLE IF NOT EXISTS controls(id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT,
                count INTEGER, status TEXT DEFAULT 'pending', created REAL);
            CREATE TABLE IF NOT EXISTS interactions(id TEXT PRIMARY KEY, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS interactions_created ON interactions(created);
        """)

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def channels(self, shards=None, count=1):
        for row in self.db.execute("SELECT value,guild FROM channels"):
            if shards is None or (int(row["guild"]) >> 22) % count in shards:
                yield json.loads(row["value"])

    def save_channel(self, state):
        value = json.dumps(state, separators=(",", ":"))
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO channels VALUES (?,?,?)", (state["id"], state["guild_id"], value)
            )
            self.db.execute(
                "INSERT INTO outbox VALUES (?,?,1) ON CONFLICT(id) DO UPDATE SET "
                "value=excluded.value,revision=outbox.revision+1",
                (state["id"], value),
            )

    def remove_channel(self, channel):
        with self.db:
            self.db.execute("DELETE FROM channels WHERE id=?", (str(channel),))
            self.db.execute(
                "INSERT INTO outbox VALUES (?,NULL,1) ON CONFLICT(id) DO UPDATE SET "
                "value=NULL,revision=outbox.revision+1",
                (str(channel),),
            )

    def install_snapshot(self, states):
        with self.db:
            self.db.execute("DELETE FROM channels")
            self.db.executemany(
                "INSERT INTO channels VALUES (?,?,?)",
                [(s["id"], s["guild_id"], json.dumps(s)) for s in states],
            )

    def pending(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM outbox LIMIT 100")]

    def acknowledge(self, rows):
        with self.db:
            self.db.executemany(
                "DELETE FROM outbox WHERE id=? AND revision=?", [(r["id"], r["revision"]) for r in rows]
            )

    def heartbeat(self, worker, value, loads):
        now = time.time()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO workers VALUES (?,?,?)", (worker, now, json.dumps(value)))
            self.db.execute("INSERT INTO samples VALUES (?,?,?)", (now, worker, json.dumps(value)))
            for guild, (events, busy, requests, interval) in loads.items():
                self.db.execute(
                    """INSERT INTO guild_load VALUES (?,?,?,?,1,?)
                    ON CONFLICT(guild) DO UPDATE SET
                    event_rate=0.9*event_rate+0.1*excluded.event_rate,
                    busy_rate=0.9*busy_rate+0.1*excluded.busy_rate,
                    request_rate=0.9*request_rate+0.1*excluded.request_rate,
                    samples=samples+1,updated=excluded.updated""",
                    (str(guild), events / interval, busy / interval, requests / interval, now),
                )

    def workers(self):
        return [
            dict(id=r["id"], heartbeat=r["heartbeat"], **json.loads(r["value"]))
            for r in self.db.execute("SELECT * FROM workers ORDER BY id")
        ]

    def control(self, action, count=None):
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO controls(action,count,created) VALUES (?,?,?)", (action, count, time.time())
            )
            return cursor.lastrowid

    def tracked_messages(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM channels AS channel, json_each(channel.value, '$.pins') AS pin "
            "WHERE NOT COALESCE(json_extract(pin.value, '$.stopping'), 0)"
        ).fetchone()[0]

    def interaction_mode(self, interaction, *, maintenance=False):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            restarting = self.get("restart_active", False)
            if maintenance and not restarting:
                return None
            result = self.db.execute(
                "INSERT OR IGNORE INTO interactions VALUES (?,?)", (str(interaction), time.time())
            )
            if not result.rowcount:
                return None
            return "restarting" if restarting else "normal"
        finally:
            self.db.commit()

    def reserve(self, key, spacing):

        now = time.time()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            deadline = self.get(key, 0)
            if deadline > now:
                return deadline - now
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(now + spacing)))
            return 0
        finally:
            self.db.commit()
