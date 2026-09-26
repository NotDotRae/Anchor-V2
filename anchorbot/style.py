import re

import hikari

PRIMARY = 0x2F88E6
SILENT = hikari.MessageFlag.SUPPRESS_NOTIFICATIONS
SAFE_MENTIONS = {"mentions_everyone": False, "user_mentions": False, "role_mentions": False}


def clean(text):
    return re.sub(r" {2,}", " ", re.sub(r"<a?:[A-Za-z0-9_]{2,32}:\d{17,20}>", "", text)).strip()


def embed(title=None, description=None):
    return hikari.Embed(title=title, description=description, color=PRIMARY)


def pin_embed(state, text):
    result = embed(description=text)
    if state.get("image"):
        result.set_thumbnail(state["image"])
    if state.get("big_image"):
        result.set_image(state["big_image"])
    return result


def uptime(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600} Hours, {(seconds // 60) % 60} Min, {seconds % 60} Seconds"


def webhook_parts(url):
    match = re.fullmatch(
        r"https://(?:discord(?:app)?\.com|canary\.discord\.com|ptb\.discord\.com)"
        r"/api(?:/v\d+)?/webhooks/(\d{17,20})/([A-Za-z0-9._-]+)",
        url,
    )
    if not match:
        raise ValueError("Please provide a valid Discord webhook URL.")
    return int(match[1]), match[2]
