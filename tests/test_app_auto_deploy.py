"""apps/Atlas/auto_deploy.py: a new commit of the Mac app is built once; a failed build waits for a new commit."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("auto_deploy", Path(__file__).parents[1] / "apps/Atlas/auto_deploy.py")
ad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ad)


def setup(monkeypatch, tmp_path, want):
    monkeypatch.setattr(ad, "SUPPORT", tmp_path)
    monkeypatch.setattr(ad, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(ad, "wanted", lambda: want[0])
    monkeypatch.setattr(ad, "export", lambda: tmp_path)
    monkeypatch.setattr(ad, "git", lambda *a: "abc1234 a commit")


def test_builds_a_changed_source_once(monkeypatch, tmp_path):
    want, builds = ["v1"], []
    setup(monkeypatch, tmp_path, want)
    build = lambda app: (builds.append(app), (True, ""))[1]
    state = ad.tick({}, build)
    state = ad.tick(state, build)                      # nothing changed: no second build
    assert len(builds) == 1 and state["deployed"] == "v1" and ad.load()["deployed"] == "v1"
    want[0] = "v2"
    assert ad.tick(state, build)["deployed"] == "v2" and len(builds) == 2


def test_a_failed_build_is_not_retried_until_the_source_changes(monkeypatch, tmp_path):
    want, builds = ["v2"], []
    setup(monkeypatch, tmp_path, want)
    fail = lambda app: (builds.append(app), (False, "error: boom"))[1]
    state = ad.tick({"deployed": "v1"}, fail)
    state = ad.tick(state, fail)
    assert len(builds) == 1 and state == {"deployed": "v1", "failed": "v2"}   # the installed app is still v1
    want[0] = "v3"
    assert ad.tick(state, lambda app: (True, ""))["deployed"] == "v3"
