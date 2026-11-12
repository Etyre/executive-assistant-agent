"""Telegram bot: optional legacy chat channel (the dedicated apps + push are the main one). Raw Bot API over httpx with long polling, so
there is no inbound port to expose. Only the allow-listed user IDs are answered; everyone else is ignored.
Voice notes are transcribed (stt.py) and handled as text."""
from __future__ import annotations

import asyncio
import logging
import pathlib
import tempfile

import httpx

from .. import config, stt
from ..agent.core import Agent

log = logging.getLogger(__name__)
API = "https://api.telegram.org"
MAX_LEN = 4000


class Telegram:
    def __init__(self, agent: Agent, settings: config.Settings | None = None):
        self.agent = agent
        self.s = settings or config.load()
        self.token = config.secret(self.s.channels.telegram_token_env)
        self.allowed = set(self.s.channels.telegram_allowed_user_ids)
        self.http = httpx.AsyncClient(base_url=f"{API}/bot{self.token}", timeout=70)
        self.file_http = httpx.AsyncClient(base_url=f"{API}/file/bot{self.token}", timeout=70)
        self._chat_id: int | None = self.agent.convo.get("telegram_chat_id")
        self._loop: asyncio.AbstractEventLoop | None = None

    def enabled(self) -> bool:
        return bool(self.token)

    # ------------------------------------------------------------------ sending
    async def send(self, chat_id: int, text: str) -> None:
        for chunk in _chunks(text, MAX_LEN):
            r = await self.http.post("/sendMessage", json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True})
            if r.status_code >= 400:
                log.warning("sendMessage failed %s: %s", r.status_code, r.text[:200])

    def send_sync(self, text: str) -> None:
        """Called from the agent's thread (send_message tool, reminders)."""
        if not self._chat_id:
            log.warning("no Telegram chat yet; message not delivered: %s", text[:80])
            return
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self.send(self._chat_id, text), self._loop)
        else:
            httpx.post(f"{API}/bot{self.token}/sendMessage", json={"chat_id": self._chat_id, "text": text[:MAX_LEN]}, timeout=30)

    # ------------------------------------------------------------------ receiving
    async def run(self) -> None:
        if not self.enabled():
            log.info("Telegram disabled (no token)")
            return
        self._loop = asyncio.get_running_loop()
        if self.agent.outbound is None:            # normally __main__ wires a fan-out (push + events + telegram)
            self.agent.outbound = self.send_sync
        offset = None
        me = await self.http.get("/getMe")
        log.info("Telegram bot @%s polling; allowed users: %s", me.json().get("result", {}).get("username"), sorted(self.allowed) or "NONE (set channels.telegram_allowed_user_ids)")
        while True:
            try:
                r = await self.http.get("/getUpdates", params={"timeout": 60, "offset": offset, "allowed_updates": '["message"]'})
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    asyncio.create_task(self._handle(upd.get("message") or {}))
            except (httpx.HTTPError, ValueError) as e:
                log.warning("telegram poll error: %s", e)
                await asyncio.sleep(5)

    async def _handle(self, msg: dict) -> None:
        user = (msg.get("from") or {}).get("id")
        chat_id = (msg.get("chat") or {}).get("id")
        if not chat_id or user not in self.allowed:
            if user:
                log.info("ignored message from unknown Telegram user %s (add to telegram_allowed_user_ids to allow)", user)
            return
        if self._chat_id != chat_id:
            self._chat_id = chat_id
            self.agent.convo.set("telegram_chat_id", chat_id)
        text = msg.get("text") or msg.get("caption") or ""
        voice = msg.get("voice") or msg.get("audio")
        if voice:
            try:
                text = (text + "\n" if text else "") + await self._transcribe(voice["file_id"])
            except Exception as e:  # noqa: BLE001
                await self.send(chat_id, f"(couldn't transcribe the voice note: {e})")
                return
        if not text.strip():
            return
        if text.strip() == "/start":
            await self.send(chat_id, f"{self.agent.settings.agent_name} here. Talk to me.")
            return
        await self.http.post("/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
        reply = await asyncio.to_thread(self.agent.handle, text, "telegram", "chat")
        await self.send(chat_id, reply)

    async def _transcribe(self, file_id: str) -> str:
        info = (await self.http.get("/getFile", params={"file_id": file_id})).json()["result"]
        data = (await self.file_http.get("/" + info["file_path"])).content
        with tempfile.NamedTemporaryFile(suffix=pathlib.Path(info["file_path"]).suffix or ".oga", delete=False) as f:
            f.write(data)
            path = pathlib.Path(f.name)
        try:
            return await asyncio.to_thread(stt.transcribe, path, self.s)
        finally:
            path.unlink(missing_ok=True)


def _chunks(text: str, n: int) -> list[str]:
    out = []
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return [c for c in out if c] or ["(empty)"]
