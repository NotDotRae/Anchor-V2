import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def runtime():
    if sys.version_info < (3, 12):
        raise RuntimeError("Install Python 3.12 or newer.")
    manifest = (ROOT / "pyproject.toml").read_bytes()
    dependencies = tomllib.loads(manifest.decode())["project"]["dependencies"]
    digest = hashlib.sha256(json.dumps(dependencies).encode()).hexdigest()
    folder = ROOT / ".venv"
    python = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    stamp = folder / "dependencies.sha256"
    needs_venv = not python.is_file() or subprocess.run(
        [str(python), "-m", "pip", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode != 0
    if needs_venv:
        stamp.unlink(missing_ok=True)
        subprocess.run([sys.executable, "-m", "venv", str(folder)], check=True)
    if not stamp.exists() or stamp.read_text() != digest:
        subprocess.run([str(python), "-m", "pip", "install", *dependencies], check=True)
        stamp.write_text(digest)
    return python


def run(arguments):
    python = runtime()
    os.chdir(ROOT)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    if os.name == "nt":
        raise SystemExit(subprocess.call([str(python), "-B", "-m", "anchorbot", *arguments]))
    os.execv(str(python), [str(python), "-B", "-m", "anchorbot", *arguments])


@contextlib.contextmanager
def launch_lock():
    import fcntl

    descriptor = os.open(ROOT, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def matches(pid):
    try:
        directory = Path("/proc") / str(pid)
        if (directory / "cwd").resolve() != ROOT:
            return False
        arguments = (directory / "cmdline").read_bytes().split(b"\0")
        return any(
            arguments[i : i + 2] == [b"-m", module]
            for i in range(len(arguments) - 1)
            for module in (b"anchorbot", b"anchorbot.launcher")
        )
    except OSError:
        return False


def recorded_pid():
    try:
        value = (ROOT / "app.pid").read_text().strip()
    except FileNotFoundError:
        return None
    if not value.isdecimal() or int(value) <= 1:
        raise RuntimeError("app.pid does not contain a valid process ID.")
    return int(value)


def start(arguments):
    with launch_lock():
        pid = recorded_pid()
        if pid and matches(pid):
            print(f"AnchorBot is already running with PID {pid}.")
            return
        python = runtime()
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        with (ROOT / "app.log").open("ab", buffering=0) as output:
            process = subprocess.Popen(
                [str(python), "-B", "-m", "anchorbot.launcher", "run", *arguments],
                cwd=ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        (ROOT / "app.pid").write_text(str(process.pid) + "\n", encoding="ascii")
        time.sleep(0.3)
        if process.poll() is not None:
            (ROOT / "app.pid").unlink(missing_ok=True)
            raise RuntimeError("AnchorBot exited during startup. See app.log.")
        print(f"AnchorBot started with PID {process.pid}. Logs: {ROOT / 'app.log'}")


def stop():
    with launch_lock():
        pid = recorded_pid()
        if not pid or not matches(pid):
            (ROOT / "app.pid").unlink(missing_ok=True)
            print("AnchorBot is not running.")
            return
        descriptor = os.pidfd_open(pid)
        try:
            if not matches(pid):
                raise RuntimeError("The recorded process changed; refusing to signal it.")
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
            deadline = time.monotonic() + config.get("shutdown_timeout_seconds", 30) + 25
            while matches(pid) and time.monotonic() < deadline:
                time.sleep(0.2)
            if matches(pid):
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        finally:
            os.close(descriptor)
        (ROOT / "app.pid").unlink(missing_ok=True)
        print(f"Stopped AnchorBot PID {pid}.")


def deploy():
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    if not config.get("convex_deploy_key") or not config.get("convex_key"):
        raise RuntimeError("Set convex_deploy_key and convex_key in config.json first.")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    node = shutil.which("node")
    if not npm or not node:
        raise RuntimeError("Install Node.js and npm to deploy Convex.")
    subprocess.run([npm, "ci"], cwd=ROOT, check=True)
    environment = os.environ.copy()
    environment["CONVEX_DEPLOY_KEY"] = config["convex_deploy_key"]
    cli = [node, str(ROOT / "node_modules/convex/bin/main.js")]
    subprocess.run(
        [*cli, "env", "set", "ANCHORBOT_CONVEX_KEY"],
        input=config["convex_key"],
        text=True,
        cwd=ROOT,
        env=environment,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    subprocess.run([*cli, "deploy", "--yes"], cwd=ROOT, env=environment, check=True)


def main():
    mode, *arguments = sys.argv[1:] or ["run"]
    try:
        if mode == "run":
            run(arguments)
        elif mode == "start" and os.name != "nt":
            start(arguments)
        elif mode == "stop" and os.name != "nt":
            stop()
        elif mode == "deploy":
            deploy()
        else:
            raise RuntimeError("Unknown launcher mode.")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
