import asyncio
import logging
import os
import subprocess
import sys
import time

from .processes import kill_tree, reported_process

log = logging.getLogger(__name__)


class RestartResponder:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.process = None
        self.identity = None
        self.retry_at = 0
        self.failures = 0

    async def start(self, count):
        self.count = count
        supervisor = self.supervisor
        supervisor.db.put("maintenance_worker", {})
        supervisor.db.put("stop_maintenance", False)
        command = [
            sys.executable,
            "-B",
            "-m",
            "anchorbot",
            "maintenance",
            "--config",
            supervisor.config["_path"],
            "--worker-id",
            "-1",
            "--shards",
            ",".join(map(str, range(count))),
            "--shard-count",
            str(count),
            "--parent-pid",
            str(os.getpid()),
        ]
        self.process = subprocess.Popen(
            command,
            stdout=sys.stdout,
            stderr=sys.stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        log.info("restart_responder_start launcher_pid=%s shards=%s", self.process.pid, count)
        self.started = time.time()
        deadline = time.monotonic() + max(supervisor.config["worker_timeout_seconds"], count * 6 + 30)
        while not supervisor.stop.is_set() and time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Restart responder exited before connecting")
            row = supervisor.db.get("maintenance_worker", {})
            self.identity = reported_process(self.process, row)
            if self.identity and row.get("ready"):
                log.info("restart_responder_ready worker_pid=%s", self.identity.pid)
                return
            await supervisor.refresh_gateway_budget()
            await supervisor.pause(0.5)
        raise RuntimeError("Restart responder did not connect in time; existing workers kept running")

    async def wait_for_workers(self):
        supervisor = self.supervisor
        deadline = time.monotonic() + max(
            supervisor.config["worker_timeout_seconds"],
            supervisor.config["shard_count"] * 6 + 30,
        )
        while not supervisor.stop.is_set() and time.monotonic() < deadline:
            await supervisor.monitor()
            await supervisor.refresh_gateway_budget()
            if self.workers_ready():
                return True
            await self.ensure_running()
            await supervisor.pause(0.5)
        return False

    async def ensure_running(self):
        row = self.supervisor.db.get("maintenance_worker", {})
        if self.process is not None and self.process.poll() is None:
            identity = reported_process(self.process, row)
            if identity:
                self.identity = identity
            heartbeat = row.get("heartbeat", self.started) if identity else self.started
            if time.time() - heartbeat < self.supervisor.config["worker_timeout_seconds"]:
                return
        if time.monotonic() < self.retry_at:
            return
        self.failures += 1
        delay = min(300, 2 ** min(self.failures, 9))
        log.error("restart_responder_failed retry_seconds=%s", delay)
        try:
            await self.close()
            await self.start(self.count)
        except Exception:
            log.exception("restart_responder_recovery_failed")
        self.retry_at = time.monotonic() + delay

    def workers_ready(self):
        supervisor = self.supervisor
        rows = {row["id"]: row for row in supervisor.db.workers()}
        return (
            bool(supervisor.children)
            and len(supervisor.children) == len(supervisor.plan)
            and all(
                reported_process(process, rows.get(worker, {}))
                and time.time() - rows[worker]["heartbeat"] < supervisor.config["worker_timeout_seconds"]
                and {s["id"] for s in rows[worker].get("shards", []) if s["status"] == "CONNECTED"}
                == set(supervisor.plan[worker])
                for worker, (process, _) in supervisor.children.items()
            )
        )

    async def close(self):
        if self.process is None:
            return
        self.supervisor.db.put("stop_maintenance", True)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.process.wait),
                self.supervisor.config["shutdown_timeout_seconds"],
            )
        except TimeoutError:
            await asyncio.to_thread(kill_tree, self.process, self.identity)
            await asyncio.to_thread(self.process.wait)
        if self.identity and self.identity.is_running():
            await asyncio.to_thread(kill_tree, self.process, self.identity)
        self.supervisor.db.put("maintenance_worker", {})
        self.process = self.identity = None
        log.info("restart_responder_stopped")
