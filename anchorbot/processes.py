import sys
from pathlib import Path

import psutil


def process_identity(process):
    if sys.platform == "linux":
        root = Path(psutil.PROCFS_PATH)
        boot = (root / "sys/kernel/random/boot_id").read_text().strip()
        stat = (root / str(process.pid) / "stat").read_text()
        started = stat.rsplit(")", 1)[1].split()[19]
        return f"linux:{boot}:{process.pid}:{started}"
    return f"{sys.platform}:{process.pid}:{process.create_time()}"


def matching_process(row):
    try:
        candidate = psutil.Process(row["pid"])
        identity = row.get("process_identity")
        if identity is not None:
            if process_identity(candidate) == identity:
                return candidate
        elif candidate.create_time() == row.get("process_birth"):
            return candidate
    except (KeyError, IndexError, TypeError, ValueError, OSError, psutil.Error):
        pass
    return None


def reported_process(process, row):
    candidate = matching_process(row)
    if candidate is None:
        return None
    try:
        if candidate.pid == process.pid or any(parent.pid == process.pid for parent in candidate.parents()):
            return candidate
    except psutil.Error:
        pass
    return None


def kill_tree(process, identity=None):
    targets = {}
    roots = [identity]
    try:
        if process.poll() is None:
            roots.append(psutil.Process(process.pid))
    except psutil.NoSuchProcess:
        pass
    for root in roots:
        if root is None:
            continue
        try:
            if root.is_running():
                for child in root.children(recursive=True):
                    targets[child.pid] = child
                targets[root.pid] = root
        except psutil.NoSuchProcess:
            pass
    for target in targets.values():
        try:
            target.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(list(targets.values()), timeout=5)
