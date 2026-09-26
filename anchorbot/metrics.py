import os
import time
from collections import defaultdict

import psutil

from .processes import process_identity


class Metrics:
    def __init__(self):
        self.started = self.last = time.time()
        self.loads = defaultdict(lambda: [0, 0.0, 0])
        self.total_requests = 0
        self.total_events = 0
        self.process = psutil.Process()
        self.process_identity = process_identity(self.process)
        self.process.cpu_percent()

    def event(self, guild, cpu=0):
        self.total_events += 1
        self.loads[str(guild)][0] += 1
        self.loads[str(guild)][1] += cpu

    def request(self, guild):
        self.total_requests += 1
        if guild:
            self.loads[str(guild)][2] += 1

    def snapshot(self, guilds):
        now = time.time()
        interval = max(now - self.last, 0.01)
        loads = {str(g): (*self.loads[str(g)], interval) for g in guilds}
        self.loads.clear()
        self.last = now
        return {
            "pid": os.getpid(),
            "process_birth": self.process.create_time(),
            "process_identity": self.process_identity,
            "started": self.started,
            "rss": self.process.memory_info().rss,
            "cpu_percent": self.process.cpu_percent(),
            "events": self.total_events,
            "requests": self.total_requests,
        }, loads
