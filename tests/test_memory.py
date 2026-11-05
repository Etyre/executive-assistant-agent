import datetime as dt

from ea.memory.store import MemoryStore, slugify

TODAY = dt.date.today().isoformat()


def test_write_records_source_date_and_edit_date(tmp_path):
    s = MemoryStore(tmp_path)
    m = s.write("Morning routine", "Up at 7, cold shower.", ["habit"], source_date="2026-10-20", channel="telegram")
    assert m["name"] == "morning-routine"
    assert m["created"] == "2026-10-20"
    assert m["sources"] == ["2026-10-20 telegram"]
    assert m["edits"] == [TODAY]
    text = (tmp_path / "morning-routine.md").read_text()
    assert text.startswith("---\ntitle: Morning routine\ncreated: '2026-10-20'") or "created: 2026-10-20" in text
    assert "Up at 7" in text


def test_update_keeps_history_and_appends(tmp_path):
    s = MemoryStore(tmp_path)
    s.write("Bedtime", "Target 23:30.", source_date="2026-10-01", channel="voice")
    m = s.update("bedtime", append="Moved to 23:00 after HRV dip.", source_date="2026-10-15", channel="telegram")
    assert m["sources"] == ["2026-10-01 voice", "2026-10-15 telegram"]
    assert m["edits"][-1] == TODAY
    assert "Target 23:30." in m["body"] and "Moved to 23:00" in m["body"]
    # a second edit the same day does not duplicate the edit date
    m = s.update("bedtime", append="Also no screens.", source_date="2026-10-15", channel="telegram")
    assert m["edits"].count(TODAY) == 1
    assert len(m["sources"]) == 2


def test_write_existing_name_becomes_update(tmp_path):
    s = MemoryStore(tmp_path)
    s.write("Project X", "v1", source_date="2026-09-01", channel="cli")
    m = s.write("Project X", "v2", source_date="2026-09-09", channel="cli")
    assert m["body"] == "v2" and m["created"] == "2026-09-01"
    assert m["sources"] == ["2026-09-01 cli", "2026-09-09 cli"]


def test_search_and_digest_and_reindex(tmp_path):
    s = MemoryStore(tmp_path)
    s.write("Exercise cadence", "Squats at least every 7 days, cardio intervals every 4.", ["habit", "exercise"])
    s.write("Dentist", "Appointment with Dr. Lee on the 12th.", ["commitment"])
    hits = s.search("squats cadence")
    assert hits and hits[0]["name"] == "exercise-cadence"
    assert s.search("dentist appointment")[0]["name"] == "dentist"
    assert s.search("nothing matches this at all") == []
    assert s.search('bad "fts( syntax') is not None          # falls back, does not raise
    d = s.digest()
    assert "exercise-cadence" in d and "[habit, exercise]" in d
    # files are the truth: a hand-written file is picked up by reindex
    (tmp_path / "handwritten.md").write_text("---\ntitle: Hand written\ntags: [x]\n---\nWritten by a human about kayaks.\n")
    s2 = MemoryStore(tmp_path)
    assert s2.search("kayaks")[0]["name"] == "handwritten"


def test_archive(tmp_path):
    s = MemoryStore(tmp_path)
    s.write("Temp", "gone soon")
    assert s.archive("temp") and s.read("temp") is None and s.search("gone soon") == []
    assert list((tmp_path / "_archive").glob("temp.*.md"))


def test_slugify():
    assert slugify("Hello, World! 2026") == "hello-world-2026"
    assert slugify("!!!") == "memory"
