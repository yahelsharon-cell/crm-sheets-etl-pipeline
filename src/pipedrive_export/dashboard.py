"""One-time Dashboard build: logo, KPI boxes and charts. Produces Sheets API requests only.

Runs only while the Dashboard tab is empty (no charts, no values). After that the
Dashboard belongs to people: edit, move or restyle anything by hand; runs never touch
it. To rebuild it from scratch, delete the Dashboard tab and run the export again.

KPI boxes are formulas that look values up in _metrics BY KEY, e.g.
    =VLOOKUP("portfolio_count", '_metrics'!$A:$B, 2, FALSE)
so they keep working however the Summary block is ordered. Charts read the fixed
blocks of _metrics and _history.
"""

from __future__ import annotations

from typing import Any

from pipedrive_export import config
from pipedrive_export.fx import FALLBACK_SOURCE
from pipedrive_export.metrics import table_spec
from pipedrive_export.sheets import rgb

INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#6b6a66"

MONEY_TITLES = {
    "fs_invested": "Fund Capital Invested",
    "portfolio_valuation": "Portfolio valuation",
}
CURRENCY_FORMATS = {"USD": '"$"#,##0', "ILS": '"₪"#,##0'}

# --- Layout (0-based rows/columns) ---------------------------------------------
# Column A is a narrow margin; B..G are the content columns (one box per column).
FIRST_COL, LAST_COL = 1, 6
COL_WIDTH_PX = 210
ROW_LOGO, ROW_AS_OF, ROW_RATE = 0, 1, 2
ROW_COUNTS, ROW_MONEY = 4, 8  # each box spans 3 rows: label, value, caption
ROW_NOTES = 11
ROW_HEIGHTS = {ROW_LOGO: 72, 3: 12, ROW_COUNTS: 34, ROW_COUNTS + 1: 50, ROW_COUNTS + 2: 42,
               7: 16, ROW_MONEY: 34, ROW_MONEY + 1: 50, ROW_MONEY + 2: 42, 12: 16}
# Charts: a 2 x 2 grid below the boxes. Chart rows keep the default ~21px row height.
CHART_W = 3 * COL_WIDTH_PX - 16
TOP_CHART_H, BOTTOM_CHART_H = 460, 360
LEFT, RIGHT = FIRST_COL, FIRST_COL + 3  # chart anchor columns: B and E
CHART_ROWS = (13, 13 + TOP_CHART_H // 21 + 2)

PADDING = {"top": 6, "bottom": 6, "left": 10, "right": 10}


def _quote(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _lookup(key: str) -> str:
    return f'VLOOKUP("{key}",{_quote(config.TAB_METRICS)}!$A:$B,2,FALSE)'


def _grid(sheet_id: int, r0: int, r1: int, c0: int, c1: int) -> dict[str, int]:
    return {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1,
            "startColumnIndex": c0, "endColumnIndex": c1}


def _fmt(size: int, color: str, bold: bool = False, background: str | None = None,
         valign: str = "MIDDLE") -> dict[str, Any]:
    fmt: dict[str, Any] = {
        "textFormat": {"fontSize": size, "bold": bold, "foregroundColorStyle": {"rgbColor": rgb(color)}},
        "verticalAlignment": valign, "wrapStrategy": "WRAP", "padding": PADDING,
    }
    if background:
        fmt["backgroundColorStyle"] = {"rgbColor": rgb(background)}
    return fmt


def _cell(sheet_id: int, row: int, col: int, value: str, fmt: dict[str, Any]) -> dict[str, Any]:
    entered = {"formulaValue": value} if value.startswith("=") else {"stringValue": value}
    return {"updateCells": {
        "start": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": col},
        "rows": [{"values": [{"userEnteredValue": entered, "userEnteredFormat": fmt}]}],
        "fields": "userEnteredValue,userEnteredFormat",
    }}


def _merge(sheet_id: int, row: int, c0: int, c1: int) -> dict[str, Any]:
    return {"mergeCells": {"range": _grid(sheet_id, row, row + 1, c0, c1), "mergeType": "MERGE_ALL"}}


def _box(sheet_id: int, row: int, col: int, label: str, value: str, caption: str,
         number_pattern: str | None = None) -> list[dict[str, Any]]:
    """A metric box: label, big value, caption, on a pale background with a border."""
    value_fmt = _fmt(22, INK, bold=True, background=config.BOX_BG)
    value_fmt["horizontalAlignment"] = "LEFT"
    if number_pattern:
        value_fmt["numberFormat"] = {"type": "NUMBER", "pattern": number_pattern}
    border = {"style": "SOLID", "colorStyle": {"rgbColor": rgb(config.BOX_BORDER)}}
    return [
        _cell(sheet_id, row, col, label, _fmt(10, INK_SECONDARY, bold=True, background=config.BOX_BG, valign="BOTTOM")),
        _cell(sheet_id, row + 1, col, value, value_fmt),
        _cell(sheet_id, row + 2, col, caption, _fmt(9, INK_MUTED, background=config.BOX_BG, valign="TOP")),
        {"updateBorders": {"range": _grid(sheet_id, row, row + 3, col, col + 1),
                           "top": border, "bottom": border, "left": border, "right": border}},
    ]


def _chart(sheet_id: int, row: int, col: int, title: str, chart_type: str,
           domain: dict[str, int], series: list[tuple[dict[str, int], str | None]], height: int,
           legend: str = "NO_LEGEND", label_size: int = 10, axis_size: int = 10) -> dict[str, Any]:
    value_axis = "BOTTOM_AXIS" if chart_type == "BAR" else "LEFT_AXIS"
    category_axis = "LEFT_AXIS" if chart_type == "BAR" else "BOTTOM_AXIS"
    series_specs = []
    for source, color in series:
        spec: dict[str, Any] = {"series": {"sourceRange": {"sources": [source]}}, "targetAxis": value_axis,
                                "dataLabel": {"type": "DATA", "textFormat": {"fontSize": label_size}}}
        if color:
            spec["colorStyle"] = {"rgbColor": rgb(color)}
        series_specs.append(spec)
    basic: dict[str, Any] = {
        "chartType": chart_type, "legendPosition": legend, "headerCount": 1,
        "domains": [{"domain": {"sourceRange": {"sources": [domain]}}}],
        "series": series_specs,
        "axis": [
            # Counts never go below 0: without this, an all-blank series draws a -1..1 axis.
            {"position": value_axis, "format": {"fontSize": axis_size},
             "viewWindowOptions": {"viewWindowMode": "EXPLICIT", "viewWindowMin": 0}},
            {"position": category_axis, "format": {"fontSize": axis_size}},
        ],
    }
    return {"addChart": {"chart": {
        "spec": {"title": title, "titleTextFormat": {"fontSize": 13, "bold": True}, "basicChart": basic},
        "position": {"overlayPosition": {
            "anchorCell": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": col},
            "widthPixels": CHART_W, "heightPixels": height}},
    }}}


def _metrics_columns(metrics_id: int, key: str) -> list[dict[str, int]]:
    """One grid range per column of a _metrics block, header row included."""
    t = table_spec(key)
    return [_grid(metrics_id, 0, t.reserve + 1, t.column + i, t.column + i + 1) for i in range(len(t.header))]


def _dimensions(dash: int) -> list[dict[str, Any]]:
    def size(dimension: str, start: int, end: int, px: int) -> dict[str, Any]:
        return {"updateDimensionProperties": {
            "range": {"sheetId": dash, "dimension": dimension, "startIndex": start, "endIndex": end},
            "properties": {"pixelSize": px}, "fields": "pixelSize"}}

    requests = [size("COLUMNS", 0, 1, 24), size("COLUMNS", FIRST_COL, LAST_COL + 1, COL_WIDTH_PX)]
    requests += [size("ROWS", row, row + 1, px) for row, px in ROW_HEIGHTS.items()]
    return requests


def build_requests(tab_ids: dict[str, int], history_header: list[str]) -> list[dict[str, Any]]:
    dash = tab_ids[config.TAB_DASHBOARD]
    metrics_id = tab_ids[config.TAB_METRICS]
    history_id = tab_ids[config.TAB_HISTORY]
    history_rows = config.HISTORY_MAX_MONTHS + 1
    hidden = ", ".join(config.DASHBOARD_HIDDEN_STAGES)

    requests: list[dict[str, Any]] = [
        {"updateSheetProperties": {"properties": {"sheetId": dash, "gridProperties": {"hideGridlines": True}},
                                   "fields": "gridProperties.hideGridlines"}},
        *_dimensions(dash),
        # Header: logo (B1, if configured), title (C1:G1), data date and exchange rate (B2:G2, B3:G3)
        *([_cell(dash, ROW_LOGO, FIRST_COL, f'=IMAGE("{config.LOGO_URL}",1)', _fmt(10, INK))]
          if config.LOGO_URL else []),
        _merge(dash, ROW_LOGO, FIRST_COL + 1, LAST_COL + 1),
        _cell(dash, ROW_LOGO, FIRST_COL + 1, "Portfolio & Pipeline Dashboard", _fmt(26, INK, bold=True)),
        _merge(dash, ROW_AS_OF, FIRST_COL, LAST_COL + 1),
        _cell(dash, ROW_AS_OF, FIRST_COL, f'="Data as of "&{_lookup("generated_at")}&" (Israel time)"',
              _fmt(10, INK_MUTED)),
        _merge(dash, ROW_RATE, FIRST_COL, LAST_COL + 1),
        _cell(dash, ROW_RATE, FIRST_COL,
              f'="Exchange rate: 1 USD = ₪"&TEXT({_lookup("usd_ils_rate")},"0.00")'
              f'&" (as of "&{_lookup("rate_date")}&", "&{_lookup("rate_source")}&")"'
              f'&IF({_lookup("rate_source")}="{FALLBACK_SOURCE}"," - live rate unavailable","")',
              _fmt(10, INK_SECONDARY, bold=True)),
    ]

    # Row 1 of boxes: counts
    requests += _box(dash, ROW_COUNTS, FIRST_COL, "Portfolio companies", f'={_lookup("portfolio_count")}',
                     f"open deals at stage {config.PORTFOLIO_STAGE}")
    requests += _box(dash, ROW_COUNTS, FIRST_COL + 1, "Active pipeline", f'={_lookup("active_pipeline")}',
                     f"open deals, excluding {config.PORTFOLIO_STAGE} and {hidden}")
    requests += _box(dash, ROW_COUNTS, FIRST_COL + 2, "New deals this month", f'={_lookup("new_deals_this_month")}',
                     f"added to {config.PIPELINE_NAME} this month, still open")

    # Row 2 of boxes: money. Each KPI as a USD total and an ILS total, both covering
    # every reported company (amounts converted at the run's exchange rate).
    col = FIRST_COL
    for key in config.MONEY_METRICS:
        included = f'={_lookup(key + "_n")}&" of "&{_lookup("portfolio_count")}&" companies included"'
        for currency in config.EXPECTED_CURRENCIES:
            requests += _box(dash, ROW_MONEY, col, f"{MONEY_TITLES.get(key, key)} ({currency})",
                             f'={_lookup(f"{key}_{currency.lower()}")}', included,
                             number_pattern=CURRENCY_FORMATS.get(currency))
            col += 1
    other, unreadable = _lookup("other_currency_amounts"), _lookup("direct_holding_unreadable")
    requests.append(_merge(dash, ROW_NOTES, FIRST_COL, LAST_COL + 1))
    requests.append(_cell(
        dash, ROW_NOTES, FIRST_COL,
        f'=TRIM(IF({other}>0,{other}&" amount(s) in other currencies not included. ","")'
        f'&IF({unreadable}>0,{unreadable}&" unreadable {config.DIRECT_HOLDING_FIELD} value(s) counted as 0.",""))',
        _fmt(9, INK_MUTED)))

    # 2 x 2 chart grid
    top, bottom = CHART_ROWS
    stage_cols = _metrics_columns(metrics_id, "stages")
    requests.append(_chart(dash, top, LEFT, f"Active deals by stage (excl. {hidden})", "BAR",
                           stage_cols[0], [(stage_cols[1], config.PALETTE[0])], TOP_CHART_H))
    sector_cols = _metrics_columns(metrics_id, "sectors")
    # Many sectors x 2 series: taller chart, smaller labels, legend out of the plot area.
    requests.append(_chart(dash, top, RIGHT, "Sectors: active pipeline vs portfolio", "BAR",
                           sector_cols[0], [(sector_cols[1], config.PALETTE[0]), (sector_cols[2], config.PALETTE[1])],
                           TOP_CHART_H, legend="BOTTOM_LEGEND", label_size=8, axis_size=9))

    month = _grid(history_id, 0, history_rows, 0, 1)
    new_col = history_header.index("New deals")
    requests.append(_chart(dash, bottom, LEFT, "New deals per month", "COLUMN", month,
                           [(_grid(history_id, 0, history_rows, new_col, new_col + 1), config.PALETTE[0])],
                           BOTTOM_CHART_H))
    # Capital allocation: bars, not a donut. With amounts filled in for only some companies,
    # a donut would present a partial set as the whole; bars stay honest as data arrives.
    # Blank/zero amounts are left out in metrics.py; with none, the chart shows a label.
    alloc_cols = _metrics_columns(metrics_id, "capital_allocation")
    requests.append(_chart(dash, bottom, RIGHT, "Capital allocation: fund investment per company (USD)",
                           "BAR", alloc_cols[0], [(alloc_cols[1], config.PALETTE[0])], BOTTOM_CHART_H,
                           axis_size=9))
    return requests
