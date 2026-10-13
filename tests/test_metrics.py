import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from pipedrive_export import config, dashboard, metrics
from pipedrive_export.fx import FALLBACK_SOURCE, FxRate
from pipedrive_export.metrics import Context, parse_holding, table_spec
from pipedrive_export.transform import Record

TZ = ZoneInfo(config.TIMEZONE)
NOW = dt.datetime(2026, 10, 1, 7, 17, tzinfo=TZ)
RATE = FxRate(usd_ils=4.0, as_of="2026-10-01", source="Bank of Israel")  # round number: easy math
FS = config.MONEY_METRICS["fs_invested"]
TI = config.MONEY_METRICS["portfolio_valuation"]  # Total Invested (x Direct Holding)
HOLD = config.DIRECT_HOLDING_FIELD
CUR = config.CURRENCY_COLUMN_SUFFIX


def rec(i, stage, label=None, added=None, **values):
    base = {c: None for c in config.COLUMNS}
    base.update({config.TITLE_FIELD: f"Deal {i}", config.STAGE_FIELD: stage, config.LABEL_FIELD: label})
    base.update(values)
    return Record(deal_id=i, values=base, add_time=added)


def ctx(portfolio=(), pipeline=(), fx=RATE):
    return Context(portfolio=list(portfolio), pipeline=list(pipeline), now=NOW, fx=fx)


def scalars(c):
    return metrics.compute_scalars(c)


PORTFOLIO = [
    rec(1, "Portfolio", "Food As Health",
        **{FS: 100_000, FS + CUR: "USD", TI: 1_000_000, TI + CUR: "USD", HOLD: "15%", "Investor A Holdings": 15}),
    rec(2, "Portfolio", "Food As Health; Delivery Systems",
        **{FS: 400_000, FS + CUR: "ILS", TI: 2_000_000, TI + CUR: "ILS", HOLD: "20"}),
    rec(3, "Portfolio", None,
        **{FS: 50_000, FS + CUR: "USD", TI: 500_000, TI + CUR: "USD", "Investor A Holdings": 0}),  # no holding
    rec(4, "Portfolio", "Delivery Systems", **{HOLD: 0.1}),  # holding but no amount
]
PIPELINE = [
    rec(10, "DD Process", "Delivery Systems"),
    rec(11, "Monitoring", "Food As Health"),
    rec(12, "YBD", "Food As Health"),
    rec(13, "YBD", None),
]


def test_counts_hide_ybd_but_keep_monitoring():
    s = scalars(ctx(PORTFOLIO, PIPELINE))
    assert s["portfolio_count"] == 4
    assert s["active_pipeline"] == 2
    assert s["hidden_stage_deals"] == 2


def test_rate_is_recorded():
    s = scalars(ctx(PORTFOLIO))
    assert (s["usd_ils_rate"], s["rate_date"], s["rate_source"]) == (4.0, "2026-10-01", "Bank of Israel")


def test_money_is_converted_into_unified_usd_and_ils_totals():
    s = scalars(ctx(PORTFOLIO, PIPELINE))
    # 100k USD + 400k ILS (= 100k USD) + 50k USD
    assert s["fs_invested_usd"] == pytest.approx(250_000)
    assert s["fs_invested_ils"] == pytest.approx(1_000_000)
    assert s["fs_invested_n"] == 3


def test_portfolio_valuation_is_total_invested_times_direct_holding():
    s = scalars(ctx(PORTFOLIO))
    # deal 1: 1M USD x 15% = 150k USD; deal 2: 2M ILS (= 500k USD) x 20% = 100k USD;
    # deal 3 has no holding: adds 0 and is not counted.
    assert s["portfolio_valuation_usd"] == pytest.approx(250_000)
    assert s["portfolio_valuation_ils"] == pytest.approx(1_000_000)
    assert s["portfolio_valuation_n"] == 2


def test_old_valuation_and_total_invested_metrics_are_gone():
    keys = {spec.key for spec in metrics.SCALARS}
    assert not any(k.startswith(("valuation_", "total_invested_")) for k in keys)


def test_no_reports_gives_zero_totals_and_zero_count():
    s = scalars(ctx(PORTFOLIO[3:]))  # only the company with a holding but no amounts
    assert (s["fs_invested_usd"], s["fs_invested_ils"], s["fs_invested_n"]) == (0, 0, 0)
    assert s["portfolio_valuation_n"] == 0


def test_other_currency_is_counted_not_converted():
    odd = rec(5, "Portfolio", **{FS: 10, FS + CUR: "EUR"})
    s = scalars(ctx(PORTFOLIO + [odd]))
    assert s["other_currency_amounts"] == 1
    assert s["fs_invested_usd"] == pytest.approx(250_000)


@pytest.mark.parametrize("raw, expected", [
    ("15%", 0.15), ("15", 0.15), (15, 0.15), (0.15, 0.15), ("0.15", 0.15), (" 12.5 % ", 0.125),
    ("100%", 1.0), (1, 1.0), (0, 0.0), (None, None), ("", None), ("  ", None),
])
def test_parse_holding(raw, expected):
    assert parse_holding(raw) == (pytest.approx(expected) if expected is not None else None)


@pytest.mark.parametrize("raw", ["n/a", "150%", -5, True])
def test_parse_holding_rejects_unreadable(raw):
    with pytest.raises(ValueError):
        parse_holding(raw)


def test_unreadable_holding_counts_as_zero_and_is_reported():
    bad = rec(6, "Portfolio", **{TI: 1_000, TI + CUR: "USD", HOLD: "about a third"})
    s = scalars(ctx(PORTFOLIO + [bad]))
    assert s["portfolio_valuation_usd"] == pytest.approx(250_000)
    assert s["direct_holding_unreadable"] == 1


def test_new_deals_this_month_uses_israel_time():
    utc = dt.timezone.utc
    deals = [
        rec(20, "DD Process", added=dt.datetime(2026, 10, 5, 9, 0, tzinfo=utc)),
        rec(21, "YBD", added=dt.datetime(2026, 9, 30, 22, 30, tzinfo=utc)),  # Oct 1 01:30 in Israel
        rec(22, "DD Process", added=dt.datetime(2026, 9, 30, 20, 0, tzinfo=utc)),  # Sep 30 in Israel
        rec(23, "DD Process", added=None),
    ]
    assert scalars(ctx(pipeline=deals))["new_deals_this_month"] == 2


def test_stage_table_in_business_order_without_hidden_stages():
    rows = metrics.stages(ctx(PORTFOLIO, PIPELINE + [rec(14, "Brand New Stage")]))
    names = [r[0] for r in rows]
    assert "YBD" not in names and config.PORTFOLIO_STAGE not in names
    assert names[:len(config.STAGE_ORDER) - 1] == [s for s in config.STAGE_ORDER if s != "YBD"]
    assert names[-1] == "Brand New Stage"
    assert dict(rows)["DD Process"] == 1 and dict(rows)["Stage Gate I - Introduction"] == 0


def test_sectors_split_multi_labels_and_exclude_ybd():
    rows = {r[0]: (r[1], r[2]) for r in metrics.sectors(ctx(PORTFOLIO, PIPELINE))}
    assert rows["Food As Health"] == (1, 2)   # active: Monitoring deal; portfolio: deals 1, 2
    assert rows["Delivery Systems"] == (1, 2)
    assert rows[metrics.NO_SECTOR] == (0, 1)  # the YBD deal without a sector is not counted
    assert list(rows)[-1] == metrics.NO_SECTOR


def test_sectors_fold_into_other_when_block_is_full():
    many = [rec(100 + i, "DD Process", f"Sector {i:02d}") for i in range(40)]
    rows = metrics.sectors(ctx(pipeline=many))
    assert len(rows) <= table_spec("sectors").reserve
    assert rows[-1][0] == metrics.OTHER_SECTORS
    assert sum(r[1] for r in rows) == 40


def test_capital_allocation_per_company_in_usd_largest_first():
    rows = metrics.capital_allocation(ctx(PORTFOLIO))
    # deal 1: 100k USD; deal 2: 400k ILS = 100k USD (tie -> by name); deal 3: 50k USD; deal 4: none
    assert rows == [["Deal 1", 100_000], ["Deal 2", 100_000], ["Deal 3", 50_000]]


def test_capital_allocation_skips_blank_zero_and_other_currencies():
    extra = [rec(7, "Portfolio", **{FS: 0, FS + CUR: "USD"}), rec(8, "Portfolio", **{FS: 9, FS + CUR: "EUR"})]
    names = [row[0] for row in metrics.capital_allocation(ctx(PORTFOLIO + extra))]
    assert "Deal 7" not in names and "Deal 8" not in names and "Deal 4" not in names


def test_capital_allocation_with_no_data_says_so():
    assert metrics.capital_allocation(ctx(PORTFOLIO[3:])) == [[metrics.NO_INVESTMENT_DATA, None]]
    assert metrics.capital_allocation(ctx()) == [[metrics.NO_INVESTMENT_DATA, None]]


def test_capital_allocation_folds_overflow():
    many = [rec(200 + i, "Portfolio", **{FS: 1000 + i, FS + CUR: "USD"}) for i in range(40)]
    rows = metrics.capital_allocation(ctx(many))
    assert len(rows) == table_spec("capital_allocation").reserve
    assert rows[-1][0] == metrics.OTHER_COMPANIES
    assert sum(r[1] for r in rows) == sum(1000 + i for i in range(40))


def test_metrics_grid_is_fixed_size_and_blocks_are_in_place():
    small = metrics.render_metrics(ctx(PORTFOLIO[:1]))
    large = metrics.render_metrics(ctx(PORTFOLIO, PIPELINE))
    assert len(small) == len(large) and len(small[0]) == len(large[0])
    assert all(len(row) == len(large[0]) for row in large)
    t = table_spec("stages")
    assert large[0][t.column:t.column + 2] == ["Stage", "Deals"]
    keys = [row[0] for row in large[1:] if row[0]]
    assert {"portfolio_count", "fs_invested_usd", "usd_ils_rate", "rate_date"} <= set(keys)
    assert "completeness" not in {t.key for t in metrics.TABLES}


def test_history_upsert_replaces_same_month_and_keeps_columns():
    c = ctx(PORTFOLIO, PIPELINE)
    first = metrics.upsert_history([], metrics.history_row(c))
    assert first[0][:len(metrics.HISTORY_FIXED)] == metrics.HISTORY_FIXED
    assert [r[0] for r in first[1:]] == ["2026-10"]
    assert first[1][first[0].index("USD/ILS rate")] == 4.0

    # Values come back from the sheet with blanks as "" and trailing blanks trimmed.
    readback = [first[0], ["2026-09", 3, 1, 0, "", ""]] + [[v if v is not None else "" for v in r] for r in first[1:]]
    again = metrics.upsert_history(readback, metrics.history_row(c))
    assert [r[0] for r in again[1:]] == ["2026-09", "2026-10"]
    assert again[1][4] is None  # "" read back becomes blank, not text


def test_history_adds_new_columns_at_the_end():
    old_header = ["Month", "Portfolio", "Active pipeline", "New deals", "FS invested USD", "FS invested ILS"]
    later = metrics.upsert_history([old_header, ["2026-09", 3, 1, 0, 5, 20]], metrics.history_row(ctx(PORTFOLIO)))
    assert later[0][:len(old_header)] == old_header
    assert "USD/ILS rate" in later[0][len(old_header):]


def _dashboard_requests():
    header = metrics.upsert_history([], metrics.history_row(ctx(PORTFOLIO, PIPELINE)))[0]
    ids = {config.TAB_DASHBOARD: 1, config.TAB_METRICS: 2, config.TAB_HISTORY: 3}
    return dashboard.build_requests(ids, header)


def _formulas(requests):
    return [r["updateCells"]["rows"][0]["values"][0]["userEnteredValue"].get("formulaValue", "")
            for r in requests if "updateCells" in r]


def test_dashboard_has_four_charts_in_a_gapless_grid():
    charts = [r["addChart"]["chart"] for r in _dashboard_requests() if "addChart" in r]
    titles = [c["spec"]["title"] for c in charts]
    assert len(charts) == 4
    assert not any("per month" in t and "stage" in t for t in titles)  # stage trend removed
    assert not any("Co-investors" in t for t in titles)
    assert any(t.startswith("Capital allocation") for t in titles)
    anchors = {(c["position"]["overlayPosition"]["anchorCell"]["rowIndex"],
                c["position"]["overlayPosition"]["anchorCell"]["columnIndex"]) for c in charts}
    rows, cols = {r for r, _ in anchors}, {c for _, c in anchors}
    assert len(anchors) == 4 and len(rows) == 2 and len(cols) == 2  # a full 2 x 2 grid
    assert not any("completeness" in f.lower() for f in _formulas(_dashboard_requests()))


def test_dashboard_money_boxes_are_fs_invested_and_portfolio_valuation():
    labels = [r["updateCells"]["rows"][0]["values"][0]["userEnteredValue"].get("stringValue", "")
              for r in _dashboard_requests() if "updateCells" in r]
    money = [label for label in labels if label.endswith(("(USD)", "(ILS)"))]
    assert money == ["Fund Capital Invested (USD)", "Fund Capital Invested (ILS)",
                     "Portfolio valuation (USD)", "Portfolio valuation (ILS)"]
    assert not any("Total invested" in label for label in labels)


def test_dashboard_without_logo_has_no_image_cell(monkeypatch):
    monkeypatch.setattr(config, "LOGO_URL", "")
    assert not any(f.startswith("=IMAGE(") for f in _formulas(_dashboard_requests()))


def test_dashboard_logo_boxes_and_axes(monkeypatch):
    monkeypatch.setattr(config, "LOGO_URL", "https://example.com/logo.png")
    requests = _dashboard_requests()
    logo = next(f for f in _formulas(requests) if f.startswith("=IMAGE("))
    assert config.LOGO_URL in logo and not config.LOGO_URL.endswith(".svg")  # Sheets can't show SVG
    assert sum(1 for r in requests if "updateBorders" in r) == 7  # 3 count boxes + 4 money boxes
    cells = [r["updateCells"]["rows"][0]["values"][0]["userEnteredFormat"] for r in requests if "updateCells" in r]
    assert all(c.get("wrapStrategy") == "WRAP" and c.get("padding") for c in cells)
    for r in requests:
        if "addChart" in r:
            value_axis = r["addChart"]["chart"]["spec"]["basicChart"]["axis"][0]
            assert value_axis["viewWindowOptions"]["viewWindowMin"] == 0
    sectors = next(r["addChart"]["chart"] for r in requests
                   if "addChart" in r and r["addChart"]["chart"]["spec"]["title"].startswith("Sectors"))
    assert sectors["spec"]["basicChart"]["legendPosition"] == "BOTTOM_LEGEND"
    assert sectors["position"]["overlayPosition"]["heightPixels"] >= 450


def test_dashboard_formulas_only_look_up_existing_metrics():
    formulas = _formulas(_dashboard_requests())
    looked_up = {f.split('VLOOKUP("')[i].split('"')[0] for f in formulas for i in range(1, f.count("VLOOKUP(") + 1)}
    assert looked_up <= {s.key for s in metrics.SCALARS}
    assert {"usd_ils_rate", "rate_date"} <= looked_up
    rate_badge = next(f for f in formulas if "Exchange rate" in f)
    assert "1 USD = ₪" in rate_badge and FALLBACK_SOURCE in rate_badge
    assert not any('"–"' in f for f in formulas)  # money boxes never show a dash
