#!/opt/homebrew/bin/python3
"""Keeps /Applications/Atlas.app at the latest commit, without interrupting the agent.

Runs forever under launchd (com.elityre.atlas-app-deploy, see install-auto-deploy.sh). Every POLL seconds it
compares the committed Mac app source (HEAD) with what it last installed. When they differ it exports that commit
to a clean folder, builds it there, waits until the agent is not in the middle of a turn (GET /health -> "busy"),
then replaces /Applications/Atlas.app and relaunches it (build.sh does the wait and the install).

Only committed work is deployed, so a half-edited tree never reaches the installed app. A failed build is logged
once and not retried until the source changes.

    auto_deploy.py            the loop
    auto_deploy.py once       one check by hand (the end-to-end test)
    auto_deploy.py status     committed vs installed

State and the build folder: ~/Library/Application Support/AtlasAppDeploy/    Log: ~/Library/Logs/atlas-app-deploy.log
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SUPPORT = Path.home() / "Library/Application Support/AtlasAppDeploy"
STATE = SUPPORT / "state.json"
CHECKOUT = SUPPORT / "checkout"
POLL = int(os.environ.get("ATLAS_DEPLOY_POLL_SECONDS", "30"))
HEALTH_URL = os.environ.get("ATLAS_HEALTH_URL", "http://127.0.0.1:8800/health")
# What the Mac app is built from. A change to anything else in the repo does not rebuild it.
SOURCES = ("apps/Atlas/Atlas", "apps/Atlas/project.yml", "apps/Atlas/build.sh")


def log(msg: str) -> None:
    print(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True).stdout.strip()


def wanted() -> str:
    """One id for the committed app source: the git object ids of SOURCES at HEAD."""
    return "-".join(git("rev-parse", f"HEAD:{p}")[:12] for p in SOURCES)


def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save(state: dict) -> None:
    SUPPORT.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)


def export() -> Path:
    """HEAD's app source in CHECKOUT, replacing the last export but keeping build/ so the next build is incremental."""
    app = CHECKOUT / "apps/Atlas"
    if app.exists():
        for child in app.iterdir():
            if child.name != "build":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    CHECKOUT.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(REPO), "archive", "HEAD", "apps/Atlas"], check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(CHECKOUT)], input=archive, check=True)
    return app


def build_and_install(app: Path) -> tuple[bool, str]:
    env = {**os.environ, "ATLAS_WAIT_IDLE": "1", "ATLAS_HEALTH_URL": HEALTH_URL}
    r = subprocess.run(["bash", "build.sh"], cwd=app, env=env, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def tick(state: dict, build=build_and_install) -> dict:
    want = wanted()
    if want in (state.get("deployed"), state.get("failed")):
        return state
    commit = git("log", "-1", "--format=%h %s")
    log(f"app source changed ({want}); building {commit}")
    ok, out = build(export())
    if ok:
        log(f"installed and relaunched /Applications/Atlas.app at {commit}")
        state = {"deployed": want, "commit": commit, "at": dt.datetime.now().isoformat(timespec="seconds")}
    else:
        log("build or install FAILED; not retrying until the app source changes. Output:\n" + out[-3000:])
        state = {**state, "failed": want}
    save(state)
    return state


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "loop"
    state = load()
    if cmd == "status":
        want = wanted()
        print(f"committed: {want}\ninstalled: {state.get('deployed', '(nothing yet)')}  {state.get('commit', '')}  {state.get('at', '')}")
        print("up to date" if want == state.get("deployed") else "failed; waiting for a new commit" if want == state.get("failed") else "will deploy at the next check")
        return
    if cmd == "once":
        state.pop("failed", None)                      # by hand: try again even after a failure
        tick(state)
        return
    log(f"watching {REPO} every {POLL} s")
    while True:
        try:
            state = tick(state)
        except Exception as e:  # noqa: BLE001 - the loop must survive a bad tick (git mid-rebase, disk full)
            log(f"check failed: {e!r}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
