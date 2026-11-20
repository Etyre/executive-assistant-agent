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


def test_restore_is_the_opposite_of_archive(tmp_path):
    from ea.tools.registry import ToolRegistry
    from ea.tools import memory_tools
    s = MemoryStore(tmp_path)
    s.write("Carpet", "the carpet guy comes Tuesday", name="carpet")
    reg = ToolRegistry(); memory_tools.register(reg, s, lambda: "test")
    assert reg.dispatch("memory_archive", {"name": "carpet"})[0] == "archived" and s.search("carpet guy") == []
    import json
    listed = json.loads(reg.dispatch("memory_archived", {})[0])
    assert [a["name"] for a in listed] == ["carpet"] and listed[0]["title"] == "Carpet"
    assert json.loads(reg.dispatch("memory_restore", {"name": "carpet"})[0]) == {"restored": "carpet", "title": "Carpet"}
    assert s.read("carpet")["body"] == "the carpet guy comes Tuesday" and s.search("carpet guy") and s.archived() == []
    assert "no archived memory" in reg.dispatch("memory_restore", {"name": "carpet"})[0]
    # never overwrites an active memory of the same name
    s.archive("carpet"); s.write("Carpet", "a newer note", name="carpet")
    assert "already exists" in reg.dispatch("memory_restore", {"name": "carpet"})[0]
    assert s.read("carpet")["body"] == "a newer note" and len(s.archived()) == 1


def test_a_name_cannot_reach_outside_the_memory_folder(tmp_path):
    import pytest
    (tmp_path / "outside.md").write_text("private")
    s = MemoryStore(tmp_path / "memory")
    for bad in ("../outside", "a/b", ".hidden", "", "..\\x"):
        with pytest.raises(ValueError):
            s.read(bad)
        if bad:                                  # an empty name means "use the title's slug", which is safe
            with pytest.raises(ValueError):
                s.write("t", "b", name=bad)
    assert not (tmp_path / "x.md").exists() and (tmp_path / "outside.md").read_text() == "private"


def test_archiving_the_same_name_twice_in_a_day_keeps_both(tmp_path):
    s = MemoryStore(tmp_path)
    s.write("Note", "first body", name="note"); s.archive("note")
    s.write("Note", "second body", name="note"); s.archive("note")
    bodies = sorted(p.read_text().split("---\n")[-1].strip() for p in (tmp_path / "_archive").glob("note.*.md"))
    assert bodies == ["first body", "second body"]
    assert len([a for a in s.archived() if a["name"] == "note"]) == 2
    assert s.restore("note")["body"] == "second body"          # the latest copy comes back


def test_many_threads_at_once_do_not_break_the_index(tmp_path):
    import threading
    s = MemoryStore(tmp_path)
    s.write("Alpha", "alpha body words", name="alpha")
    errors = []

    def work(i):
        try:
            for n in range(60):
                s.search("alpha"); s.update("alpha", append=f"line {i}-{n}") if i == 0 else s.digest()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
    ts = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert errors == []
