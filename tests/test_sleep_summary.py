"""sleep_summary must fit in a tool result: Sleep Bandit's full answer was 100,000 characters and the CLI refused
to hand it to the model (2026-11-19)."""
import json

import pytest

from ea.connectors import sleep_bandit as sb


def _readout(n_doses: int) -> dict:
    return {"p_helps": 0.5546666, "p_meaningful": 0.0856666, "on_effect": 0.17321669, "on_ci_low": -2.04486, "on_ci_high": 2.32136,
            "recommended_dose": 150.0, "recommended_p": 0.3876666, "n_on": 100, "n_off": 18,
            "doses": [{"dose": float(d), "p_best": 0.1893333, "mean_effect": 0.488003, "ci_low": -1.5458, "ci_high": 2.6103,
                       "raw_mean": 43.1869, "n_nights": 5} for d in range(n_doses)]}


def _full() -> dict:
    cells = [{"intervention": "Glycine", "unit": "mg", "assigned_dose": 1000.0, "on": True, "done": True, "taken_dose": 1000.0,
              "excluded": False, "input_value": None},
             {"intervention": "Melatonin", "unit": "mcg", "assigned_dose": 300.0, "on": True, "done": True, "taken_dose": 150.0,
              "excluded": False, "input_value": None},
             {"intervention": "Apigenin", "unit": "mg", "assigned_dose": 0, "on": False, "done": False, "taken_dose": None,
              "excluded": False, "input_value": None},
             {"intervention": "Eyemask", "unit": "", "assigned_dose": 0, "on": False, "done": True, "taken_dose": None,
              "excluded": False, "input_value": None}]
    return {"today": "2026-11-19", "n_completed_nights": 124,
            "nights": [{"date": "2026-11-18", "is_tonight": False, "hrv": 48.4, "cells": cells, "tags": ["short-night"]}] * 14,
            "interventions": [{"id": i, "name": name, "category": "supplement", "kind": "dose", "unit": "mg", "active": True,
                               "observational": None, "p_scheduled": 0.55466,
                               **{k: _readout(13) for k in sb.OUTCOMES}}
                              for i, name in enumerate(["Glycine", "Melatonin", "Apigenin", "Spiky mat"] + [f"Other {n}" for n in range(14)])]}


@pytest.fixture
def bandit(monkeypatch):
    monkeypatch.setattr(sb, "get_json", lambda c, path, **kw: _full())

    class Router:
        def client(self, name):
            import contextlib
            return contextlib.nullcontext()
    return sb.SleepBandit(Router())


def test_summary_is_small_and_keeps_the_verdicts(bandit):
    assert len(json.dumps(_full())) > 100_000
    out = bandit.summary(14)
    assert len(json.dumps(out)) < 25_000 and "note" not in out
    g = out["interventions"][0]
    assert g["name"] == "Glycine" and g["hrv"]["recommended_dose"] == 150.0 and g["hrv"]["p_helps"] == 0.555
    assert g["latency_min"] == {"p_helps": 0.555, "on_effect": 0.173}
    assert "doses" not in json.dumps(out)


def test_night_keeps_what_was_on_and_what_was_taken(bandit):
    night = bandit.summary(14)["nights"][0]
    assert night["hrv"] == 48.4 and night["tags"] == ["short-night"] and "cells" not in night
    assert night["on"] == {"Glycine": 1000.0, "Melatonin": 300.0}
    assert night["done"] == ["Glycine", "Melatonin", "Eyemask"]      # Eyemask was off but done anyway
    assert night["off"] == ["Apigenin", "Eyemask"]
    assert night["taken"] == {"Melatonin": 150.0}                    # differed from the assigned 300
    assert "excluded" not in night and "input" not in night


def test_a_long_range_still_fits_and_says_what_was_cut(bandit, monkeypatch):
    full = _full()
    full["nights"] = [{**full["nights"][0], "date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"} for i in range(300)]
    monkeypatch.setattr(sb, "get_json", lambda c, path, **kw: full)
    out = bandit.summary(300)
    assert len(json.dumps(out)) <= sb.SUMMARY_CHARS + 500
    newest = max(out["nights"], key=lambda n: n["date"])
    oldest = min(out["nights"], key=lambda n: n["date"])
    assert "on" in newest and "on" not in oldest and oldest["hrv"] == 48.4
    assert "only their numbers" in out["note"] and "oldest are left out" in out["note"]


def test_one_intervention_by_name_has_its_dose_tables(bandit):
    out = bandit.summary(14, "spiky")
    assert [i["name"] for i in out["interventions"]] == ["Spiky mat"]
    assert len(out["interventions"][0]["readout_lat"]["doses"]) == 13
    with pytest.raises(ValueError, match="Glycine"):
        bandit.summary(14, "nonsense")
