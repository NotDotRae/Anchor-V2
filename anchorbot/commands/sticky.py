from urllib.parse import urlparse

import hikari

from ..style import embed, webhook_parts


class StickyCommands:
    def __init__(self, app):
        self.app = app
        router = app.router
        for name, description in {
            "stick": "Create a channel pin message.",
            "stickslow": "Create a slower channel pin.",
            "stickembed": "Create a pinned embed.",
            "stickwebhook": "Create a pinned webhook embed.",
        }.items():
            router.add(
                name,
                description,
                self.start,
                (
                    "message",
                    {"stickembed": "Embed pin text.", "stickwebhook": "Webhook pin text."}.get(
                        name, "Pin text."
                    ),
                    hikari.OptionType.STRING,
                    True,
                ),
                maximum=2000 if name in {"stick", "stickslow"} else 4000,
            )
        for name in ("stickstop", "unstick", "webhookstop"):
            router.add(
                name,
                "Stop the webhook pin in this channel."
                if name == "webhookstop"
                else "Stop pins in this channel.",
                self.stop,
            )
        for name in ("getstick", "getsticks", "getstickies"):
            router.add(name, "List active pins in this server.", self.list)
        descriptions = {
            "setimage": "Set the small image for pin embeds.",
            "setbigimage": "Set the large image for pin embeds.",
            "setwebhook": "Set the channel webhook URL.",
            "removeimage": "Remove the small image for pin embeds.",
            "removebigimage": "Remove the large image for pin embeds.",
            "getimage": "Show the current image settings.",
            "getbigimage": "Show the current large image setting.",
        }
        for name, description in descriptions.items():
            router.add(
                name,
                description,
                self.settings,
                ("url", "URL.", hikari.OptionType.STRING, True) if name.startswith("set") else None,
                maximum=1000,
            )

    async def start(self, ctx):
        if not await ctx.manage():
            return
        kinds = {"stick": "classic", "stickslow": "slow", "stickembed": "embed", "stickwebhook": "webhook"}
        kind = kinds[ctx.i.command_name]
        text = ctx.value("message")
        if not text:
            raise ValueError(
                "Please provide pin embed text." if kind == "embed" else "Please provide pin text."
            )
        if kind == "webhook" and not self.app.pins.states.get(str(ctx.i.channel_id), {}).get("webhook_url"):
            raise ValueError("Set the channel webhook URL first with `/setwebhook`.")
        await ctx.defer(ephemeral=True)
        await self.app.pins.start(ctx.i.channel_id, ctx.i.guild_id, kind, text)
        label = {"classic": "Pin", "slow": "Pin", "embed": "Pin embed", "webhook": "Webhook pin"}[kind]
        await ctx.reply(f"{label} started in <#{ctx.i.channel_id}>.")

    async def stop(self, ctx):
        if not await ctx.manage():
            return
        await ctx.defer(ephemeral=True)
        webhook = ctx.i.command_name == "webhookstop"
        await self.app.pins.stop(ctx.i.channel_id, {"webhook"} if webhook else None)
        await ctx.reply(
            "Webhook pin stopped in this channel." if webhook else f"Pins stopped in <#{ctx.i.channel_id}>."
        )

    async def settings(self, ctx):
        name = ctx.i.command_name
        state = self.app.pins.states.get(str(ctx.i.channel_id), {})
        if name.startswith("get"):
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
        if not await ctx.manage():
            return
        await ctx.defer(ephemeral=True)
        async with self.app.pins.locks[str(ctx.i.channel_id)]:
            state = self.app.pins.state(ctx.i.channel_id, ctx.i.guild_id)
            key = "webhook_url" if name == "setwebhook" else "big_image" if "big" in name else "image"
            if name.startswith("set"):
                url = ctx.value("url")
                if key == "webhook_url":
                    hook, token = webhook_parts(url)
                    webhook = await self.app.bot.rest.fetch_webhook(hook, token=token)
                    if webhook.channel_id != ctx.i.channel_id:
                        raise ValueError("The webhook must belong to this channel.")
                    if state.get("webhook_url") != url and "webhook" in state["pins"]:
                        raise ValueError(
                            "Stop the webhook pin with `/webhookstop` before changing its webhook."
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
            message = f"{label} image {'set' if name.startswith('set') else 'removed'} for pin embeds."
        await ctx.reply(message, ephemeral=True)

    async def list(self, ctx):
        if not await ctx.manage():
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
        result = embed(f"-Active Pins in **{name}**-").set_footer("AnchorBot", icon=self.app.avatar)
        size = 0
        for kind, title in titles.items():
            for state in self.app.pins.states.values():
                if state["guild_id"] != str(ctx.i.guild_id) or kind not in state["pins"]:
                    continue
                value = f"Channel: <#{state['id']}>\n__Pinned Message:__```\n{state['pins'][kind]['text']}```"
                for offset in range(0, len(value), 1024):
                    chunk = value[offset : offset + 1024]
                    if len(result.fields) == 20 or size + len(chunk) > 4500:
                        pages.append(result)
                        result = embed(f"-Active Pins in **{name}**-").set_footer(
                            "AnchorBot", icon=self.app.avatar
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
            await ctx.i.execute(embed=page, flags=hikari.MessageFlag.EPHEMERAL)
