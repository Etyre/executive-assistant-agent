"""Otter transcripts: the Logseq archive parser, the merged SQLite/FTS index, the live API shaping (through
a fake transport), lazy sync behaviour, and the tools as the agent sees them."""
import json
import pathlib

import httpx
import pytest

from ea import config
from ea.connectors.http import SourceError
from ea.connectors.otter import Otter, OtterClient, parse_archive_page, shape_speech, transcript_text
from zoneinfo import ZoneInfo

PAGE = """- List [This block is functional! Don't remove it]:
  id:: a3456aa6-5f18-5e64-82f2-373a6c82b694
\t- [[January 5th, 2024]] #[[Walking Journal]]
\t  id:: 767f0e29-87dc-55e5-bb0c-bfa2ab1704f1
\t\t- [[Automated transcirpt from otter]]
\t\t  id:: a5417269-3bb2-54c9-b2d8-fbffa17a3e08
\t\t\t- Recroding Metadata
\t\t\t  id:: 6f0e4199-4243-50c0-bb28-e20a06fbf527
\t\t\t\t- Date:: [[January 5th, 2024]]
\t\t\t\t  id:: 550cb3d7-6711-56f4-a3e3-bc9489568fec
\t\t\t\t- start-time:: 17:16
\t\t\t\t  id:: 47906db6-60e1-5edb-b85f-d93b6f01fce5
\t\t\t\t- otter-link:: https://otter.ai/u/kczuqdx8Gd65SjFV_w793AgqKss
\t\t\t\t  id:: 7a8cbf5f-0c6f-5247-9a5d-e0135f1eae73
\t\t\t- TODO [[Transcricpt]]
\t\t\t  id:: 52d69a3e-e1d8-5d9f-807b-4b3b8bdc8657
\t\t\t\t- Eli Tyre:
\t\t\t\t  id:: ff779d44-a79a-5fde-b26e-588b57c6bda2
\t\t\t\t  I want to think about the zebra project today.
\t\t\t\t  
\t\t\t\t  Unknown:
\t\t\t\t  Hear you
\t- DONE [[September 21st, 2024]]
\t  id:: 2db771b7-8986-5fa3-be27-08ca31bcf1e9
\t\t- [[Automated transcript from otter]] #[[transcribed verbal notes]] (Startup idea)
\t\t  id:: 4b1f9956-5127-5692-a50d-eafe776d55ec
\t\t\t- Recording Metadata
\t\t\t  id:: 60b135d7-411b-5490-b956-106f00b9d532
\t\t\t\t- Date:: [[September 21st, 2024]]
\t\t\t\t- start-time:: 12:08
\t\t\t\t- otter-link:: https://otter.ai/u/unr4EcxbmLCULOVVsmIP6OxtjNE
\t\t\t\t- Source::
\t\t\t- DONE [[Transcript]]
\t\t\t\t- Eli Tyre:
\t\t\t\t  id:: faba289b-c7f9-5673-af10-2a4ac18e0b2a
\t\t\t\t  the one hundred dollar startup, giraffe edition.
"""


def test_archive_parser_reads_both_spellings_and_titles():
    recs = parse_archive_page(PAGE, "walking journal")
    assert [r["otid"] for r in recs] == ["kczuqdx8Gd65SjFV_w793AgqKss", "unr4EcxbmLCULOVVsmIP6OxtjNE"]
    a, b = recs
    assert a["date"] == "2024-01-05" and a["start_time"] == "17:16" and a["title"] == ""
    assert "zebra project" in a["text"] and "Unknown:\nHear you" in a["text"]
    assert "id::" not in a["text"]
    assert b["date"] == "2024-09-21" and b["title"] == "Startup idea" and "giraffe" in b["text"]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.delenv("OTTER_EMAIL", raising=False)
    monkeypatch.delenv("OTTER_PASSWORD", raising=False)
    config.load.cache_clear()
    s = config.load()
    graph = tmp_path / "graph" / "pages"
    graph.mkdir(parents=True)
    (graph / "Otter auto-import walking journal transcripts.md").write_text(PAGE)
    s.sources.otter_logseq_graph = str(tmp_path / "graph")
    s.timezone = "UTC"
    return s


def test_index_from_archive_search_recent_get(settings):
    ot = Otter(settings)
    assert ot.enabled() and not ot.live_enabled()
    assert ot.reindex_archive() == 2
    assert ot.reindex_archive() == 0                      # unchanged pages are not re-parsed
    # the same recording on the "(no folder)" page too: the folder page's label must win
    (tmp_path_pages := pathlib.Path(settings.sources.otter_logseq_graph) / "pages" / "Otter auto-imported transcripts (no folder).md").write_text(PAGE)
    assert ot.reindex_archive() == 4
    assert ot.get("kczuqdx8Gd65SjFV_w793AgqKss")["folder"] == "walking journal"
    assert ot.reindex_archive(force=True) == 4
    hits = ot.search("zebra")
    assert len(hits) == 1 and hits[0]["otid"] == "kczuqdx8Gd65SjFV_w793AgqKss" and "«zebra»" in hits[0]["snippet"]
    assert ot.search("zebra OR giraffe", limit=5).__len__() == 2
    assert ot.search("giraffe edition's (weird")               # invalid FTS syntax falls back, does not raise
    rec = ot.get("unr4EcxbmLCULOVVsmIP6OxtjNE")
    assert rec["title"] == "Startup idea" and "giraffe" in rec["text"] and rec["truncated"] is False
    assert ot.get("nope") is None
    recent = ot.recent(days=1, start_date="2024-09-21")
    assert [r["otid"] for r in recent] == ["unr4EcxbmLCULOVVsmIP6OxtjNE"] and recent[0]["preview"].startswith("Eli Tyre")
    assert ot.recent(days=400, start_date="2024-01-01", folder="walking journal").__len__() == 2
    assert ot.recent(days=400, start_date="2024-01-01", folder="audio notes") == []
    warn = ot.ensure_fresh()
    assert "not configured" in warn
    st = ot.status()
    assert st["recordings"] == 2 and st["first_date"] == "2024-01-05" and st["last_date"] == "2024-09-21"


def test_get_pages_long_text(settings):
    ot = Otter(settings)
    ot._upsert({"otid": "long", "date": "2026-01-01", "start_time": "09:00", "title": "", "folder": "", "link": "",
                "text": "word " * 2000, "duration_s": None, "source": "logseq"})
    first = ot.get("long", max_chars=1000)
    assert first["truncated"] and first["next_offset"] == 1000 and first["total_chars"] == 10000
    rest = ot.get("long", offset=first["next_offset"], max_chars=50000)
    assert rest["truncated"] is False and len(first["text"]) + len(rest["text"]) == 10000


SPEECHES = [
    {"otid": "live1", "title": "Walk about the project", "created_at": 1767225600, "process_finished": True,
     "folder": {"id": 1072467, "name": "Walking journal"}, "duration": 600},
    {"otid": "kczuqdx8Gd65SjFV_w793AgqKss", "title": "Untitled", "created_at": 1704500000, "process_finished": True},
    {"otid": "pending", "title": "Still uploading", "created_at": 1767225700, "process_finished": False},
]
FULL = {
    "live1": {"otid": "live1", "summary": "A walk.", "transcripts": [
        {"transcript": "Hello there.", "start_offset": 0, "speaker_model_label": "Eli Tyre"},
        {"transcript": "Second sentence.", "start_offset": 3000000, "speaker_model_label": "Eli Tyre"},
        {"transcript": "Hi.", "start_offset": 7, "speaker": {"speaker_name": "Guest"}}]},
    "kczuqdx8Gd65SjFV_w793AgqKss": {"otid": "kczuqdx8Gd65SjFV_w793AgqKss", "transcripts": [
        {"transcript": "The real zebra text, from Otter.", "start_offset": 0, "speaker_model_label": "Eli Tyre"}]},
}


class FakeOtterAPI:
    def __init__(self, fail_login=False):
        self.calls = []
        self.fail_login = fail_login

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/login"):
            if self.fail_login:
                return httpx.Response(401, text="bad credentials")
            assert request.headers.get("authorization", "").startswith("Basic ")
            return httpx.Response(200, json={"userid": 42})
        if request.url.path.endswith("/speeches"):
            return httpx.Response(200, json={"speeches": SPEECHES, "end_of_list": True})
        if request.url.path.endswith("/speech"):
            return httpx.Response(200, json={"speech": FULL[request.url.params["otid"]]})
        return httpx.Response(404)


def test_transcript_text_merges_speakers_and_handles_ms_offsets():
    text = transcript_text(FULL["live1"])
    assert text.split("\n\n") == ["[00:00:00] Eli Tyre: Hello there. Second sentence.", "[00:00:07] Guest: Hi."]
    rec = shape_speech(SPEECHES[0], FULL["live1"], ZoneInfo("UTC"))
    assert rec["date"] == "2026-01-01" and rec["start_time"] == "00:00" and rec["folder"] == "Walking journal"
    assert rec["duration_s"] == 600 and rec["summary"] == "A walk." and rec["source"] == "otter"


def test_live_sync_merges_with_archive_and_is_lazy(settings, monkeypatch):
    monkeypatch.setenv("OTTER_EMAIL", "eli@example.com")
    monkeypatch.setenv("OTTER_PASSWORD", "pw")
    api = FakeOtterAPI()
    client = OtterClient("eli@example.com", "pw", transport=httpx.MockTransport(api))
    clock = {"t": 1000.0}
    ot = Otter(settings, client=client, now=lambda: clock["t"])
    assert ot.live_enabled()
    assert ot.ensure_fresh() is None                  # archive indexed + synced, no warning
    assert ot.count() == 3                             # 2 archive, 1 new live; the overlap is one row
    rec = ot.get("kczuqdx8Gd65SjFV_w793AgqKss")
    assert rec["source"] == "otter" and "from Otter" in rec["text"]   # live copy replaced the archive copy
    assert ot.search("zebra")[0]["source"] == "otter"
    assert ot.get("pending") is None
    assert ot.recent(days=1, start_date="2026-01-01")[0]["minutes"] == 10
    n_calls = len(api.calls)
    ot.ensure_fresh()                                  # inside sync window: no network
    assert len(api.calls) == n_calls
    clock["t"] += ot.sync_seconds + 1
    ot.ensure_fresh()
    assert len(api.calls) > n_calls
    assert [c for c in api.calls if c[0].endswith("/speech")].__len__() == 2   # already-indexed live rows are not refetched
    assert ot.reindex_archive(force=True) == 1         # archive never overwrites a live row
    assert ot.get("kczuqdx8Gd65SjFV_w793AgqKss")["source"] == "otter"
    r = ot.sync()
    assert r == {"listed": 3, "added": 0, "already_had": 2, "still_processing": 1}
    assert ot.status()["from_otter_live"] == 2


def test_failed_login_becomes_warning_and_is_not_retried_every_read(settings, monkeypatch):
    monkeypatch.setenv("OTTER_EMAIL", "eli@example.com")
    monkeypatch.setenv("OTTER_PASSWORD", "wrong")
    api = FakeOtterAPI(fail_login=True)
    client = OtterClient("eli@example.com", "wrong", transport=httpx.MockTransport(api))
    ot = Otter(settings, client=client, now=lambda: 5000.0)
    warn = ot.ensure_fresh()
    assert "Otter sync failed" in warn and "401" in warn
    assert ot.search("zebra")                          # archive still answers
    n = len(api.calls)
    ot.ensure_fresh()
    assert len(api.calls) == n
    with pytest.raises(SourceError):
        ot.sync()


def test_tools_registered_and_dispatch(settings, monkeypatch):
    from ea.agent.core import Agent
    from tests.test_tools_and_agent import FakeRouter, FakeTracker, ScriptedProvider
    a = Agent(settings, provider=ScriptedProvider([]), router=FakeRouter(FakeTracker().handler))
    for name in ("otter_recent", "otter_search", "otter_transcript", "otter_status"):
        assert name in a.tools
    out, err = a.tools.dispatch("otter_search", {"query": "zebra"})
    assert not err
    data = json.loads(out)
    assert "warning" in data and data["results"][0]["otid"] == "kczuqdx8Gd65SjFV_w793AgqKss"
    out, err = a.tools.dispatch("otter_transcript", {"otid": "kczuqdx8Gd65SjFV_w793AgqKss"})
    assert not err and "zebra project" in out
    out, err = a.tools.dispatch("otter_transcript", {"otid": "missing"})
    assert not err and "no recording" in out
    out, err = a.tools.dispatch("otter_recent", {"days": 400, "start_date": "2024-01-01"})
    assert not err and len(json.loads(out)["results"]) == 2
    out, err = a.tools.dispatch("otter_status", {})
    assert not err and json.loads(out)["recordings"] == 2
