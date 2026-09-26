import argparse
import asyncio
import json
import signal
from pathlib import Path

from .config import load
from .logging_setup import configure


def main():
    parser = argparse.ArgumentParser(prog="anchorbot")
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["run", "worker", "maintenance", "check-config", "status", "reload", "self-test"],
        default="run",
    )
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--worker-id", type=int, default=0)
    parser.add_argument("--shards", default="0")
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args()
    try:
        config = load(
            args.config, credentials=not args.simulate and args.mode not in {"status", "reload", "self-test"}
        )
    except (ValueError, OSError, KeyError) as error:
        parser.exit(2, f"Configuration error: {error}\n")
    if args.mode == "check-config":
        print("Configuration valid.")
        return
    if args.mode == "self-test":
        import tempfile
        from types import SimpleNamespace

        from .worker import Worker

        with tempfile.TemporaryDirectory(prefix="anchorbot-check-") as directory:
            config["database_path"] = str(Path(directory) / "check.sqlite3")
            config["bot_token"] = "MTAwMDAwMDAwMDAwMDAwMDAw.offline.test"
            worker = Worker(config, SimpleNamespace(shard_count=1, shards=[0], worker_id=0, parent_pid=1))
            try:
                for builder in worker.router.builders:
                    builder.build(worker.bot.entity_factory)
                for view in ("overview", "shards", "guilds"):
                    result, rows = worker.admin.render(view, 0)
                    worker.bot.entity_factory.serialize_embed(result)
                    for row in rows:
                        row.build()
                print(
                    f"Offline self-test passed: Hikari {len(worker.router.builders)} commands, menus, SQLite, timezone data."
                )
            finally:
                worker.db.close()
        return
    if args.mode in {"status", "reload"}:
        from .storage import LocalDB

        if not Path(config["database_path"]).exists():
            parser.exit(2, "No local database yet. Start the supervisor first.\n")
        db = LocalDB(config["database_path"])
        print(
            json.dumps(db.workers() if args.mode == "status" else {"request": db.control("reload")}, indent=2)
        )
        db.close()
        return
    configure(config, "supervisor" if args.mode == "run" else f"worker-{args.worker_id}")
    if args.mode in {"worker", "maintenance"}:
        args.shards = list(map(int, args.shards.split(",")))
        if args.mode == "maintenance":
            from .maintenance import MaintenanceWorker

            asyncio.run(MaintenanceWorker(config, args).run())
            return
        from .worker import Worker, simulate

        asyncio.run(simulate(config, args) if args.simulate else Worker(config, args).run())
    else:
        from .supervisor import Supervisor

        async def run():
            supervisor = Supervisor(config, simulation=args.simulate)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(supervisor.stop.set))
            await supervisor.run()

        asyncio.run(run())


if __name__ == "__main__":
    main()
