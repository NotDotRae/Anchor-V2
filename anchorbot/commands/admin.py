import math
import time
from pathlib import Path

import hikari
import psutil

from ..style import embed, uptime


class AdminCommands:
    def __init__(self, app):
        self.app = app
        app.router.add("admin", "Owner administration.", self.stats, subcommand="stats")
        app.router.add("reload", "Reload and deploy the configured shards.", self.reload, subcommand="shards")

    async def stats(self, ctx):
        if await ctx.owner():
            await ctx.defer(ephemeral=True)
            result, components = self.render("overview", 0)
            await ctx.reply(embed=result, components=components, ephemeral=True)

    async def reload(self, ctx):
        if not await ctx.owner():
            return
        request = self.app.db.control("reload")
        await ctx.reply(
            f"Shard reload queued (request {request}). The supervisor will reread config.json and deploy the configured shards. Use `/admin stats` to check the result.",
            ephemeral=True,
        )

    def render(self, view, page):
        db = self.app.db
        workers = db.workers()
        shards = [(w, s) for w in workers for s in w.get("shards", [])]
        result = embed("AnchorBot Admin Stats", "Runtime and shard status for AnchorBot.")
        deployment = db.get("deployment", {})
        count = deployment.get("shard_count", self.app.count)
        supervisor = db.get("supervisor", {})
        if view == "overview":
            pages = 1
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage(str(Path(self.app.config["database_path"]).parent))
            result.add_field("Available processors:", str(psutil.cpu_count()))
            result.add_field("Total servers:", str(sum(w.get("guilds", 0) for w in workers)), inline=True)
            result.add_field("Tracked messages:", str(sum(w.get("pins", 0) for w in workers)), inline=True)
            result.add_field("Maximum memory:", f"{memory.total / 2**20:,.0f} MiB", inline=True)
            result.add_field("Free memory:", f"{memory.available / 2**20:,.0f} MiB", inline=True)
            result.add_field(
                "Worker memory:", f"{sum(w.get('rss', 0) for w in workers) / 2**20:,.1f} MiB", inline=True
            )
            result.add_field(
                "AnchorBot session uptime:",
                f"Uptime: ``{uptime(time.time() - supervisor.get('started', time.time()))}``",
            )
            result.add_field("Shards:", f"{count} shards across {len(workers)} worker processes")
            result.add_field("Next restart:", supervisor.get("next_restart", "Unknown"))
            outbox = db.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            result.add_field(
                "Convex sync:", f"Pending channel updates: {outbox}\nStatus: {db.get('cloud', {})}"
            )
            result.add_field(
                "Disk:",
                f"Total space: {disk.total / 2**30:,.1f} GiB\nFree space: {disk.free / 2**30:,.1f} GiB",
            )
            result.add_field("Last operation:", str(db.get("last_control", "None"))[:1024])
        elif view == "shards":
            pages = max(1, math.ceil(len(shards) / 8))
            page = max(0, min(page, pages - 1))
            for worker, shard in shards[page * 8 : page * 8 + 8]:
                stale = time.time() - worker["heartbeat"] > self.app.config["worker_timeout_seconds"]
                latency = shard.get("latency_ms")
                result.add_field(
                    f"Shard {shard['id']}",
                    f"Status: {'UNRESPONSIVE' if stale else shard['status']}\nWorker: {worker['id']} · PID: {worker['pid']}\n"
                    f"Uptime: {uptime(time.time() - shard.get('started', worker['started']))}\n"
                    f"Ping: {round(latency) if latency is not None else 'unavailable'} ms\nGuilds: {shard.get('guilds', 0)}\n"
                    f"Process CPU: {worker.get('cpu_percent', 0):.1f}% · Memory: {worker.get('rss', 0) / 2**20:.1f} MiB\n"
                    f"Process events: {worker.get('events', 0):,} · REST requests: {worker.get('requests', 0):,}",
                )
        elif view == "guilds":
            guilds = list(db.db.execute("SELECT * FROM guild_load ORDER BY busy_rate DESC,request_rate DESC"))
            pages = max(1, math.ceil(len(guilds) / 10))
            page = max(0, min(page, pages - 1))
            for row in guilds[page * 10 : page * 10 + 10]:
                shard = (int(row["guild"]) >> 22) % count
                result.add_field(
                    f"Guild {row['guild']} · Shard {shard}",
                    f"Average events/s: {row['event_rate']:.3f}\nAverage REST requests/s: {row['request_rate']:.3f}\n"
                    f"Measured handler CPU seconds/s: {row['busy_rate']:.6f}\nSamples: {row['samples']}",
                )
            if not guilds:
                result.add_field("Guild load:", "Waiting for worker samples.")
        else:
            raise ValueError("Unknown admin stats view.")
        result.set_footer(f"AnchorBot · {view.title()} · Page {page + 1}/{pages}")
        row = hikari.impl.MessageActionRowBuilder()
        for target, label in (
            ("overview", "Overview"),
            ("shards", "Shards"),
            ("guilds", "Guild Load"),
        ):
            row.add_interactive_button(
                hikari.ButtonStyle.PRIMARY if view == target else hikari.ButtonStyle.SECONDARY,
                f"admin:{target}:0",
                label=label,
            )
        nav = hikari.impl.MessageActionRowBuilder()
        nav.add_interactive_button(
            hikari.ButtonStyle.SECONDARY,
            f"admin:{view}:{max(0, page - 1)}:previous",
            label="Previous",
            is_disabled=page == 0,
        )
        nav.add_interactive_button(
            hikari.ButtonStyle.SECONDARY, f"admin:{view}:{page}:refresh", label="Refresh"
        )
        nav.add_interactive_button(
            hikari.ButtonStyle.SECONDARY,
            f"admin:{view}:{page + 1}:next",
            label="Next",
            is_disabled=page >= pages - 1,
        )
        return result, [row, nav]

    async def component(self, interaction):
        if not interaction.custom_id.startswith("admin:"):
            return
        if int(interaction.user.id) != int(self.app.config["owner_id"]):
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                "Only configured bot owners can use this command.",
                flags=hikari.MessageFlag.EPHEMERAL,
            )
            return
        deferred = False
        try:
            parts = interaction.custom_id.split(":")
            if len(parts) == 4 and parts[3] in {"previous", "refresh", "next"}:
                parts = parts[:3]
            _, view, raw_page = parts
            page = int(raw_page)
            if view in {"manage", "resize", "resize_delta", "rebalance"}:
                raise ValueError("This menu is outdated. Reopen `/admin stats`.")
            if view not in {"overview", "shards", "guilds"}:
                return
            await interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_UPDATE)
            deferred = True
            result, components = self.render(view, page)
            await interaction.edit_initial_response(embed=result, components=components)
        except ValueError as error:
            if deferred:
                await interaction.execute(str(error), flags=hikari.MessageFlag.EPHEMERAL)
            else:
                await interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE, str(error), flags=hikari.MessageFlag.EPHEMERAL
                )
        except Exception:
            import logging

            logging.getLogger(__name__).exception("admin_component_failed")
            if deferred:
                try:
                    await interaction.execute(
                        "Unable to load admin stats. Check the bot console for the error.",
                        flags=hikari.MessageFlag.EPHEMERAL,
                    )
                except hikari.HikariError:
                    logging.getLogger(__name__).exception("admin_error_response_failed")
