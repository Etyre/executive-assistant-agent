"""On a server, the document folders (`[[document_roots]]`) are on Eli's Mac. Each root marked `from_mac = true`
is kept as a copy on the server: the bridge on the Mac lists the Markdown files (GET /documents) and hands out
any that changed. Run from the scheduler every two minutes; when the Mac is asleep the copy simply stays as it
was, which is also the newest version there is (the files only change on the Mac).

Modified times are copied exactly (nanoseconds), so the "document edited" watch sees the same files it saw on
the Mac and reports only real edits. A file deleted on the Mac is deleted here, but only when the Mac answered
with a listing for that root: an unreachable Mac or a missing folder never empties the copy.
"""
from __future__ import annotations

import logging
import os
import pathlib

from . import config
from .connectors.http import ServiceRouter, SourceError

log = logging.getLogger(__name__)


def sync(settings: config.Settings | None = None, router: ServiceRouter | None = None) -> dict[str, int]:
    """Bring every `from_mac` root up to date. Returns {"fetched": n, "removed": n}; zeros when the Mac is away."""
    s = settings or config.load()
    roots = {r.name: pathlib.Path(r.path) for r in s.document_roots if r.from_mac}
    done = {"fetched": 0, "removed": 0}
    router = router or ServiceRouter(s)
    if not roots or not router.bridge:
        return done
    try:
        listing = router.mac("GET", "/documents")
    except SourceError as e:
        log.debug("document mirror: %s", str(e)[:160])
        return done
    for root in listing.get("roots", []):
        folder = roots.get(root.get("name"))
        if folder is None or root.get("missing"):
            continue
        folder.mkdir(parents=True, exist_ok=True)
        want = set()
        for f in root.get("files", []):
            rel = pathlib.PurePosixPath(f["path"])
            if rel.is_absolute() or ".." in rel.parts or rel.suffix != ".md":
                continue
            want.add(rel.as_posix())
            dest = folder / rel
            if dest.exists() and dest.stat().st_mtime_ns == f["mtime_ns"] and dest.stat().st_size == f["size"]:
                continue
            try:
                body = router.mac("GET", f"/documents/{root['name']}/file", raw=True, params={"path": rel.as_posix()})
            except SourceError as e:
                log.warning("document mirror: %s/%s not fetched (%s)", root["name"], rel, str(e)[:160])
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(body)
            os.utime(tmp, ns=(f["mtime_ns"], f["mtime_ns"]))
            tmp.replace(dest)
            done["fetched"] += 1
        for p in folder.rglob("*.md"):
            if p.relative_to(folder).as_posix() not in want:
                p.unlink()
                done["removed"] += 1
    if done["fetched"] or done["removed"]:
        log.info("document mirror: %d fetched, %d removed", done["fetched"], done["removed"])
    return done
