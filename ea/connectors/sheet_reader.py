"""Google Sheets the user has opened to the agent, read-only (first: the responses of his "Intentionality and work
block record" form). Each sheet has its own small Apps Script bound to it (`deploy/sheet_reader_script/`), which
only ever returns rows. READ_SHEETS lists them as "name=/exec URL", comma- or newline-separated; READ_SHEETS_TOKEN
is the one shared secret every such script checks."""
from __future__ import annotations

from typing import Any

import httpx

from .. import config
from .http import SourceError


class SheetReader:
    def __init__(self, settings: config.Settings | None = None):
        s = settings or config.load()
        raw = config.secret(s.sources.read_sheets_env) or ""
        self.sheets = {n.strip(): u.strip() for n, _, u in (part.partition("=") for part in raw.replace("\n", ",").split(",")) if n.strip() and u.strip()}
        self.token = config.secret(s.sources.read_sheets_token_env) or ""

    def enabled(self) -> bool:
        return bool(self.sheets and self.token)

    def _call(self, sheet: str, action: str, **fields: Any) -> dict[str, Any]:
        if not self.enabled():
            raise SourceError("no readable sheets are set up: READ_SHEETS and READ_SHEETS_TOKEN are not both set "
                              "(deploy/sheet_reader_script/Code.gs has the steps; the user has to deploy it)")
        if sheet not in self.sheets:
            raise SourceError(f"no sheet named '{sheet}'; the ones set up: {', '.join(self.sheets)}")
        try:
            r = httpx.post(self.sheets[sheet], json={"token": self.token, "action": action, **{k: v for k, v in fields.items() if v is not None}},
                           follow_redirects=True, timeout=60)
            r.raise_for_status()
            out = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise SourceError(f"the script of sheet '{sheet}' did not answer with JSON ({str(e)[:160]}); check its deployment (Execute as: Me, access: Anyone)") from e
        if out.get("error"):
            raise SourceError(f"sheet '{sheet}': {out['error']}")
        return out

    def describe(self) -> list[dict[str, Any]]:
        """Every sheet set up: its name here, its title in Google, and each tab's row count and columns."""
        out = []
        for name in self.sheets:
            try:
                out.append({"sheet": name, **self._call(name, "tabs")})
            except SourceError as e:
                out.append({"sheet": name, "error": str(e)})
        return out

    def rows(self, sheet: str, last: int = 20, offset: int = 0, tab: str | None = None) -> dict[str, Any]:
        """The last `last` records, oldest first, each a dict of column -> value with empty cells left out."""
        out = self._call(sheet, "rows", last=max(1, min(int(last), 1000)), offset=max(0, int(offset)), tab=tab)
        heads = [h.strip() or f"column {i + 1}" for i, h in enumerate(out.get("headers", []))]
        records = [{h: v for h, v in zip(heads, row) if str(v).strip()} for row in out.get("rows", [])]
        return {"sheet": sheet, "title": out.get("title"), "tab": out.get("tab"), "total_rows": out.get("total_rows"),
                "first_row": out.get("first_row"), "records": records}
