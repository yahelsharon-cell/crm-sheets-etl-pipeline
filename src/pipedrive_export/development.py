"""Development Dashboard: a milestone tracker with one row per company.

    Company | Stage | Phase | Runway | 1st..4th + Fundable milestone (description, status) | Comments | Deal ID (hidden)

Rows: Portfolio companies first (A-Z), then Pipeline companies grouped by stage in
config.STAGE_ORDER (then A-Z), matching the data tabs. Deals in
config.DEVELOPMENT_EXCLUDED_STAGES (YBD) are left out.

Milestones come from Pipedrive and are refreshed on every run. Every run rewrites
Company, Stage and the Pipedrive columns, and KEEPS what the team typed in the tracking
columns (Comments, plus any column they add). Rows are matched to companies by the
hidden Deal ID, never by row position, so a new company appearing, a stage change or
re-sorting cannot shift someone's notes onto the wrong company.

A company that drops off the tracker (e.g. moved to YBD, won/lost) keeps its row at the
bottom, marked, if anything was typed for it (so no notes are lost); otherwise the row
is dropped.
"""

from __future__ import annotations

from typing import Any

from pipedrive_export import config
from pipedrive_export.sheets import header_column_width, rgb
from pipedrive_export.transform import Record, TransformError, normalize

COMPANY = "Company"
STAGE = "Stage"
DEAL_ID = "Deal ID"
NO_LONGER_TRACKED = " (no longer tracked)"

AUTO_NOTE = "Filled from Pipedrive on every run. Edits here are overwritten: update milestones in Pipedrive."
TRACKING_NOTE = "Typed by the team. Kept across runs."
TRACKING_HEADER_BG = "#0f4c5c"  # tracking headers differ from Pipedrive ones: "you type here"


def _auto_columns() -> list[str]:
    return [COMPANY, STAGE, *config.DEVELOPMENT_PIPEDRIVE_COLUMNS]


def tracked(portfolio: list[Record], pipeline: list[Record]) -> list[Record]:
    """Companies on the tracker, in display order. `pipeline` must already be in stage
    order (as transform.segment returns it); excluded stages (YBD) are dropped."""
    excluded = {normalize(s) for s in config.DEVELOPMENT_EXCLUDED_STAGES}
    return ([*sorted(portfolio, key=lambda r: (normalize(r.title), r.deal_id))]
            + [r for r in pipeline if normalize(r.stage) not in excluded])


def build_header(existing_header: list[Any]) -> list[str]:
    """Auto columns, configured tracking columns, then any column the team added, then Deal ID."""
    auto = _auto_columns()
    header = auto + list(config.DEVELOPMENT_TRACKING_COLUMNS)
    for h in existing_header:
        h = str(h).strip() if h is not None else ""
        if h and h not in header and h != DEAL_ID:
            header.append(h)
    return header + [DEAL_ID]


def _blank(v: Any) -> Any:
    return None if v == "" else v


def merge(existing: list[list[Any]], portfolio: list[Record], pipeline: list[Record] = ()) -> list[list[Any]]:
    """New tab content (header + rows) from the current tab values and the deals."""
    companies = tracked(list(portfolio), list(pipeline))
    missing = [c for c in config.DEVELOPMENT_PIPEDRIVE_COLUMNS
               if companies and c not in companies[0].values]
    if missing:
        raise TransformError(f"DEVELOPMENT_PIPEDRIVE_COLUMNS not among exported columns: {missing}")

    old_header = [str(h).strip() for h in existing[0]] if existing else []
    header = build_header(old_header)
    tracking = header[len(_auto_columns()):-1]

    old_rows: dict[int, dict[str, Any]] = {}
    if DEAL_ID in old_header:
        id_col = old_header.index(DEAL_ID)
        for row in existing[1:]:
            padded = [_blank(v) for v in (list(row) + [None] * len(old_header))[:len(old_header)]]
            try:
                deal_id = int(padded[id_col])
            except (TypeError, ValueError):
                continue  # a row without a deal ID was typed by hand; nothing to match it to
            old_rows[deal_id] = dict(zip(old_header, padded))

    rows = []
    for r in companies:
        old = old_rows.get(r.deal_id, {})
        rows.append([r.title, r.stage, *(r.values.get(c) for c in config.DEVELOPMENT_PIPEDRIVE_COLUMNS),
                     *(old.get(c) for c in tracking), r.deal_id])

    current = {r.deal_id for r in companies}
    for deal_id, old in old_rows.items():
        if deal_id in current or all(old.get(c) is None for c in tracking):
            continue
        name = str(old.get(COMPANY) or f"Deal {deal_id}").removesuffix(NO_LONGER_TRACKED) + NO_LONGER_TRACKED
        rows.append([name, None, *([None] * len(config.DEVELOPMENT_PIPEDRIVE_COLUMNS)),
                     *(old.get(c) for c in tracking), deal_id])
    return [header, *rows]


def _col_range(sheet_id: int, col: int, first_row: int = 1) -> dict[str, int]:
    """A whole column from first_row down, unbounded, so it covers rows added later."""
    return {"sheetId": sheet_id, "startRowIndex": first_row, "startColumnIndex": col, "endColumnIndex": col + 1}


def _is_status_rule(rule: dict[str, Any]) -> bool:
    """A rule this module added: text-equals coloring on whole single columns from row 2
    down (the shape _col_range makes). Recognised by shape, not by column position, so
    rules from an older layout (columns since shifted) are cleaned up too."""
    condition = rule.get("booleanRule", {}).get("condition", {})
    ranges = rule.get("ranges", [])
    return (condition.get("type") == "TEXT_EQ" and bool(ranges)
            and all(rg.get("startRowIndex") == 1 and "endRowIndex" not in rg
                    and rg.get("endColumnIndex") == rg.get("startColumnIndex", 0) + 1 for rg in ranges))


def format_requests(sheet_id: int, header: list[str],
                    existing_rules: list[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """Formatting for the tab; safe to run every time.

    Status colors are replaced on each run: rules this module added earlier (found in
    `existing_rules`, the tab's current conditional formats in order) are deleted and
    re-added with today's config, so a color change takes effect and rules never stack
    up. Conditional formats people add elsewhere on the tab are left alone.
    """
    n_auto = len(_auto_columns())
    requests: list[dict[str, Any]] = []

    def header_cells(start: int, end: int, bg: str, note: str) -> dict[str, Any]:
        return {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": start, "endColumnIndex": end},
            "cell": {"note": note, "userEnteredFormat": {
                "backgroundColorStyle": {"rgbColor": rgb(bg)},
                "textFormat": {"bold": True, "foregroundColorStyle": {"rgbColor": rgb(config.HEADER_TEXT)}},
                "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE",
                "padding": {"top": 6, "bottom": 6, "left": 8, "right": 8}}},
            "fields": "note,userEnteredFormat(backgroundColorStyle,textFormat,wrapStrategy,"
                      "verticalAlignment,padding)",
        }}

    requests.append(header_cells(0, n_auto, config.HEADER_BG, AUTO_NOTE))
    requests.append(header_cells(n_auto, len(header) - 1, TRACKING_HEADER_BG, TRACKING_NOTE))

    # Body: wrap long text, top-aligned, a little padding.
    requests.append({"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(header)},
        "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP",
                                       "padding": {"top": 4, "bottom": 4, "left": 6, "right": 6}}},
        "fields": "userEnteredFormat(wrapStrategy,verticalAlignment,padding)",
    }})

    status_cols = [header.index(c) for c in config.DEVELOPMENT_STATUS_COLUMNS if c in header]
    widths = {COMPANY: 200, STAGE: 220, "Comments": 320}
    for i, name in enumerate(header[:-1]):
        width = 130 if i in status_cols else widths.get(name, 220 if name.endswith("Description") else 160)
        width = max(width, header_column_width(name))  # never break a header word mid-way
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": width}, "fields": "pixelSize"}})
    requests.append({"updateDimensionProperties": {  # Deal ID: needed for matching, not for people
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                  "startIndex": len(header) - 1, "endIndex": len(header)},
        "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})

    # Status colors: delete our old rules (highest index first, so indexes stay valid),
    # then add one rule per status covering all status columns.
    managed = [i for i, rule in enumerate(existing_rules) if _is_status_rule(rule)]
    for index in sorted(managed, reverse=True):
        requests.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": index}})
    if status_cols:
        ranges = [_col_range(sheet_id, col) for col in status_cols]
        for status, color in config.DEVELOPMENT_STATUS_COLORS.items():
            requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
                "ranges": ranges,
                "booleanRule": {
                    "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": status}]},
                    "format": {"backgroundColorStyle": {"rgbColor": rgb(color)}, "textFormat": {"bold": True}},
                },
            }}})
    return requests
