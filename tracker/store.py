"""Where state lives. The Google Sheet is the source of truth between hourly runs, so the
scraper needs no database. LocalStore (CSV files) has the same shape for testing."""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

TABS = {
    "Summary": ["builder", "active_homes", "price_cuts_24h", "price_cuts_7d", "total_cut_7d", "avg_cut_7d",
                "biggest_cut_7d", "new_homes_7d", "removed_7d", "active_offers", "last_run", "last_run_status"],
    "Homes": ["home_id", "builder", "community", "city", "zip", "address", "plan", "beds", "baths", "sqft",
              "price", "builder_was_price", "first_price", "prev_price", "change_since_first",
              "pct_since_first", "last_change", "last_change_at", "cuts", "price_per_sqft", "status", "move_in",
              "incentive_text", "url", "first_seen", "last_seen", "last_checked", "active", "pending_price"],
    "Price Drops": ["at", "builder", "address", "community", "city", "old", "new", "delta", "pct", "url"],
    "Changes": ["at", "builder", "event", "home_id", "address", "community", "city", "old", "new", "delta",
                "pct", "url", "detail"],
    "Incentives": ["offer_id", "builder", "kind", "headline_amount", "rate", "text", "source_url",
                   "first_seen", "last_seen", "active"],
    "Run Log": ["run_at", "builder", "pages", "homes_seen", "new_homes", "price_cuts", "price_increases",
                "offers_seen", "offer_changes", "status", "note"],
    "Frontier": ["url", "builder", "kind", "discovered_at", "last_checked", "last_status", "misses"],
}
STATE_TABS = ["Homes", "Incentives", "Frontier"]
LOG_TABS = ["Changes", "Run Log"]
DERIVED_TABS = ["Summary", "Price Drops"]
NUMERIC = {"beds", "baths", "sqft", "price", "builder_was_price", "first_price", "prev_price",
           "change_since_first", "pct_since_first", "last_change", "cuts", "price_per_sqft", "headline_amount",
           "rate", "misses", "pending_price", "last_status", "old", "new", "delta", "pct"}


def _coerce(k: str, v):
    if v in ("", None):
        return None if k in NUMERIC else ""
    if k in NUMERIC:
        try:
            f = float(str(v).replace(",", "").replace("$", "").replace("%", ""))
            return int(f) if f.is_integer() and k not in {"baths", "pct", "pct_since_first", "rate"} else f
        except ValueError:
            return None
    if k == "active":
        return str(v).upper() in ("TRUE", "1", "YES")
    return v


def _row(cols, rec, for_sheet=False):
    out = []
    for c in cols:
        v = rec.get(c)
        if v is None:
            out.append("")
        elif isinstance(v, bool):
            out.append(v if for_sheet else ("TRUE" if v else "FALSE"))
        elif for_sheet and isinstance(v, str) and v and v[0] in "=+-@":
            out.append("'" + v)  # never let scraped text become a Sheets formula
        else:
            out.append(v)
    return out


@dataclass
class State:
    homes: dict = field(default_factory=dict)       # home_id -> record
    incentives: dict = field(default_factory=dict)  # offer_id -> record
    frontier: dict = field(default_factory=dict)    # url -> record
    changes: list = field(default_factory=list)     # full history (read for Summary / Price Drops)


def _records(rows):
    if not rows:
        return []
    head, body = rows[0], rows[1:]
    return [{k: _coerce(k, r[i] if i < len(r) else "") for i, k in enumerate(head)} for r in body if any(r)]


def _state_from(tab_rows: dict) -> State:
    st = State()
    st.homes = {r["home_id"]: r for r in _records(tab_rows.get("Homes"))}
    st.incentives = {r["offer_id"]: r for r in _records(tab_rows.get("Incentives"))}
    st.frontier = {r["url"]: r for r in _records(tab_rows.get("Frontier"))}
    st.changes = _records(tab_rows.get("Changes"))
    return st


class LocalStore:
    def __init__(self, folder: str):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, tab):
        return self.dir / (tab.lower().replace(" ", "_") + ".csv")

    def _read(self, tab):
        p = self._path(tab)
        if not p.exists():
            return []
        with p.open(newline="", encoding="utf-8") as f:
            return list(csv.reader(f))

    def _write(self, tab, rows):
        with self._path(tab).open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)

    def load(self) -> State:
        return _state_from({t: self._read(t) for t in STATE_TABS + ["Changes"]})

    MAX_LOG_ROWS = {"Run Log": 5000, "Changes": 200_000}

    def save(self, st: State, new_changes: list, runlog: list, derived: dict):
        self._write("Homes", [TABS["Homes"]] + [_row(TABS["Homes"], r) for r in st.homes.values()])
        self._write("Incentives", [TABS["Incentives"]] + [_row(TABS["Incentives"], r) for r in st.incentives.values()])
        self._write("Frontier", [TABS["Frontier"]] + [_row(TABS["Frontier"], r) for r in st.frontier.values()])
        for tab, new in (("Changes", new_changes), ("Run Log", runlog)):
            rows = self._read(tab) or [TABS[tab]]
            rows += [_row(TABS[tab], r) for r in new]
            cap = self.MAX_LOG_ROWS[tab]
            if len(rows) - 1 > cap:
                rows = [rows[0]] + rows[-cap:]
            self._write(tab, rows)
        (self.dir / "updated.txt").write_text(new_changes[0]["at"] if new_changes else
                                              (runlog[0]["run_at"] if runlog else ""))
        for tab, recs in derived.items():
            self._write(tab, [TABS[tab]] + [_row(TABS[tab], r) for r in recs])


class SheetStore:
    """Google Sheet backend (gspread + service account)."""

    MAX_LOG_ROWS = {"Run Log": 5000, "Changes": 200_000}

    def __init__(self, sheet_id: str, creds_json: str):
        import gspread
        from google.oauth2.service_account import Credentials
        info = json.loads(creds_json) if creds_json.strip().startswith("{") else json.load(open(creds_json))
        creds = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.gc = gspread.authorize(creds)
        self.sh = self.gc.open_by_key(sheet_id)
        self._ws = {}

    def _sheet(self, tab):
        if tab in self._ws:
            return self._ws[tab]
        import gspread
        try:
            ws = self.sh.worksheet(tab)
        except gspread.WorksheetNotFound:
            ws = self.sh.add_worksheet(tab, rows=1000, cols=len(TABS[tab]))
            ws.update([TABS[tab]], "A1", value_input_option="RAW")
            ws.freeze(rows=1)
            ws.format("1:1", {"textFormat": {"bold": True}})
            money = {"Homes": ["K:R"], "Price Drops": ["F:H"], "Changes": ["H:J"],
                     "Summary": ["E:G"], "Incentives": ["D:D"]}.get(tab, [])
            for rng in money:
                ws.format(rng, {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0"}})
            if tab == "Frontier":
                ws.hide()
        self._ws[tab] = ws
        return ws

    def _read(self, tab):
        return self._sheet(tab).get_all_values()

    def _replace(self, tab, rows):
        ws = self._sheet(tab)
        need_rows = max(len(rows) + 50, 100)
        if ws.row_count < need_rows or ws.col_count < len(TABS[tab]):
            ws.resize(rows=need_rows, cols=len(TABS[tab]))
        from gspread.utils import rowcol_to_a1
        ws.batch_clear([f"A2:{rowcol_to_a1(ws.row_count, ws.col_count)}"])
        ws.update(rows, "A1", value_input_option="RAW")

    def load(self) -> State:
        for t in TABS:  # make sure every tab exists, in order
            self._sheet(t)
        return _state_from({t: self._read(t) for t in STATE_TABS + ["Changes"]})

    def save(self, st: State, new_changes: list, runlog: list, derived: dict):
        self._replace("Homes", [TABS["Homes"]] + [_row(TABS["Homes"], r, True) for r in st.homes.values()])
        self._replace("Incentives", [TABS["Incentives"]] + [_row(TABS["Incentives"], r, True) for r in st.incentives.values()])
        self._replace("Frontier", [TABS["Frontier"]] + [_row(TABS["Frontier"], r, True) for r in st.frontier.values()])
        for tab, new in (("Changes", new_changes), ("Run Log", runlog)):
            if new:
                ws = self._sheet(tab)
                ws.append_rows([_row(TABS[tab], r, True) for r in new], value_input_option="RAW")
                excess = len(ws.col_values(1)) - 1 - self.MAX_LOG_ROWS[tab]
                if excess > 0:
                    ws.delete_rows(2, 1 + excess)
        for tab, recs in derived.items():
            self._replace(tab, [TABS[tab]] + [_row(TABS[tab], r, True) for r in recs])


def store_from_env():
    sheet_id = os.environ.get("SHEET_ID", "").strip()
    creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if sheet_id and creds:
        return SheetStore(sheet_id, creds)
    return LocalStore(os.environ.get("LOCAL_DATA_DIR", "docs/data"))
