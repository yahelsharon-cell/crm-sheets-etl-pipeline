"""Pure transformation: Pipedrive deals -> typed, segmented, sorted records.

No I/O. Inputs are the raw API dicts plus lookup tables; outputs are Python values
of the right type (str, int, float, datetime.date) or None. None means "no data"
and is never turned into 0 or "" here; zero is a real value.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pipedrive_export import config

log = logging.getLogger(__name__)


class TransformError(RuntimeError):
    """A data or configuration problem that must stop the run."""


def normalize(name: Any) -> str:
    """Comparison form of a name: trimmed, inner whitespace collapsed, case-folded."""
    return re.sub(r"\s+", " ", str(name)).strip().casefold()


@dataclass(frozen=True)
class Field:
    name: str  # column header: as written in config.COLUMNS, or an EXTRA_COLUMNS name
    key: str  # API key: built-in name (e.g. "stage_id") or 40-char hash
    field_type: str
    options: dict[int, str] = field(default_factory=dict)  # option ID -> label
    tabs: frozenset[str] | None = None  # EXTRA_COLUMNS: tabs showing it; None = standard column

    @property
    def columns(self) -> list[str]:
        if self.field_type == "monetary":
            return [self.name, self.name + config.CURRENCY_COLUMN_SUFFIX]
        return [self.name]


@dataclass(frozen=True)
class Lookups:
    stages: dict[int, str]  # stage ID -> name, for the exported pipeline only
    orgs: dict[int, str] = field(default_factory=dict)
    persons: dict[int, str] = field(default_factory=dict)
    users: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Record:
    deal_id: int
    values: dict[str, Any]  # column name -> typed value or None
    add_time: dt.datetime | None = None  # when the deal was created (UTC); used by metrics only

    @property
    def title(self) -> str:
        return self.values[config.TITLE_FIELD]

    @property
    def stage(self) -> str:
        return self.values[config.STAGE_FIELD]


@dataclass(frozen=True)
class Segments:
    portfolio_columns: list[str]  # Portfolio Overview tab
    pipeline_columns: list[str]  # Pipeline tab
    portfolio: list[Record]
    pipeline: list[Record]


# --- Field resolution ------------------------------------------------------

def _meta_options(meta: dict[str, Any]) -> dict[int, str]:
    """Option ID -> label from a field's metadata (dropdowns, labels)."""
    return {int(o["id"]): str(o["label"]) for o in meta.get("options") or []
            if str(o.get("id", "")).lstrip("-").isdigit()}


def resolve_fields(names: Sequence[str], field_meta: Iterable[dict[str, Any]],
                   extras: Sequence[dict[str, Any]] = ()) -> list[Field]:
    """Match configured columns to Pipedrive dealFields metadata.

    `names` (config.COLUMNS) match by name. `extras` (config.EXTRA_COLUMNS) match their
    "source" by API key first, then by name. Fails listing every problem at once, so
    one run shows them all.
    """
    field_meta = list(field_meta)
    by_key = {meta["key"]: meta for meta in field_meta}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for meta in field_meta:
        by_name.setdefault(normalize(meta.get("name", "")), []).append(meta)

    resolved: list[Field] = []
    missing, ambiguous, invalid = [], [], []

    def lookup(source: str) -> dict[str, Any] | None:
        if source in by_key:
            return by_key[source]
        matches = by_name.get(normalize(source), [])
        if not matches:
            missing.append(source)
        elif len(matches) > 1:
            ambiguous.append(f"{source!r} (keys: {', '.join(m['key'] for m in matches)})")
        else:
            return matches[0]
        return None

    for name in names:
        meta = lookup(name)
        if meta:
            resolved.append(Field(name=name, key=meta["key"], field_type=meta.get("field_type", ""),
                                  options=_meta_options(meta)))

    for spec in extras:
        source, tabs = str(spec.get("source", "")).strip(), spec.get("tabs") or []
        bad_tabs = [t for t in tabs if t not in config.DATA_TABS]
        if not source or not tabs or bad_tabs:
            invalid.append(f"{spec!r} (needs a 'source' and 'tabs' from {config.DATA_TABS})")
            continue
        meta = lookup(source)
        if meta:
            resolved.append(Field(name=str(spec.get("name") or meta["name"]).strip(), key=meta["key"],
                                  field_type=meta.get("field_type", ""), options=_meta_options(meta),
                                  tabs=frozenset(tabs)))

    seen: dict[str, str] = {}
    duplicates = []
    for column in columns_for(resolved):
        if normalize(column) in seen:
            duplicates.append(column)
        seen[normalize(column)] = column

    problems = []
    if missing:
        problems.append("not found in Pipedrive deal fields: " + ", ".join(repr(n) for n in missing))
    if ambiguous:
        problems.append("matches more than one Pipedrive field: " + ", ".join(ambiguous))
    if invalid:
        problems.append("invalid EXTRA_COLUMNS entries: " + ", ".join(invalid))
    if duplicates:
        problems.append("duplicate column names (give extras a different 'name'): " + ", ".join(duplicates))
    if problems:
        raise TransformError("Configured columns " + "; ".join(problems))

    for f in resolved:
        bad = [label for label in f.options.values() if config.LABEL_SEPARATOR.strip() in label]
        if bad:
            log.warning("Field %r has options containing the separator %r: %s",
                        f.name, config.LABEL_SEPARATOR, bad)
    return resolved


def columns_for(fields: Sequence[Field]) -> list[str]:
    return [col for f in fields for col in f.columns]


def tab_columns(fields: Sequence[Field], tab: str) -> list[str]:
    """Columns shown on one data tab: standard columns (minus Pipeline's exclusions on
    the Pipeline tab) plus the extra columns assigned to that tab, in config order."""
    columns = [col for f in fields if f.tabs is None or tab in f.tabs for col in f.columns]
    return pipeline_columns(columns) if tab == config.TAB_PIPELINE else columns


def column_kinds(fields: Sequence[Field]) -> dict[str, str]:
    """Column -> display kind (money, number, percent, date, text) for sheet formatting."""
    percent = {normalize(c) for c in config.PERCENT_COLUMNS}
    kinds = {}
    for f in fields:
        if f.field_type == "monetary":
            kinds[f.name] = "money"
            kinds[f.name + config.CURRENCY_COLUMN_SUFFIX] = "text"
        elif normalize(f.name) in percent:
            kinds[f.name] = "percent"
        elif f.field_type in ("double", "int"):
            kinds[f.name] = "number"
        elif f.field_type == "date":
            kinds[f.name] = "date"
        else:
            kinds[f.name] = "text"
    return kinds


# --- Stage validation ------------------------------------------------------

def validate_pipeline_stages(pipeline_stages: Iterable[str],
                             stage_order: Sequence[str] = config.STAGE_ORDER,
                             portfolio_stage: str = config.PORTFOLIO_STAGE) -> list[str]:
    """Check config against the pipeline's actual stages.

    Raises if the portfolio stage or any ordered stage is missing (probably renamed).
    Returns pipeline stages that are in neither list; they sort last, with a warning.
    """
    actual = {normalize(s): s for s in pipeline_stages}
    missing = [s for s in [portfolio_stage, *stage_order] if normalize(s) not in actual]
    if missing:
        raise TransformError(
            f"Stages configured but not in the pipeline: {missing}. "
            f"Pipeline stages are: {sorted(actual.values(), key=normalize)}"
        )
    known = {normalize(s) for s in [portfolio_stage, *stage_order]}
    extra = sorted((name for key, name in actual.items() if key not in known), key=normalize)
    if extra:
        log.warning("Pipeline stages not in STAGE_ORDER (sorted last on %r): %s", config.TAB_PIPELINE, extra)
    return extra


# --- Value conversion ------------------------------------------------------

# Built-in keys named differently in dealFields metadata vs the v2 deal object.
_V2_KEY = {"label": "label_ids"}


def _raw(deal: dict[str, Any], f: Field) -> Any:
    key = _V2_KEY.get(f.key, f.key)
    if key in deal:
        return deal[key]
    return (deal.get("custom_fields") or {}).get(f.key)


def _is_blank(raw: Any) -> bool:
    return raw is None or (isinstance(raw, str) and not raw.strip()) or raw == [] or raw == {}


def _ref_id(raw: Any) -> int | None:
    """ID from a reference value: 12, "12", {"value": 12} or {"id": 12}."""
    if isinstance(raw, dict):
        raw = raw.get("value", raw.get("id"))
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _number(raw: Any) -> int | float | None:
    if _is_blank(raw):
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        try:
            return float(raw.replace(",", "").strip())
        except ValueError:
            pass
    raise ValueError(f"not a number: {raw!r}")


def _date(raw: Any) -> dt.date | None:
    if _is_blank(raw):
        return None
    return dt.date.fromisoformat(str(raw).strip()[:10])


def _timestamp(raw: Any) -> dt.datetime | None:
    """Pipedrive timestamps are UTC: "2024-01-31T10:00:00Z" (v2) or "2024-01-31 10:00:00" (v1)."""
    if _is_blank(raw):
        return None
    try:
        ts = dt.datetime.fromisoformat(str(raw).strip().replace(" ", "T"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def _text(raw: Any) -> str | None:
    if _is_blank(raw):
        return None
    if isinstance(raw, dict):  # e.g. address: {"value": "...", "formatted_address": ...}
        raw = raw.get("formatted_address") or raw.get("value") or ""
    text = str(raw).strip()
    return text or None


def _option_ids(raw: Any) -> list[int]:
    if isinstance(raw, str):
        raw = [part for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list):
        raw = [raw]
    ids = [_ref_id(item) for item in raw]
    if None in ids:
        raise ValueError(f"not an option ID: {raw!r}")
    return ids  # type: ignore[return-value]


def _options(deal_id: int, f: Field, raw: Any) -> str | None:
    """Single or multi-option field: IDs -> labels, sorted, joined. Unknown IDs are skipped."""
    if _is_blank(raw):
        return None
    labels = []
    for option_id in _option_ids(raw):
        label = f.options.get(option_id)
        if label is None:
            log.warning("Deal %s: %r has unknown option ID %s (deleted?); skipped", deal_id, f.name, option_id)
        else:
            labels.append(label)
    return config.LABEL_SEPARATOR.join(sorted(labels, key=normalize)) or None


def _reference(deal_id: int, f: Field, raw: Any, names: dict[int, str]) -> str | None:
    if _is_blank(raw):
        return None
    if isinstance(raw, dict) and raw.get("name"):
        return str(raw["name"]).strip()
    ref = _ref_id(raw)
    name = names.get(ref) if ref is not None else None
    if name is None:
        log.warning("Deal %s: %r refers to unknown ID %r; left blank", deal_id, f.name, ref)
    return name


def _money(deal: dict[str, Any], f: Field, raw: Any) -> tuple[int | float | None, str | None]:
    if f.key == "value":  # built-in deal value: currency is a sibling key
        amount, currency = raw, deal.get("currency")
    elif isinstance(raw, dict):  # v2 custom monetary: {"value": 100, "currency": "USD"}
        amount, currency = raw.get("value"), raw.get("currency")
    else:  # v1 style: amount under the hash, currency under "<hash>_currency"
        custom = deal.get("custom_fields") or {}
        amount, currency = raw, custom.get(f"{f.key}_currency", deal.get(f"{f.key}_currency"))

    amount = _number(amount)
    if amount is None:
        return None, None  # a currency without an amount is just the field's default
    currency = str(currency).strip().upper() if currency else None
    if currency not in config.EXPECTED_CURRENCIES:
        log.warning("Deal %s: %r has currency %r (expected one of %s)",
                    deal.get("id"), f.name, currency, config.EXPECTED_CURRENCIES)
    return amount, currency


def _convert(deal: dict[str, Any], f: Field, lookups: Lookups) -> dict[str, Any]:
    """Column name(s) -> typed value(s) for one field of one deal."""
    deal_id = deal.get("id")
    raw = _raw(deal, f)
    t = f.field_type

    if t == "monetary":
        amount, currency = _money(deal, f, raw)
        return {f.name: amount, f.name + config.CURRENCY_COLUMN_SUFFIX: currency}
    if t == "stage":
        stage_id = _ref_id(raw)
        if stage_id not in lookups.stages:
            raise TransformError(f"Deal {deal_id}: stage ID {raw!r} is not in pipeline {config.PIPELINE_NAME!r}")
        return {f.name: lookups.stages[stage_id]}
    if t in ("double", "int"):
        value = _number(raw)
    elif t == "date":
        value = _date(raw)
    elif t in ("enum", "set") or f.key == "label":
        value = _options(deal_id, f, raw)
    elif t == "org":
        value = _reference(deal_id, f, raw, lookups.orgs)
    elif t == "people":
        value = _reference(deal_id, f, raw, lookups.persons)
    elif t == "user":
        value = _reference(deal_id, f, raw, lookups.users)
    else:  # varchar, text, phone, address, and anything unrecognised: keep as text
        value = _text(raw)
    return {f.name: value}


def referenced_ids(deals: Iterable[dict[str, Any]], fields: Sequence[Field]) -> dict[str, set[int]]:
    """IDs of organizations, persons and users referenced by the configured fields."""
    ids: dict[str, set[int]] = {"org": set(), "people": set(), "user": set()}
    for deal in deals:
        for f in fields:
            if f.field_type in ids:
                ref = _ref_id(_raw(deal, f))
                if ref is not None:
                    ids[f.field_type].add(ref)
    return ids


def build_records(deals: Iterable[dict[str, Any]], fields: Sequence[Field], lookups: Lookups) -> list[Record]:
    records, problems = [], []
    for deal in deals:
        values: dict[str, Any] = {}
        for f in fields:
            try:
                values.update(_convert(deal, f, lookups))
            except (ValueError, TypeError) as exc:
                raise TransformError(f"Deal {deal.get('id')}: field {f.name!r} ({f.field_type}): {exc}") from exc
        record = Record(deal_id=deal.get("id"), values=values, add_time=_timestamp(deal.get("add_time")))
        if record.title is None or record.stage is None:
            problems.append(record.deal_id)
        records.append(record)

    if problems:
        raise TransformError(f"Deals missing {config.TITLE_FIELD} or {config.STAGE_FIELD}: {problems}")
    return records


# --- Segmentation and sorting ----------------------------------------------

def _title_key(record: Record) -> tuple[str, int]:
    return normalize(record.title), record.deal_id  # deal ID breaks ties deterministically


def segment(records: Iterable[Record], portfolio_columns: list[str], pipeline_columns: list[str],
            stage_order: Sequence[str] = config.STAGE_ORDER,
            portfolio_stage: str = config.PORTFOLIO_STAGE) -> Segments:
    """Split on Stage only (never Label), then sort each tab."""
    portfolio_key = normalize(portfolio_stage)
    rank = {normalize(s): i for i, s in enumerate(stage_order)}

    portfolio, pipeline = [], []
    for r in records:
        (portfolio if normalize(r.stage) == portfolio_key else pipeline).append(r)

    portfolio.sort(key=_title_key)
    # Stages missing from stage_order sort after all listed stages, alphabetically.
    pipeline.sort(key=lambda r: (rank.get(normalize(r.stage), len(rank)), normalize(r.stage), *_title_key(r)))
    return Segments(portfolio_columns=portfolio_columns, pipeline_columns=pipeline_columns,
                    portfolio=portfolio, pipeline=pipeline)


def pipeline_columns(columns: Sequence[str], excluded: Sequence[str] | None = None) -> list[str]:
    """Columns for the Pipeline tab. Fails if an excluded name matches no column (a typo
    or a renamed field), so a portfolio-only column can never leak onto Pipeline silently."""
    if excluded is None:
        excluded = config.PIPELINE_EXCLUDED_COLUMNS
    by_key = {normalize(c): c for c in columns}
    unknown = [name for name in excluded if normalize(name) not in by_key]
    if unknown:
        raise TransformError(f"PIPELINE_EXCLUDED_COLUMNS names not among the exported columns: {unknown}")
    drop = {normalize(name) for name in excluded}
    return [c for c in columns if normalize(c) not in drop]


def transform(deals: Sequence[dict[str, Any]], fields: Sequence[Field], lookups: Lookups) -> Segments:
    validate_pipeline_stages(lookups.stages.values())
    records = build_records(deals, fields, lookups)
    return segment(records, tab_columns(fields, config.TAB_PORTFOLIO), tab_columns(fields, config.TAB_PIPELINE))
