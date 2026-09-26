import math

import hikari

_installed = False


async def retry_delay(response):
    values = [response.headers.get("Retry-After")]
    if response.content_type == "application/json":
        try:
            body = await response.json()
            if isinstance(body, dict):
                values.append(body.get("retry_after"))
        except (ValueError, TypeError):
            pass
    for candidates in (values, [response.headers.get("X-RateLimit-Reset-After")]):
        delays = []
        for value in candidates:
            try:
                delay = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(delay) and delay >= 0:
                delays.append(delay)
        if delays:
            return max(delays)
    return 0


def install():
    global _installed
    if _installed:
        return
    if hikari.__version__ != "2.6.0":
        raise RuntimeError("The Discord rate-limit adapter requires Hikari 2.6.0")
    original = hikari.impl.RESTClientImpl._parse_ratelimits

    async def parse(self, compiled_route, authentication, response):
        delay = await original(self, compiled_route, authentication, response)
        if response.status == 429 and delay is not None:
            delay = max(delay, await retry_delay(response))
        return delay

    hikari.impl.RESTClientImpl._parse_ratelimits = parse
    _installed = True
