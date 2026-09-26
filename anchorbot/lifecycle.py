import asyncio
import contextlib
import logging

import psutil

from .presence import Presence

log = logging.getLogger(__name__)


async def run_gateway(app, heartbeat, close=None):
    parent = psutil.Process(app.args.parent_pid)
    presence = Presence(app)
    tasks = {
        "heartbeat": asyncio.create_task(heartbeat()),
        "start": asyncio.create_task(
            app.bot.start(
                shard_ids=app.args.shards,
                shard_count=app.count,
                activity=presence.activity(),
                check_for_updates=False,
                ignore_session_start_limit=True,
            )
        ),
        "stop": asyncio.create_task(app.stop.wait()),
    }
    try:
        while not app.stop.is_set():
            if not parent.is_running():
                log.info("worker_stop reason=parent_exited")
                break
            done, _ = await asyncio.wait(tasks.values(), timeout=5, return_when=asyncio.FIRST_COMPLETED)
            if tasks["stop"] in done:
                break
            for name, task in list(tasks.items()):
                if task not in done:
                    continue
                await task
                if name == "start":
                    del tasks[name]
                    tasks["gateway"] = asyncio.create_task(app.bot.join())
                    tasks["presence"] = asyncio.create_task(presence.run())
                elif not app.stop.is_set():
                    raise RuntimeError(f"Worker {name} task exited unexpectedly")
    except Exception:
        log.exception("worker_runtime_failed worker=%s", app.args.worker_id)
        raise
    finally:
        app.stop.set()
        for task in tasks.values():
            task.cancel()
        for task in tasks.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        try:
            if close:
                await asyncio.wait_for(close(), max(1, app.config["shutdown_timeout_seconds"] / 2))
        except TimeoutError:
            log.warning("worker_cleanup_timeout worker=%s", app.args.worker_id)
        finally:
            if app.bot.is_alive:
                await app.bot.close()
        log.info("worker_stopped worker=%s", app.args.worker_id)
