"""Google Sheets output layer.

Tab lifecycle is ensure -> clear -> write; tabs are never deleted (that would destroy
charts, conditional formats and any references to them). Every method is safe to
run twice with the same input.

Values are written with valueInputOption=RAW: numbers arrive as JSON numbers, dates
as date serials with a date format, and text is never parsed (a note starting with
"=" stays text, "007" keeps its zeros). No type inference happens here; values
arrive from transform already typed.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from googleapiclient.discovery import build

from pipedrive_export import config

log = logging.getLogger(__name__)

_SHEETS_EPOCH = dt.date(1899, 12, 30)


@dataclass(frozen=True)
class TabPayload:
    header: list[Any]
    rows: list[list[Any]]  # typed values; None = blank
    kinds: dict[str, str]  # column -> money/number/percent/date/text
    # Never shrink the grid below this many rows (header included). Charts read fixed
    # ranges; deleting rows under a chart would permanently shorten its range.
    min_rows: int = 2


@dataclass(frozen=True)
class SheetState:
    tabs: dict[str, int]  # title -> sheetId
    generated_at: dt.datetime | None
    generated_at_metadata_id: int | None


def _a1(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


HEADER_CHAR_PX = 8.5  # generous average width of a 10pt bold character
HEADER_EXTRA_PX = 16 + 24  # cell padding (8 + 8) + the filter button in the header cell
MIN_COLUMN_PX, MAX_COLUMN_PX = 110, 320


def header_column_width(header: Any) -> int:
    """Column width (px) that keeps the header's longest word on one line.

    Sheets wraps between words, but a single word wider than the column is broken
    mid-word ("Technolo-gy"). Sizing to the longest word lets headers wrap cleanly.
    """
    longest = max((len(word) for word in str(header or "").split()), default=0)
    return math.ceil(min(MAX_COLUMN_PX, max(MIN_COLUMN_PX, longest * HEADER_CHAR_PX + HEADER_EXTRA_PX)))


def rgb(hex_color: str) -> dict[str, float]:
    """'#1f2a37' -> Sheets API color {red, green, blue} in 0-1."""
    h = hex_color.lstrip("#")
    return {k: int(h[i:i + 2], 16) / 255 for k, i in (("red", 0), ("green", 2), ("blue", 4))}


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, dt.date):
        return (value - _SHEETS_EPOCH).days
    return value


class SheetsPublisher:
    def __init__(self, credentials, spreadsheet_id: str):
        self._service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        self.spreadsheet_id = spreadsheet_id

    def batch_update(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        return self._service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests}).execute()

    def is_empty(self, title: str) -> bool:
        """True if the tab has no charts and no values (e.g. just created, or a failed build)."""
        meta = self._service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id, fields="sheets(properties(title),charts(chartId))",
        ).execute()
        sheet = next(s for s in meta.get("sheets", []) if s["properties"]["title"] == title)
        return not sheet.get("charts") and not self.read_values(title)

    def conditional_formats(self, title: str) -> list[dict[str, Any]]:
        """The tab's conditional format rules, in order (a rule's position is its index)."""
        meta = self._service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id, fields="sheets(properties(title),conditionalFormats)",
        ).execute()
        sheet = next(s for s in meta.get("sheets", []) if s["properties"]["title"] == title)
        return sheet.get("conditionalFormats", [])

    def read_values(self, title: str) -> list[list[Any]]:
        """All values of a tab, numbers as numbers (not display strings)."""
        result = self._service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id, range=_a1(title), valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        return result.get("values", [])

    def read_state(self) -> SheetState:
        meta = self._service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            fields="developerMetadata,sheets(properties(sheetId,title))",
        ).execute()
        tabs = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}

        generated_at, metadata_id = None, None
        for md in meta.get("developerMetadata", []):
            if md.get("metadataKey") == config.GENERATED_AT_METADATA_KEY:
                generated_at = dt.datetime.fromisoformat(md["metadataValue"])
                metadata_id = md["metadataId"]
        return SheetState(tabs, generated_at, metadata_id)

    def count_data_rows(self, titles: Sequence[str]) -> dict[str, int]:
        """Rows currently holding data, counted as non-blank cells in column A (Title,
        always populated). Grid size is not used: it includes empty rows people add."""
        result = self._service.spreadsheets().values().batchGet(
            spreadsheetId=self.spreadsheet_id, ranges=[f"{_a1(t)}!A2:A" for t in titles],
        ).execute()
        return {
            title: sum(1 for row in vr.get("values", []) if row and str(row[0]).strip())
            for title, vr in zip(titles, result.get("valueRanges", []))
        }

    def ensure_tabs(self, titles: Sequence[str], hidden: set[str] = frozenset(),
                    renames: dict[str, str] | None = None) -> tuple[dict[str, int], set[str]]:
        """Make sure every title exists; return (title -> sheetId, titles created now).

        A tab listed in `renames` (old -> new) under its old title is renamed in place.
        A missing tab is created at its position in `titles` (hidden if listed in
        `hidden`). Existing tabs are otherwise never moved, renamed or unhidden. A
        brand-new spreadsheet has one default tab (named by locale, e.g. "Sheet1"): it
        is renamed to the first title instead of adding a tab and deleting it.
        """
        renames = renames or {}
        state = self.read_state()
        existing = dict(state.tabs)
        requests: list[dict[str, Any]] = []

        # Renamed tabs (config.TAB_RENAMES): rename in place, keeping ID and formatting.
        for old, new in renames.items():
            if old in existing and new not in existing:
                log.info("Renaming tab %r to %r", old, new)
                requests.append({"updateSheetProperties": {
                    "properties": {"sheetId": existing[old], "title": new}, "fields": "title"}})
                existing[new] = existing.pop(old)

        missing = [t for t in titles if t not in existing]
        if not missing:
            if requests:
                self.batch_update(requests)
            return {t: existing[t] for t in titles}, set()

        to_add = missing
        if len(existing) == 1 and not any(t in existing for t in titles):
            (default_title, default_id), = existing.items()
            log.info("Renaming default tab %r to %r", default_title, missing[0])
            requests.append({"updateSheetProperties": {
                "properties": {"sheetId": default_id, "title": missing[0], "hidden": missing[0] in hidden},
                "fields": "title,hidden"}})
            to_add = missing[1:]
        sheet_count = len(existing)
        for title in to_add:
            log.info("Adding tab %r", title)
            index = min(titles.index(title), sheet_count)
            requests.append({"addSheet": {"properties": {"title": title, "index": index, "hidden": title in hidden}}})
            sheet_count += 1
        self.batch_update(requests)

        tabs = self.read_state().tabs
        return {t: tabs[t] for t in titles}, set(missing)

    def write_tabs(self, tab_ids: dict[str, int], payloads: dict[str, TabPayload]) -> None:
        """Resize each grid to fit, clear it, write header + rows. Three API calls total."""
        # 1. Size the grid exactly: expands for more data, and drops stale rows/columns.
        #    At least one data row, since the frozen header cannot be the only row.
        resize = []
        for title, p in payloads.items():
            resize.append({"updateSheetProperties": {
                "properties": {"sheetId": tab_ids[title], "gridProperties": {
                    "rowCount": max(len(p.rows) + 1, p.min_rows, 2),
                    "columnCount": len(p.header),
                    "frozenRowCount": config.FROZEN_ROWS,
                    "frozenColumnCount": config.FROZEN_COLUMNS,
                }},
                "fields": "gridProperties(rowCount,columnCount,frozenRowCount,frozenColumnCount)",
            }})
        self.batch_update(resize)

        # 2. Clear, so a month with fewer deals leaves no stale values.
        values = self._service.spreadsheets().values()
        values.batchClear(spreadsheetId=self.spreadsheet_id,
                          body={"ranges": [_a1(t) for t in payloads]}).execute()

        # 3. Write. Every row is full width; blanks are "".
        data = []
        for title, p in payloads.items():
            rows = [[_cell(v) for v in row] for row in p.rows]
            if any(len(r) != len(p.header) for r in rows):
                raise ValueError(f"Tab {title!r}: a row does not match the header width")
            data.append({"range": f"{_a1(title)}!A1", "values": [[_cell(v) for v in p.header], *rows]})
        values.batchUpdate(spreadsheetId=self.spreadsheet_id,
                           body={"valueInputOption": "RAW", "data": data}).execute()
        for title, p in payloads.items():
            log.info("Wrote %d rows to %r", len(p.rows), title)

    def apply_standard_formatting(self, tab_ids: dict[str, int], payloads: dict[str, TabPayload],
                                  generated_at: dt.datetime, previous: SheetState) -> None:
        """Header style, column widths, number formats, basic filter, and the generated-at
        stamp. One call. Column widths are reset every run to fit the headers."""
        requests: list[dict[str, Any]] = []
        for title, p in payloads.items():
            sid, n_rows, n_cols = tab_ids[title], len(p.rows), len(p.header)
            requests.append({"repeatCell": {  # table-style header: dark background, white bold text
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {
                    "backgroundColorStyle": {"rgbColor": rgb(config.HEADER_BG)},
                    "textFormat": {"bold": True, "foregroundColorStyle": {"rgbColor": rgb(config.HEADER_TEXT)}},
                    "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE",
                    "padding": {"top": 6, "bottom": 6, "left": 8, "right": 8}}},
                "fields": "userEnteredFormat(backgroundColorStyle,textFormat,wrapStrategy,verticalAlignment,padding)",
            }})
            requests.append({"setBasicFilter": {"filter": {"range": {
                "sheetId": sid, "startRowIndex": 0, "endRowIndex": n_rows + 1,
                "startColumnIndex": 0, "endColumnIndex": n_cols}}}})
            for col, name in enumerate(p.header):  # wide enough that no header word breaks
                requests.append({"updateDimensionProperties": {
                    "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                    "properties": {"pixelSize": header_column_width(name)}, "fields": "pixelSize"}})
            if n_rows == 0:
                continue
            for col, name in enumerate(p.header):
                fmt = config.NUMBER_FORMATS.get(p.kinds.get(name, "text"))
                if fmt:
                    requests.append({"repeatCell": {
                        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": n_rows + 1,
                                  "startColumnIndex": col, "endColumnIndex": col + 1},
                        "cell": {"userEnteredFormat": {"numberFormat": fmt}},
                        "fields": "userEnteredFormat.numberFormat",
                    }})

        stamp = generated_at.isoformat(timespec="seconds")
        if previous.generated_at_metadata_id is None:
            requests.append({"createDeveloperMetadata": {"developerMetadata": {
                "metadataKey": config.GENERATED_AT_METADATA_KEY, "metadataValue": stamp,
                "location": {"spreadsheet": True}, "visibility": "DOCUMENT"}}})
        else:
            requests.append({"updateDeveloperMetadata": {
                "dataFilters": [{"developerMetadataLookup": {"metadataId": previous.generated_at_metadata_id}}],
                "developerMetadata": {"metadataValue": stamp},
                "fields": "metadataValue"}})
        self.batch_update(requests)
