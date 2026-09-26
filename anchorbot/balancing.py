from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def shard_for(guild, count):
    return (int(guild) >> 22) % count


def balanced_plan(db, count, workers):

    weights = [1.0] * count
    for row in db.db.execute("SELECT * FROM guild_load"):
        weights[shard_for(row["guild"], count)] += (
            row["event_rate"] + 1000 * row["busy_rate"] + 5 * row["request_rate"]
        )
    bins = [[] for _ in range(min(count, workers))]
    totals = [0.0] * len(bins)
    for shard in sorted(range(count), key=lambda s: (-weights[s], s)):
        worker = min(range(len(bins)), key=lambda i: (totals[i], i))
        bins[worker].append(shard)
        totals[worker] += weights[shard]
    return [sorted(b) for b in bins], weights


def improvement(old, new, weights):
    def peak(plan):
        return max(sum(weights[s] for s in group) for group in plan)

    return (peak(old) - peak(new)) / max(peak(old), 1)


def next_restart(now: datetime, timezone: str, at: str):
    local = now.astimezone(ZoneInfo(timezone))
    hour, minute = map(int, at.split(":"))
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target
