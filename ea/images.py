"""Images the user sends in the chat: stored as files under `<EA_DATA>/images/`, referenced from the conversation
store as {"type": "image", "source": {"type": "file", "name": ..., "media_type": ...}} and inflated to base64
blocks only when a turn is sent to the model."""
from __future__ import annotations

import base64
import hashlib
import pathlib
import re
from typing import Any

from . import config

MEDIA = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
MAX_BYTES = 8 * 1024 * 1024
_NAME = re.compile(r"^[a-f0-9]{16}\.(png|jpg|gif|webp)$")


class ImageStore:
    def __init__(self, folder: pathlib.Path | None = None):
        self.folder = pathlib.Path(folder or config.load().data_dir / "images")
        self.folder.mkdir(parents=True, exist_ok=True)

    def save(self, data_b64: str, media_type: str) -> dict[str, Any]:
        if media_type not in MEDIA:
            raise ValueError(f"unsupported image type {media_type!r}; use png, jpeg, gif or webp")
        raw = base64.b64decode(data_b64, validate=False)
        if len(raw) > MAX_BYTES:
            raise ValueError(f"image too large ({len(raw) // 1024} KB; max {MAX_BYTES // 1024} KB)")
        name = hashlib.sha256(raw).hexdigest()[:16] + "." + MEDIA[media_type]
        p = self.folder / name
        if not p.exists():
            p.write_bytes(raw)
        return {"type": "image", "source": {"type": "file", "name": name, "media_type": media_type}}

    def load(self, name: str) -> tuple[bytes, str] | None:
        if not _NAME.match(name):
            return None
        p = self.folder / name
        if not p.exists():
            return None
        ext = name.rsplit(".", 1)[1]
        media = next(m for m, e in MEDIA.items() if e == ext)
        return p.read_bytes(), media

    def inflate(self, block: dict[str, Any]) -> dict[str, Any]:
        """A stored file reference -> the base64 block the API wants (other blocks pass through)."""
        src = block.get("source") if isinstance(block, dict) else None
        if block.get("type") != "image" or not src or src.get("type") != "file":
            return block
        loaded = self.load(src.get("name", ""))
        if loaded is None:
            return {"type": "text", "text": "[an image that is no longer available]"}
        raw, media = loaded
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": base64.b64encode(raw).decode()}}

    def inflate_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for m in messages:
            c = m.get("content")
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in c):
                m = {**m, "content": [self.inflate(b) if isinstance(b, dict) else b for b in c]}
            out.append(m)
        return out
