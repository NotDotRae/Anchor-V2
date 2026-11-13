import re

MAX_EXPIRY_SECONDS = 365 * 86400
DURATION = re.compile(r"([0-9]+)([smhdw])")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_expiry(value):
    value = value.strip().lower()
    if value == "off":
        return None
    parts = list(DURATION.finditer(value))
    if not parts or "".join(part.group() for part in parts) != value:
        raise ValueError("Use a duration such as `30m`, `2h`, `1d`, `1h30m`, or `off` to cancel expiry.")
    seconds = sum(int(part[1]) * UNITS[part[2]] for part in parts)
    if not 1 <= seconds <= MAX_EXPIRY_SECONDS:
        raise ValueError("Choose an expiry from 1 second to 365 days, or `off`.")
    return seconds
