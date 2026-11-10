from urllib.parse import urlparse

import hikari

from ..command_access import is_admin
from ..permissions import readable_channels
from ..style import SAFE_MENTIONS, embed, webhook_parts


class StickyCommands:
    def __init__(self, app):
        self.app = app
        router = app.router
        for name, description in {
            "stick start": "Create a channel pin message.",
            "stick slow": "Create a slower channel pin.",
            "stick embed": "Create a pinned embed.",
            "stick webhook": "Create a pinned webhook embed.",
        }.items():
            router.add(
                name,
                description,
                self.start,
                (
                    "message",
                    {"stick embed": "Embed pin text.", "stick webhook": "Webhook pin text."}.get(
                        name, "Pin text."
                    ),
                    hikari.OptionType.STRING,
                    True,
                ),
                maximum=2000 if name in {"stick start", "stick slow"} else 4000,
            )
        for name in ("stick stop", "webhook stop"):
            router.add(
                name,
                "Stop the webhook pin in this channel."
                if name == "webhook stop"
                else "Stop pins in this channel.",
                self.stop,
            )
        for name in ("stick list",):
            router.add(name, "List active pins in this server.", self.list)
        descriptions = {
            "image set": "Set the small image for pin embeds.",
            "image big set": "Set the large image for pin embeds.",
            "webhook set": "Set the channel webhook URL.",
            "image remove": "Remove the small image for pin embeds.",
            "image big remove": "Remove the large image for pin embeds.",
            "image get": "Show the current image settings.",
            "image big get": "Show the current large image setting.",
        }
        for name, description in descriptions.items():
            router.add(
                name,
                description,
                self.settings,
                ("url", "URL.", hikari.OptionType.STRING, True) if name.endswith(" set") else None,
                maximum=1000,
            )

    async def start(self, ctx):
        if not await ctx.manage():
            return
        if not await ctx.can_post():
            return
        kinds = {"stick start": "classic", "stick slow": "slow", "stick embed": "embed", "stick webhook": "webhook"}
        kind = kinds[ctx.command_name]
        text = ctx.value("message")
        if not text:
            raise ValueError(
                "Please provide pin embed text." if kind == "embed" else "Please provide pin text."
            )
        if kind == "webhook" and not self.app.pins.states.get(str(ctx.i.channel_id), {}).get("webhook_url"):
            raise ValueError("Set the channel webhook URL first with `/webhook set`.")
        await ctx.defer(ephemeral=True)
        await self.app.pins.start(ctx.i.channel_id, ctx.i.guild_id, kind, text, authorize=ctx.require_publish)
        label = {"classic": "Pin", "slow": "Pin", "embed": "Pin embed", "webhook": "Webhook pin"}[kind]
        await ctx.reply(f"{label} started in <#{ctx.i.channel_id}>.")

    async def stop(self, ctx):
        if not await ctx.manage():
            return
        await ctx.defer(ephemeral=True)
        webhook = ctx.command_name == "webhook stop"
        await self.app.pins.stop(ctx.i.channel_id, {"webhook"} if webhook else None, authorize=ctx.require_access)
        await ctx.reply(
            "Webhook pin stopped in this channel." if webhook else f"Pins stopped in <#{ctx.i.channel_id}>."
        )

    async def settings(self, ctx):
        if not await ctx.manage():
            return
        name = ctx.command_name
        state = self.app.pins.states.get(str(ctx.i.channel_id), {})
        if name.endswith(" get"):
            result = embed("Current image settings for pin embeds")
            if state.get("image"):
                result.add_field("Small Image Link:", state["image"]).set_thumbnail(state["image"])
            if state.get("big_image"):
                result.add_field("Large Image Link:", state["big_image"]).set_image(state["big_image"])
            if not result.fields:
                await ctx.reply("No image is set for pin embeds in this channel.", ephemeral=True)
            else:
                await ctx.reply(embed=result, ephemeral=True)
            return
        if not await ctx.can_post():
            return
        await ctx.defer(ephemeral=True)
        async with self.app.pins.channel_lock(ctx.i.channel_id):
            ctx.require_publish()
            state = self.app.pins.state(ctx.i.channel_id, ctx.i.guild_id)
            key = "webhook_url" if name == "webhook set" else "big_image" if "big" in name else "image"
            if name.endswith(" set"):
                url = ctx.value("url")
                if key == "webhook_url":
                    hook, token = webhook_parts(url)
                    webhook = await self.app.bot.rest.fetch_webhook(hook, token=token)
                    if webhook.channel_id != ctx.i.channel_id or webhook.guild_id != ctx.i.guild_id:
                        raise ValueError("The webhook must belong to this channel.")
                    if state.get("webhook_url") != url and "webhook" in state["pins"]:
                        raise ValueError(
                            "Stop the webhook pin with `/webhook stop` before changing its webhook."
                        )
                elif urlparse(url).scheme not in {"https", "http"} or not urlparse(url).netloc:
                    raise ValueError("Please provide a valid image URL.")
                state[key] = url
            else:
                state.pop(key, None)
            self.app.db.save_channel(state)
        if key == "webhook_url":
            message = "Webhook URL set for this channel."
        else:
            label = "Large" if key == "big_image" else "Small"
            message = f"{label} image {'set' if name.endswith(' set') else 'removed'} for pin embeds."
        await ctx.reply(message, ephemeral=True)

    async def list(self, ctx):
        if not await ctx.manage():
            return
        await ctx.defer(ephemeral=True)
        try:
            readable = await readable_channels(ctx)
            if not is_admin(ctx.i):
                readable &= {str(ctx.i.channel_id)}
        except (hikari.ForbiddenError, hikari.NotFoundError):
            await ctx.reply("Unable to verify your channel access. Please try again later.", ephemeral=True)
            return
        guild = self.app.bot.cache.get_guild(ctx.i.guild_id)
        name = guild.name if guild else str(ctx.i.guild_id)
        titles = {
            "classic": "Classic Pin:",
            "slow": "Slow Pin:",
            "embed": "Pin Embed:",
            "webhook": "Webhook Pin Embed:",
        }
        pages = []
        result = embed(f"-Active Pins in **{name}**-").set_footer("Anchor", icon=self.app.avatar)
        size = 0
        for kind, title in titles.items():
            for state in self.app.pins.states.values():
                if (state["guild_id"] != str(ctx.i.guild_id)
                    or state["id"] not in readable or kind not in state["pins"]):
                    continue
                value = f"Channel: <#{state['id']}>\n__Pinned Message:__```\n{state['pins'][kind]['text']}```"
                for offset in range(0, len(value), 1024):
                    chunk = value[offset : offset + 1024]
                    if len(result.fields) == 20 or size + len(chunk) > 4500:
                        pages.append(result)
                        result = embed(f"-Active Pins in **{name}**-").set_footer(
                            "Anchor", icon=self.app.avatar
                        )
                        size = 0
                    result.add_field(title, chunk)
                    size += len(chunk)
        if result.fields:
            pages.append(result)
        if not pages:
            await ctx.reply("No active pins in this server.", ephemeral=True)
            return
        await ctx.reply(embed=pages[0], ephemeral=True)
        for page in pages[1:]:
            await ctx.i.execute(embed=page, flags=hikari.MessageFlag.EPHEMERAL, **SAFE_MENTIONS)
