import asyncio
import logging
import math
import os
import random
import subprocess
import sys
import time
from datetime import UTC, datetime

import aiohttp
import psutil

from . import config as configuration
from .balancing import balanced_plan, improvement, next_restart
from .cloud import Cloud, UnsupportedCloudState
from .command_sync import synchronize
from .processes import kill_tree, matching_process, reported_process
from .restarts import RestartResponder
from .storage import LocalDB
from .topgg import post_stats

log = logging.getLogger(__name__)


class GatewayRateLimited(RuntimeError):
    pass


class Supervisor:
    def __init__(self, config, *, simulation=False):
        self.config = config
        self.db = LocalDB(config["database_path"])
        self.children = {}
        self.identities = {}
        self.failures = {}
        self.retry_at = {}
        self.redeploy_at = 0
        self.redeploy_failures = 0
        self.gateway_retry_at = 0
        self.restart_responder = None
        self.plan = []
        self.stop = asyncio.Event()
        self.simulation = simulation
        self.started = time.time()
        self.cloud_writes_allowed = True

    def spawn(self, worker, shards):
        command = [sys.executable, "-B", "-m", "anchorbot"]
        command += [
            "worker",
            "--config",
            self.config["_path"],
            "--worker-id",
            str(worker),
            "--shards",
            ",".join(map(str, shards)),
            "--shard-count",
            str(self.config["shard_count"]),
            "--parent-pid",
            str(os.getpid()),
        ]
        if self.simulation:
            command.append("--simulate")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.db.put(f"stop_worker_{worker}", False)
        process = subprocess.Popen(command, creationflags=flags, stdout=sys.stdout, stderr=sys.stderr)
        self.identities.pop(worker, None)
        self.children[worker] = (process, time.time())
        log.info("worker_spawn worker=%s pid=%s shards=%s", worker, process.pid, shards)

    async def stop_workers(self):
        for worker in self.children:
            self.db.put(f"stop_worker_{worker}", True)
        deadline = time.monotonic() + self.config["shutdown_timeout_seconds"]
        while any(p.poll() is None for p, _ in self.children.values()) and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        for worker, (process, _) in self.children.items():
            identity = self.identities.get(worker)
            if process.poll() is None or (identity and identity.is_running()):
                await asyncio.to_thread(kill_tree, process, identity)
            await asyncio.to_thread(process.wait)
        self.children.clear()
        self.identities.clear()

    async def deploy(self, config=None, plan=None, *, preflight=True):
        config = config or self.config
        if not self.simulation and preflight:
            await self.gateway_limits(config)
        await self.stop_workers()
        self.config = config
        if hasattr(self, "cloud"):
            self.cloud.config = config
        self.plan = plan or balanced_plan(self.db, config["shard_count"], config["worker_count"])[0]
        with self.db.db:
            self.db.db.execute("DELETE FROM workers")
        self.db.put(
            "deployment",
            {
                "shard_count": config["shard_count"],
                "plan": self.plan,
                "started": self.started,
                "deployed": time.time(),
            },
        )
        self.failures.clear()
        self.retry_at.clear()
        try:
            for worker, shards in enumerate(self.plan):
                self.spawn(worker, shards)
        except Exception:
            await self.stop_workers()
            raise
        log.info("shards_deployed count=%s workers=%s", config["shard_count"], len(self.plan))

    async def recover_deployment(self):
        if self.children:
            self.redeploy_at = self.redeploy_failures = 0
            return
        if time.monotonic() < self.redeploy_at:
            return
        try:
            await self.deploy()
        except Exception:
            self.redeploy_failures += 1
            delay = min(300, 2 ** min(self.redeploy_failures, 9))
            self.redeploy_at = time.monotonic() + delay
            log.exception("empty_deployment_retry retry_seconds=%s", delay)
        else:
            self.redeploy_at = self.redeploy_failures = 0

    async def gateway_limits(self, config, *, deployment=True):
        if time.time() < self.gateway_retry_at:
            raise GatewayRateLimited("Discord gateway preflight is waiting for its retry deadline")
        try:
            async with self.session.get(
                "https://discord.com/api/v10/gateway/bot",
                headers={"Authorization": "Bot " + config["bot_token"]},
            ) as response:
                if response.status == 429:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after is None:
                        try:
                            data = await response.json()
                            retry_after = data.get("retry_after", 30) if isinstance(data, dict) else 30
                        except (ValueError, aiohttp.ContentTypeError):
                            retry_after = 30
                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        delay = 30
                    delay = max(1, delay) if math.isfinite(delay) else 30
                    self.gateway_retry_at = time.time() + delay
                    log.warning("gateway_rate_limited retry_seconds=%.1f", delay)
                    raise GatewayRateLimited(f"Discord gateway preflight rate limited; retry in {delay:.1f}s")
                if response.status != 200:
                    raise RuntimeError(f"Discord gateway preflight returned HTTP {response.status}")
                gateway = await response.json()
            limit = gateway["session_start_limit"]
            self.db.put("gateway_info", {"fetched_monotonic": time.monotonic(), "value": gateway})
            self.db.put(
                "identify_budget",
                {
                    "remaining": limit["remaining"],
                    "total": limit["total"],
                    "reset_at": time.time() + max(60, limit["reset_after"] / 1000),
                    "concurrency": limit["max_concurrency"],
                },
            )
        except GatewayRateLimited:
            raise
        except Exception:
            self.gateway_retry_at = time.time() + 30
            raise
        self.gateway_retry_at = 0
        if deployment:
            if config["shard_count"] < gateway["shards"]:
                raise ValueError(f"Discord recommends/requires at least {gateway['shards']} shards")
            if limit["remaining"] < config["shard_count"]:
                raise ValueError("Discord session start budget is too low to deploy all shards now")
        return gateway

    async def scheduled_restart(self):
        config = configuration.load(self.config["_path"], credentials=not self.simulation)
        if config["database_path"] != self.config["database_path"]:
            raise ValueError("Changing database_path requires stopping the supervisor")
        if self.simulation or not self.children:
            await self.deploy(config)
            return
        if self.restart_responder:
            return
        gateway = await self.gateway_limits(config)
        count = 1 if gateway["shards"] == 1 else config["shard_count"]
        if gateway["session_start_limit"]["remaining"] < config["shard_count"] + count:
            raise ValueError("Not enough Discord sessions for a restart responder and replacement shards")
        responder = RestartResponder(self)
        try:
            await responder.start(count)
        except Exception:
            await responder.close()
            raise
        self.restart_responder = responder
        self.db.put("restart_active", True)
        log.info("scheduled_restart_begin responder_shards=%s", count)
        await self.deploy(config, preflight=False)
        if await responder.wait_for_workers():
            await self.finish_restart()
        else:
            log.warning("restart_waiting_for_replacement_workers")

    async def finish_restart(self):
        self.db.put("restart_active", False)
        await self.restart_responder.close()
        self.restart_responder = None
        log.info("scheduled_restart_complete")

    async def refresh_gateway_budget(self):
        budget = self.db.get("identify_budget", {})
        if time.time() < max(budget.get("reset_at", 0), self.gateway_retry_at):
            return
        try:
            await self.gateway_limits(self.config, deployment=False)
        except GatewayRateLimited:
            pass
        except Exception:
            log.exception("gateway_budget_refresh_failed")

    async def monitor(self):
        workers = {w["id"]: w for w in self.db.workers()}
        for worker, (process, started) in list(self.children.items()):
            row = workers.get(worker, {})
            identity = reported_process(process, row)
            if identity:
                self.identities[worker] = identity
            heartbeat = row.get("heartbeat", started) if identity else started
            stale = time.time() - heartbeat > self.config["worker_timeout_seconds"]
            if process.poll() is None and not stale:
                if time.time() - started > 600:
                    self.failures[worker] = 0
                continue
            if process.poll() is None:
                log.error(
                    "worker_unresponsive worker=%s launcher_pid=%s worker_pid=%s "
                    "reason=%s heartbeat_age=%.1f unverified_age=%.1f",
                    worker,
                    process.pid,
                    row.get("pid"),
                    "heartbeat_timeout" if identity else "process_identity_unverified",
                    time.time() - row["heartbeat"] if "heartbeat" in row else -1,
                    time.time() - started if not identity else 0,
                )
                await asyncio.to_thread(kill_tree, process, self.identities.get(worker))
                await asyncio.to_thread(process.wait)
            elif self.identities.get(worker) and self.identities[worker].is_running():
                await asyncio.to_thread(kill_tree, process, self.identities[worker])
            if worker not in self.retry_at:
                failures = self.failures.get(worker, 0) + 1
                self.failures[worker] = failures
                delay = min(300, 2 ** min(failures, 8)) + random.random()
                self.retry_at[worker] = time.time() + delay
                log.error(
                    "worker_failed worker=%s exit=%s retry_seconds=%.1f", worker, process.returncode, delay
                )
            if time.time() >= self.retry_at[worker]:
                del self.retry_at[worker]
                try:
                    self.spawn(worker, self.plan[worker])
                except Exception:
                    log.exception("worker_respawn_failed worker=%s", worker)

    async def controls(self):
        row = self.db.db.execute(
            "SELECT * FROM controls WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return
        try:
            config = configuration.load(self.config["_path"], credentials=not self.simulation)
            if config["database_path"] != self.config["database_path"]:
                raise ValueError("Changing database_path requires stopping the supervisor")
            if row["action"] == "resize":
                config["shard_count"] = row["count"]
                if config["shard_count"] < 1:
                    raise ValueError("At least one shard is required")
            if row["action"] not in {"reload", "resize", "rebalance"}:
                raise ValueError("Unknown supervisor operation")
            await self.deploy(config)
            if row["action"] == "resize":
                configuration.set_shard_count(config["_path"], row["count"])
            self.cloud.config = config
            from .logging_setup import configure

            configure(config, "supervisor")
            status = "completed"
        except Exception as error:
            status = f"failed: {error}"
            log.exception("deployment_failed request=%s", row["id"])
        with self.db.db:
            self.db.db.execute("UPDATE controls SET status=? WHERE id=?", (status, row["id"]))
        self.db.put("last_control", {"id": row["id"], "status": status})

    async def run(self):
        from .process_lock import ProcessLock

        with ProcessLock(self.config["database_path"] + ".lock"):
            await self._run_locked()

    async def _run_locked(self):
        for row in [*self.db.workers(), self.db.get("maintenance_worker", {})]:
            if not row.get("pid"):
                continue
            try:
                process = matching_process(row)
                if process is not None:
                    process.kill()
                    await asyncio.to_thread(process.wait, 10)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                pass
        self.db.put("restart_active", False)
        self.db.put("maintenance_worker", {})
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as self.session:
            self.cloud = Cloud(self.config, self.db, self.session)
            try:
                if not self.simulation:
                    while not self.stop.is_set():
                        try:
                            await self.cloud.bootstrap()
                            break
                        except UnsupportedCloudState:
                            self.cloud_writes_allowed = False
                            raise
                        except Exception:
                            log.exception("bootstrap_retry_in_30_seconds")
                            if self.db.get("snapshot_loaded", False):
                                log.warning("starting_from_local_snapshot_cloud_sync_will_retry")
                                break
                            await self.pause(30)
                    while not self.stop.is_set():
                        try:
                            await synchronize(self.config)
                            break
                        except Exception:
                            log.exception("startup_command_sync_retry_in_30_seconds")
                            await self.pause(30)
                if self.stop.is_set():
                    return
                while not self.stop.is_set():
                    try:
                        await self.deploy()
                        break
                    except Exception:
                        log.exception("initial_deployment_retry_in_30_seconds")
                        await self.pause(30)
                restart = next_restart(
                    datetime.now(UTC), self.config["restart_timezone"], self.config["restart_time"]
                )
                restart_config = (self.config["restart_timezone"], self.config["restart_time"])
                sync_at = rebalance_at = topgg_at = time.time()
                while not self.stop.is_set():
                    self.db.put(
                        "supervisor",
                        {
                            "pid": os.getpid(),
                            "heartbeat": time.time(),
                            "started": self.started,
                            "next_restart": restart.isoformat(),
                        },
                    )
                    await self.monitor()
                    await self.controls()
                    await self.recover_deployment()
                    if self.restart_responder:
                        if self.restart_responder.workers_ready():
                            await self.finish_restart()
                        else:
                            await self.restart_responder.ensure_running()
                    if restart_config != (self.config["restart_timezone"], self.config["restart_time"]):
                        restart_config = (self.config["restart_timezone"], self.config["restart_time"])
                        restart = next_restart(datetime.now(UTC), *restart_config)
                    now = time.time()
                    if not self.simulation:
                        await self.refresh_gateway_budget()
                    if now >= sync_at and not self.simulation:
                        try:
                            await self.cloud.flush()
                            self.db.put("cloud", {"ok": True, "synced": now})
                        except Exception:
                            log.exception("cloud_sync_failed_outbox_retained")
                            self.db.put("cloud", {"ok": False, "failed": now})
                        sync_at = now + self.config["convex_sync_interval_seconds"]
                    if datetime.now(UTC) >= restart:
                        try:
                            await self.scheduled_restart()
                        except Exception:
                            log.exception("scheduled_restart_failed")
                        restart = next_restart(
                            datetime.now(UTC), self.config["restart_timezone"], self.config["restart_time"]
                        )
                    if now >= rebalance_at:
                        plan, weights = balanced_plan(
                            self.db, self.config["shard_count"], self.config["worker_count"]
                        )
                        if (
                            plan != self.plan
                            and improvement(self.plan, plan, weights)
                            >= self.config["rebalance_min_improvement"]
                        ):
                            try:
                                await self.deploy(plan=plan)
                            except Exception:
                                log.exception("automatic_rebalance_failed")
                        with self.db.db:
                            cutoff = now - self.config["metrics_retention_days"] * 86400
                            self.db.db.execute("DELETE FROM samples WHERE time<?", (cutoff,))
                            self.db.db.execute("DELETE FROM interactions WHERE created<?", (now - 900,))
                            self.db.db.execute("DELETE FROM guild_load WHERE updated<?", (cutoff,))
                            self.db.db.execute(
                                "DELETE FROM controls WHERE created<? AND status!='pending'", (cutoff,)
                            )
                        rebalance_at = now + self.config["rebalance_interval_seconds"]
                    if now >= topgg_at and self.config.get("topgg_token") and not self.simulation:
                        try:
                            posted = await post_stats(self.config, self.db, self.session)
                        except Exception:
                            log.exception("topgg_stats_failed")
                            topgg_at = now + 1800
                        else:
                            topgg_at = now + (1800 if posted else 30)
                    await self.pause(1)
            finally:
                self.db.put("restart_active", False)
                if self.restart_responder:
                    try:
                        await self.restart_responder.close()
                    except Exception:
                        log.exception("restart_responder_shutdown_failed")
                await self.stop_workers()
                if not self.simulation and self.cloud_writes_allowed:
                    try:
                        await asyncio.wait_for(self.cloud.flush(), 20)
                    except Exception:
                        log.exception("shutdown_sync_deferred_to_next_start")
                self.db.close()

    async def pause(self, seconds):
        try:
            await asyncio.wait_for(self.stop.wait(), seconds)
        except TimeoutError:
            pass
