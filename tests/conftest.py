import os
import pathlib
import tempfile

_tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["EA_DATA"] = str(_tmp / "data")
os.environ["EA_CONFIG"] = str(_tmp / "nonexistent.toml")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
