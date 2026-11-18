"""Images the user sends in the chat: stored as files under `<EA_DATA>/images/`, referenced from the conversation
store as {"type": "image", "source": {"type": "file", "name": ..., "media_type": ...}} and inflated to base64
blocks only when a turn is sent to the model.

An image can also arrive as a path on Eli's Mac written in the message ("/Users/.../Screenshot ... .png", which is
how a screenshot dragged into a terminal or pasted from another tool shows up). `from_paths` fetches each such file
(through the Mac bridge when the agent runs on a server) and attaches it like any other image. Only image files in
the Mac's Downloads and Desktop folders can be fetched this way (`read_mac_image`, which the bridge uses too)."""
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


MAC_PATH = re.compile(r"(?:/Users/|~/)[^\n\"'<>|*`\[\]]*?\.(?:png|jpe?g|gif|webp)(?![A-Za-z0-9])", re.I)
MAC_FOLDERS = ("Downloads", "Desktop")
_SUFFIX = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def read_mac_image(path: str, home: pathlib.Path | None = None) -> tuple[bytes, str]:
    """An image file on this Mac by its path, or ValueError saying why not. Only image files inside Downloads or
    Desktop (after resolving links), up to MAX_BYTES. macOS writes a narrow no-break space into a screenshot's name
    ("12.44.21\u202fAM") that turns into a plain space when the path is copied, so a path that does not exist is
    matched again with all white space treated alike."""
    home = (home or pathlib.Path.home()).resolve()
    p = pathlib.Path(path.strip().replace("~/", str(home) + "/", 1) if path.strip().startswith("~/") else path.strip())
    media = _SUFFIX.get(p.suffix.lower())
    if media is None or not p.is_absolute():
        raise ValueError("not the path of a png, jpeg, gif or webp file")
    if not p.exists() and p.parent.is_dir():
        flat = lambda s: re.sub(r"\s", " ", s)
        p = next((f for f in p.parent.iterdir() if flat(f.name) == flat(p.name)), p)
    p = p.resolve()
    if not any(p.is_relative_to(home / f) for f in MAC_FOLDERS):
        raise ValueError(f"only images in {' or '.join(MAC_FOLDERS)} on the Mac can be fetched by path")
    if not p.is_file():
        raise ValueError("no such file on the Mac")
    if p.stat().st_size > MAX_BYTES:
        raise ValueError(f"image too large ({p.stat().st_size // 1024} KB; max {MAX_BYTES // 1024} KB)")
    return p.read_bytes(), media


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

    def from_paths(self, text: str, router: Any = None, limit: int = 6) -> tuple[list[dict[str, Any]], str]:
        """The images named by Mac paths in a message: (stored blocks, a line for the model about any that could
        not be fetched, or ""). `router`: the ServiceRouter; with a bridge the files come from the Mac through it,
        without one this is the Mac and they are read here."""
        blocks, missed = [], []
        for path in list(dict.fromkeys(m.group(0) for m in MAC_PATH.finditer(text or "")))[:limit]:
            try:
                if router is not None and getattr(router, "bridge", ""):
                    r = router.mac("GET", "/mac/image", raw=True, params={"path": path})
                    raw, media = r, _SUFFIX[pathlib.PurePath(path).suffix.lower()]
                else:
                    raw, media = read_mac_image(path)
                blocks.append(self.save(base64.b64encode(raw).decode(), media))
            except Exception as e:  # noqa: BLE001 - the message still goes through; the model is told
                missed.append(f"{path} ({str(e)[:160]})")
        note = ("\n\n[From the server, not from Eli: the image at this path could not be fetched from his Mac, so you "
                "cannot see it: " + "; ".join(missed) + "]") if missed else ""
        return blocks, note

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
