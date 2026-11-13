"""tracker_checklist compact mode: the quarter's date columns boiled down to today, last met, misses and colour."""
import datetime as dt
import json

from ea.connectors.project_tracker import compact_checklist


def _sheet():
    def row(item, values, n=2, green=True, checkable=True, computed=False, **extra):
        fields = {"steps to Yellow": n, "Value for green": green, "G": item, **extra}
        fields.update(values)
        return {"item": item, "done": False, "checkable": checkable, "computed": computed, "fields": fields}
    d = lambda i: (dt.date(2026, 11, 1) + dt.timedelta(days=i)).isoformat()
    return {"source": "apps_script", "writable": True, "items": [
        row("Woke up without alarm", {d(i): (i == 7) for i in range(0, 30)}),                        # met on 11-08 only
        row("Morning routine", {d(i): True for i in range(0, 30)}, n=1, **{"Covered when overwelmed": "*"}),
        row("No late screens", {d(i): (i < 5) for i in range(0, 30)}, n=1, green=False),            # green when False
        row("Made something", {d(i): False for i in range(0, 30)}, n=None, checkable=False, computed=True, dimmed=1),
    ]}


def test_compact_rows_have_colour_runway_and_are_small():
    out = compact_checklist(_sheet(), dt.date(2026, 11, 12))
    rows = {r["item"]: r for r in out["rows"]}
    woke = rows["Woke up without alarm"]
    assert woke["steps_to_yellow"] == 2 and woke["last_true"] == "2026-11-08" and woke["today"] is False
    assert woke["misses"] == 3 and woke["colour"] == "yellow"                 # 9th, 10th, 11th missed: N=2 -> yellow (orange at 4)
    assert rows["Morning routine"]["misses"] == 0 and rows["Morning routine"]["colour"] == "green"
    assert rows["Morning routine"]["markers"] == ["covered_when_overwhelmed"]
    late = rows["No late screens"]                                             # false is the good value
    assert late["value_for_green"] is False and late["last_true"] == "2026-11-12" and late["misses"] == 0
    made = rows["Made something"]
    assert made["today"] is None and "colour" not in made and "steps_to_yellow" not in made and made["markers"] == ["dimmed"]
    assert out["today"] == "2026-11-12" and out["source"] == "apps_script"
    assert len(json.dumps(out)) < 2000


def test_days_adds_only_the_last_raw_values():
    out = compact_checklist(_sheet(), dt.date(2026, 11, 12), days=3)
    assert list(out["rows"][0]["recent"]) == ["2026-11-10", "2026-11-11", "2026-11-12"]
    assert "recent" not in compact_checklist(_sheet(), dt.date(2026, 11, 12))["rows"][0]


def test_colour_thresholds():
    def with_misses(k):
        d = lambda i: (dt.date(2026, 11, 1) + dt.timedelta(days=i)).isoformat()
        values = {d(i): (i < 11 - k) for i in range(0, 12)}                  # last k days before the 12th missed
        sheet = {"items": [{"item": "x", "checkable": True, "computed": False, "fields": {"steps to Yellow": 2, **values}}]}
        return compact_checklist(sheet, dt.date(2026, 11, 12))["rows"][0]
    assert [with_misses(k)["colour"] for k in (0, 1, 2, 3, 4, 5, 6)] == ["green", "green", "yellow", "yellow", "orange", "orange", "red"]
