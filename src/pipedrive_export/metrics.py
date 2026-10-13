"""Metric registry and the layout of the hidden _metrics and _history tabs. No I/O.

Every metric is a small function: records in, a scalar or a table out.

  * Scalars go in the Summary block (columns A-C of _metrics) as key/value rows.
    The Dashboard looks them up BY KEY (VLOOKUP), so their row order never matters.
  * Tables go in fixed blocks: fixed left column, fixed number of reserved rows.
    Dashboard charts point at these exact ranges, so a block must never move or
    shrink. Add new blocks to the right of the last one.

Adding a metric: write the function here with @scalar or @table, then show it on
the Dashboard (dashboard.py, or by hand in the sheet).
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pipedrive_export import config
from pipedrive_export.fx import FxRate
from pipedrive_export.transform import Record, normalize


class MetricsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Context:
    portfolio: list[Record]
    pipeline: list[Record]
    now: dt.datetime  # timezone-aware, in config.TIMEZONE
    fx: FxRate

    @property
    def active(self) -> list[Record]:
        """Open deals that are neither Portfolio nor a hidden stage (YBD)."""
        hidden = {normalize(s) for s in config.DASHBOARD_HIDDEN_STAGES}
        return [r for r in self.pipeline if normalize(r.stage) not in hidden]

    @property
    def all(self) -> list[Record]:
        return self.portfolio + self.pipeline


# --- Registry --------------------------------------------------------------

@dataclass(frozen=True)
class ScalarSpec:
    key: str
    description: str
    fn: Callable[[Context], Any]


@dataclass(frozen=True)
class TableSpec:
    key: str
    column: int  # 0-based left column in _metrics (0 = A)
    header: list[str]
    reserve: int  # data rows reserved below the header
    fn: Callable[[Context], list[list[Any]]]


SCALARS: list[ScalarSpec] = []
TABLES: list[TableSpec] = []


def scalar(key: str, description: str):
    def register(fn):
        SCALARS.append(ScalarSpec(key, description, fn))
        return fn
    return register


def table(key: str, column: int, header: list[str], reserve: int):
    def register(fn):
        TABLES.append(TableSpec(key, column, header, reserve, fn))
        return fn
    return register


SUMMARY_HEADER = ["Key", "Value", "Description"]  # columns A-C


def table_spec(key: str) -> TableSpec:
    return next(t for t in TABLES if t.key == key)


# --- Scalars ---------------------------------------------------------------

@scalar("generated_at", "When this data was pulled from Pipedrive (Israel time)")
def generated_at(ctx: Context) -> str:
    return ctx.now.strftime("%Y-%m-%d %H:%M")


@scalar("portfolio_count", f"Open deals at stage {config.PORTFOLIO_STAGE}")
def portfolio_count(ctx: Context) -> int:
    return len(ctx.portfolio)


@scalar("active_pipeline", "Open deals excluding Portfolio and hidden stages")
def active_pipeline(ctx: Context) -> int:
    return len(ctx.active)


@scalar("hidden_stage_deals", f"Open deals in hidden stages {config.DASHBOARD_HIDDEN_STAGES}")
def hidden_stage_deals(ctx: Context) -> int:
    return len(ctx.pipeline) - len(ctx.active)


@scalar("new_deals_this_month", f"Open deals in {config.PIPELINE_NAME} added this calendar month")
def new_deals_this_month(ctx: Context) -> int:
    return sum(1 for r in ctx.all if r.add_time and _same_month(r.add_time, ctx.now))


def _same_month(ts: dt.datetime, now: dt.datetime) -> bool:
    local = ts.astimezone(now.tzinfo)
    return (local.year, local.month) == (now.year, now.month)


@scalar("usd_ils_rate", "ILS per 1 USD used for every conversion in this run")
def usd_ils_rate(ctx: Context) -> float:
    return ctx.fx.usd_ils


@scalar("rate_date", "Date of the exchange rate")
def rate_date(ctx: Context) -> str:
    return ctx.fx.as_of


@scalar("rate_source", "Where the exchange rate came from")
def rate_source(ctx: Context) -> str:
    return ctx.fx.source


def parse_holding(value: Any) -> float | None:
    """Direct holding as a fraction of 1.

    "15%", "15", 15 and 0.15 all mean 15% -> 0.15. Rule: a % sign, or a number above 1,
    is a percentage; a number of 1 or less is already a fraction (so "1" means 100%).
    Returns None when blank. Raises ValueError when unreadable or outside 0-100%.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"not a holding: {value!r}")
    text = str(value).strip()
    is_percent = text.endswith("%")
    number = float(text.rstrip("%").strip())
    if is_percent or number > 1:
        number /= 100
    if not 0 <= number <= 1:
        raise ValueError(f"holding outside 0-100%: {value!r}")
    return number


def holding_fraction(record: Record) -> float | None:
    """Parsed Direct Holding; None if blank or unreadable (callers treat it as 0)."""
    try:
        return parse_holding(record.values.get(config.DIRECT_HOLDING_FIELD))
    except ValueError:
        return None


def money_totals(ctx: Context, key: str) -> tuple[float, float, int]:
    """(USD total, ILS total, companies counted) for one MONEY_METRICS key.

    Every USD or ILS amount is converted at the run's rate, so both totals cover all
    reported companies. For ADJUSTED_BY_HOLDING keys each amount is multiplied by the
    company's Direct Holding; a company without a readable holding adds 0 and is not
    counted as reported.
    """
    column = config.MONEY_METRICS[key]
    usd = ils = 0.0
    counted = 0
    for r in ctx.portfolio:
        amount = r.values.get(column)
        if amount is None:
            continue
        converted = ctx.fx.convert(amount, r.values.get(column + config.CURRENCY_COLUMN_SUFFIX))
        if converted is None:
            continue  # other currency: reported by other_currency_amounts
        factor = 1.0
        if key in config.ADJUSTED_BY_HOLDING:
            holding = holding_fraction(r)
            if holding is None:
                continue
            factor = holding
        usd += converted[0] * factor
        ils += converted[1] * factor
        counted += 1
    return usd, ils, counted


def _register_money(key: str, column: str) -> None:
    what = f"{column} x {config.DIRECT_HOLDING_FIELD}" if key in config.ADJUSTED_BY_HOLDING else column
    scalar(f"{key}_usd", f"{what}, portfolio, total in USD")(lambda ctx: money_totals(ctx, key)[0])
    scalar(f"{key}_ils", f"{what}, portfolio, total in ILS")(lambda ctx: money_totals(ctx, key)[1])
    scalar(f"{key}_n", f"Portfolio companies included in {key}")(lambda ctx: money_totals(ctx, key)[2])


for _key, _column in config.MONEY_METRICS.items():
    _register_money(_key, _column)


@scalar("other_currency_amounts", "Portfolio money amounts in a currency other than USD/ILS (not included)")
def other_currency_amounts(ctx: Context) -> int:
    return sum(
        1 for r in ctx.portfolio for column in config.MONEY_METRICS.values()
        if r.values.get(column) is not None
        and r.values.get(column + config.CURRENCY_COLUMN_SUFFIX) not in config.EXPECTED_CURRENCIES
    )


@scalar("direct_holding_unreadable", f"Portfolio companies whose {config.DIRECT_HOLDING_FIELD} could not be read")
def direct_holding_unreadable(ctx: Context) -> int:
    return sum(1 for r in ctx.portfolio
               if r.values.get(config.DIRECT_HOLDING_FIELD) is not None and holding_fraction(r) is None)


# --- Tables ----------------------------------------------------------------

def dashboard_stages(active: list[Record]) -> list[str]:
    """Stages shown on the Dashboard, in business order; unlisted stages last."""
    hidden = {normalize(s) for s in [config.PORTFOLIO_STAGE, *config.DASHBOARD_HIDDEN_STAGES]}
    listed = [s for s in config.STAGE_ORDER if normalize(s) not in hidden]
    known = {normalize(s) for s in listed}
    extra = sorted({r.stage for r in active if normalize(r.stage) not in known}, key=normalize)
    return listed + extra


def stage_counts(active: list[Record]) -> dict[str, int]:
    counts = Counter(normalize(r.stage) for r in active)
    return {s: counts.get(normalize(s), 0) for s in dashboard_stages(active)}


@table("stages", column=4, header=["Stage", "Deals"], reserve=15)  # E:F
def stages(ctx: Context) -> list[list[Any]]:
    return [[stage, n] for stage, n in stage_counts(ctx.active).items()]


NO_SECTOR = "(No sector)"
OTHER_SECTORS = "Other sectors"


def _sectors(record: Record) -> list[str]:
    label = record.values.get(config.LABEL_FIELD)
    if not label:
        return [NO_SECTOR]
    return [s.strip() for s in label.split(config.LABEL_SEPARATOR.strip()) if s.strip()]


@table("sectors", column=7, header=["Sector", "Active pipeline", "Portfolio"], reserve=30)  # H:J
def sectors(ctx: Context) -> list[list[Any]]:
    """A deal with two sectors counts in both. Largest first; no-sector last."""
    active = Counter(s for r in ctx.active for s in _sectors(r))
    portfolio = Counter(s for r in ctx.portfolio for s in _sectors(r))
    names = sorted((set(active) | set(portfolio)) - {NO_SECTOR},
                   key=lambda s: (-(active[s] + portfolio[s]), normalize(s)))
    rows = [[s, active[s], portfolio[s]] for s in names]

    limit = table_spec("sectors").reserve - 1  # keep one row for "(No sector)"
    if len(rows) > limit:  # fold the smallest into one row rather than overflow the block
        kept, folded = rows[:limit - 1], rows[limit - 1:]
        rows = kept + [[OTHER_SECTORS, sum(r[1] for r in folded), sum(r[2] for r in folded)]]
    if active[NO_SECTOR] or portfolio[NO_SECTOR]:
        rows.append([NO_SECTOR, active[NO_SECTOR], portfolio[NO_SECTOR]])
    return rows


NO_INVESTMENT_DATA = "No investment amounts in Pipedrive yet"
OTHER_COMPANIES = "Other companies"


@table("capital_allocation", column=11, header=["Company", "Fund invested (USD)"], reserve=30)  # L:M
def capital_allocation(ctx: Context) -> list[list[Any]]:
    """The fund's investment per portfolio company, in USD (ILS converted at the run's
    rate), largest first. Companies with no amount, a zero amount or another currency are
    left out, so the chart shows only real investments. With none at all, one labelled
    row with no value makes the chart say so instead of rendering empty."""
    column = config.MONEY_METRICS["fs_invested"]
    rows = []
    for r in ctx.portfolio:
        amount = r.values.get(column)
        converted = ctx.fx.convert(amount, r.values.get(column + config.CURRENCY_COLUMN_SUFFIX)) if amount else None
        if converted and converted[0] > 0:
            rows.append([r.title, round(converted[0])])
    if not rows:
        return [[NO_INVESTMENT_DATA, None]]
    rows.sort(key=lambda row: (-row[1], normalize(row[0])))

    limit = table_spec("capital_allocation").reserve
    if len(rows) > limit:  # fold the smallest into one row rather than overflow the block
        rows = rows[:limit - 1] + [[OTHER_COMPANIES, sum(row[1] for row in rows[limit - 1:])]]
    return rows


# --- _metrics grid ---------------------------------------------------------

def compute_scalars(ctx: Context) -> dict[str, Any]:
    return {s.key: s.fn(ctx) for s in SCALARS}


def render_metrics(ctx: Context) -> list[list[Any]]:
    """The whole _metrics tab as a rectangular grid (None = blank). Always the same size."""
    width = max(3, *(t.column + len(t.header) for t in TABLES))
    height = 1 + max(len(SCALARS), *(t.reserve for t in TABLES))
    grid: list[list[Any]] = [[None] * width for _ in range(height)]

    def place(row: int, col: int, values: list[Any]) -> None:
        grid[row][col:col + len(values)] = values

    place(0, 0, SUMMARY_HEADER)
    for i, (spec, value) in enumerate(zip(SCALARS, compute_scalars(ctx).values()), start=1):
        place(i, 0, [spec.key, value, spec.description])

    for t in TABLES:
        rows = t.fn(ctx)
        if len(rows) > t.reserve:
            raise MetricsError(f"_metrics block {t.key!r} has {len(rows)} rows; only {t.reserve} reserved")
        place(0, t.column, t.header)
        for i, row in enumerate(rows, start=1):
            place(i, t.column, row)
    return grid


# --- _history --------------------------------------------------------------

HISTORY_FIXED = ["Month", "Portfolio", "Active pipeline", "New deals",
                 "FS invested USD", "FS invested ILS", "USD/ILS rate"]


def history_row(ctx: Context) -> dict[str, Any]:
    values = compute_scalars(ctx)
    row = {
        "Month": ctx.now.strftime("%Y-%m"),
        "Portfolio": values["portfolio_count"],
        "Active pipeline": values["active_pipeline"],
        "New deals": values["new_deals_this_month"],
        "FS invested USD": values["fs_invested_usd"],
        "FS invested ILS": values["fs_invested_ils"],
        "USD/ILS rate": values["usd_ils_rate"],
    }
    row.update(stage_counts(ctx.active))
    return row


def upsert_history(existing: list[list[Any]], row: dict[str, Any]) -> list[list[Any]]:
    """Merge this month's row into the history table (header + rows).

    Replaces the row for the same month (reruns, including on-demand runs: the month's
    row always holds the latest run), otherwise appends; sorted by month.
    Existing columns never move; a new stage or field becomes a new column at the end,
    so chart ranges stay valid. Stages that disappear keep their column, blank from then on.
    """
    header = [str(h) for h in existing[0]] if existing else list(HISTORY_FIXED)
    for key in row:
        if key not in header:
            header.append(key)

    by_month: dict[str, list[Any]] = {}
    for old in existing[1:]:
        padded = (list(old) + [None] * len(header))[:len(header)]
        padded = [None if v == "" else v for v in padded]
        if padded[0] is not None:
            by_month[str(padded[0])] = padded
    by_month[row["Month"]] = [row.get(h) for h in header]

    rows = [by_month[m] for m in sorted(by_month)]
    if len(rows) > config.HISTORY_MAX_MONTHS:
        raise MetricsError(f"_history has more than {config.HISTORY_MAX_MONTHS} months; "
                           "raise HISTORY_MAX_MONTHS and extend the trend chart ranges")
    return [header, *rows]
