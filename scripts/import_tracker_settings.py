#!/usr/bin/env python3
"""Copy the exercise export URL and the Google Calendar feeds from the Project Tracker's config.toml into this
agent's data/.env, without printing them. Run it yourself:  .venv/bin/python scripts/import_tracker_settings.py
Safe to re-run; it only fills values that are empty in data/.env."""
import pathlib
import re
import sys
import tomllib

TRACKER = pathlib.Path("~/Documents/agent-coding/project-tracker-and-organizer/config.toml").expanduser()
ENV = pathlib.Path(__file__).resolve().parent.parent / "data" / ".env"

if not TRACKER.exists():
    sys.exit(f"tracker config not found at {TRACKER}")
cfg = tomllib.loads(TRACKER.read_text())


def find(key: str):
    for section in cfg.values():
        if isinstance(section, dict) and section.get(key):
            return section[key]
    return cfg.get(key)


values = {
    "EXERCISE_EXPORT_URL": find("exercise_script_url") or find("exercise_csv") or "",
    "CALENDAR_ICS_URLS": ",".join(find("ics_urls") or []) if isinstance(find("ics_urls"), list) else (find("ics_urls") or ""),
}
text = ENV.read_text() if ENV.exists() else ""
changed = []
for k, v in values.items():
    if not v:
        print(f"{k}: nothing in the tracker config; skipped")
        continue
    m = re.search(rf"^{k}=(.*)$", text, re.M)
    current = (m.group(1).split("#")[0].strip() if m else "")
    if current:
        print(f"{k}: already set in data/.env; left as is")
        continue
    line = f"{k}={v}"
    text = re.sub(rf"^{k}=.*$", line, text, flags=re.M) if m else text.rstrip("\n") + f"\n{line}\n"
    changed.append(k)
ENV.write_text(text)
print("filled:", ", ".join(changed) or "nothing", "-> restart the server (pkill -f 'ea serve'; python -m ea serve, or launchd) and run: python -m ea check")
