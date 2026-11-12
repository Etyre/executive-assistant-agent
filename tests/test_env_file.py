"""data/.env is copied from .env.example, which carries inline comments."""
import os

from ea import config


def test_env_file_inline_comments_are_not_values(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# header\nA_KEY=abc   # a comment\nB_KEY=    # only a comment\nC_KEY=\"quoted\"\nD_KEY=x#notacomment\n")
    for k in ("A_KEY", "B_KEY", "C_KEY", "D_KEY"):
        monkeypatch.delenv(k, raising=False)
    config._load_env_file(f)
    assert os.environ["A_KEY"] == "abc"
    assert os.environ["B_KEY"] == ""
    assert os.environ["C_KEY"] == "quoted"
    assert os.environ["D_KEY"] == "x#notacomment"
