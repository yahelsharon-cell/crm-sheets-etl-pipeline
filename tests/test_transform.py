import datetime as dt
import logging

import pytest

from pipedrive_export import config
from pipedrive_export.transform import (
    Field, Lookups, TransformError, build_records, column_kinds, columns_for, pipeline_columns, referenced_ids,
    resolve_fields, segment, tab_columns, transform, validate_pipeline_stages,
)

MONEY_KEY = "a" * 40
DATE_KEY = "b" * 40
PCT_KEY = "c" * 40
NOTES_KEY = "d" * 40

FIELDS = [
    Field("Title", "title", "varchar"),
    Field("Stage", "stage_id", "stage"),
    Field("Total Invested", MONEY_KEY, "monetary"),
    Field("Investment Date", DATE_KEY, "date"),
    Field("Investor A Holdings", PCT_KEY, "double"),
    Field("Notes", NOTES_KEY, "text"),
    Field("Organization", "org_id", "org"),
    Field("Contact person", "person_id", "people"),
    Field("Label", "label", "set", options={1: "Beta", 2: "alpha", 3: "Gamma"}),
]

STAGES = {10: "Portfolio", 11: "DD Process", 12: "YBD", 13: "Stage Gate V - IIA Filing", 14: "Brand New"}
LOOKUPS = Lookups(stages=STAGES, orgs={100: "Acme Ltd"}, persons={200: "Dana"})


def deal(id, title="Deal", stage_id=11, custom=None, **top):
    return {"id": id, "title": title, "stage_id": stage_id, "org_id": None, "person_id": None,
            "label_ids": [], "custom_fields": custom or {}, **top}


def one(d):
    return build_records([d], FIELDS, LOOKUPS)[0].values


# --- resolve_fields

def test_resolve_matches_trimmed_case_insensitive_and_keeps_config_order():
    meta = [
        {"key": "label", "name": " Label", "field_type": "set", "options": [{"id": 5, "label": "Hot"}]},
        {"key": "title", "name": "Title", "field_type": "varchar"},
        {"key": MONEY_KEY, "name": "total  invested", "field_type": "monetary"},
    ]
    fields = resolve_fields(["Title", "Total Invested", "Label"], meta)
    assert [f.key for f in fields] == ["title", MONEY_KEY, "label"]
    assert fields[2].options == {5: "Hot"}
    assert fields[1].columns == ["Total Invested", "Total Invested Currency"]


def test_resolve_reports_all_missing_names():
    with pytest.raises(TransformError) as exc:
        resolve_fields(["Title", "Nope", "Also Nope"], [{"key": "title", "name": "Title"}])
    assert "'Nope'" in str(exc.value) and "'Also Nope'" in str(exc.value)


def test_resolve_rejects_ambiguous_names():
    meta = [{"key": "x1", "name": "Scope"}, {"key": "x2", "name": "scope "}]
    with pytest.raises(TransformError, match="more than one"):
        resolve_fields(["Scope"], meta)


# --- value conversion

def test_money_extracts_amount_and_currency():
    v = one(deal(1, custom={MONEY_KEY: {"value": 250000, "currency": "usd"}}))
    assert v["Total Invested"] == 250000
    assert v["Total Invested Currency"] == "USD"


def test_money_zero_is_zero_and_missing_is_blank():
    assert one(deal(1, custom={MONEY_KEY: {"value": 0, "currency": "ILS"}}))["Total Invested"] == 0
    v = one(deal(2, custom={MONEY_KEY: None}))
    assert v["Total Invested"] is None and v["Total Invested Currency"] is None


def test_unexpected_currency_warns(caplog):
    with caplog.at_level(logging.WARNING):
        one(deal(1, custom={MONEY_KEY: {"value": 5, "currency": "EUR"}}))
    assert "EUR" in caplog.text


def test_blanks_are_none_never_zero_or_text():
    v = one(deal(1, custom={NOTES_KEY: "   ", PCT_KEY: None, DATE_KEY: ""}))
    assert v["Notes"] is None and v["Investor A Holdings"] is None and v["Investment Date"] is None
    assert v["Organization"] is None and v["Label"] is None


def test_numbers_and_dates_are_typed():
    v = one(deal(1, custom={PCT_KEY: 12.5, DATE_KEY: "2024-03-01"}))
    assert v["Investor A Holdings"] == 12.5
    assert v["Investment Date"] == dt.date(2024, 3, 1)


def test_references_resolve_to_names():
    v = one(deal(1, org_id=100, person_id={"value": 200}))
    assert v["Organization"] == "Acme Ltd" and v["Contact person"] == "Dana"


def test_labels_sorted_and_joined():
    assert one(deal(1, label_ids=[3, 1, 2]))["Label"] == "alpha; Beta; Gamma"


def test_unknown_label_is_skipped_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert one(deal(7, label_ids=[1, 99]))["Label"] == "Beta"
    assert "99" in caplog.text


def test_missing_title_fails_with_deal_ids():
    with pytest.raises(TransformError, match=r"\[2, 3\]"):
        build_records([deal(1), deal(2, title=""), deal(3, title=None)], FIELDS, LOOKUPS)


def test_stage_outside_pipeline_fails():
    with pytest.raises(TransformError, match="stage ID"):
        build_records([deal(1, stage_id=999)], FIELDS, LOOKUPS)


def test_referenced_ids():
    ids = referenced_ids([deal(1, org_id=100, person_id=200), deal(2, org_id={"value": 101})], FIELDS)
    assert ids == {"org": {100, 101}, "people": {200}, "user": set()}


# --- stages, segmentation, sorting

def test_validate_stages_requires_portfolio_and_ordered_stages():
    order = ["DD Process", "YBD"]
    with pytest.raises(TransformError, match="Portfolio"):
        validate_pipeline_stages(["DD Process", "YBD"], order, "Portfolio")
    with pytest.raises(TransformError, match="YBD"):
        validate_pipeline_stages(["Portfolio", "DD Process"], order, "Portfolio")


def test_validate_stages_returns_unlisted_stages(caplog):
    with caplog.at_level(logging.WARNING):
        extra = validate_pipeline_stages(["Portfolio", "DD Process", "YBD", "Brand New"],
                                         ["DD Process", "YBD"], "Portfolio")
    assert extra == ["Brand New"]
    assert "Brand New" in caplog.text


def test_segment_on_stage_only_and_sort():
    records = build_records([
        deal(1, "zeta", stage_id=10, label_ids=[1]),
        deal(2, "Alpha", stage_id=10),
        deal(3, "beta", stage_id=12),       # YBD (last listed stage)
        deal(4, "Aardvark", stage_id=14),   # unlisted stage -> after all listed stages
        deal(5, "Cobalt", stage_id=11),     # DD Process
        deal(6, "Apple", stage_id=11),
        deal(7, "Zulu", stage_id=13),       # Stage Gate V (first listed stage)
    ], FIELDS, LOOKUPS)
    seg = segment(records, ["Title"], ["Title"], stage_order=["Stage Gate V - IIA Filing", "DD Process", "YBD"])

    assert [r.title for r in seg.portfolio] == ["Alpha", "zeta"]
    assert [(r.stage, r.title) for r in seg.pipeline] == [
        ("Stage Gate V - IIA Filing", "Zulu"),
        ("DD Process", "Apple"),
        ("DD Process", "Cobalt"),
        ("YBD", "beta"),
        ("Brand New", "Aardvark"),
    ]


def test_column_kinds():
    kinds = column_kinds(FIELDS)
    assert kinds["Total Invested"] == "money"
    assert kinds["Total Invested Currency"] == "text"
    assert kinds["Investor A Holdings"] == "percent"
    assert kinds["Investment Date"] == "date"
    assert kinds["Title"] == "text"


def test_transform_end_to_end_with_real_config(monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_EXCLUDED_COLUMNS", ["Investment Date", "Investor A Holdings"])
    stages = {i: name for i, name in enumerate([config.PORTFOLIO_STAGE, *config.STAGE_ORDER])}
    lookups = Lookups(stages=stages)
    seg = transform([deal(1, "B", stage_id=0), deal(2, "A", stage_id=len(stages) - 1)], FIELDS, lookups)
    assert [r.deal_id for r in seg.portfolio] == [1]
    assert [r.deal_id for r in seg.pipeline] == [2]
    assert seg.portfolio_columns[:4] == ["Title", "Stage", "Total Invested", "Total Invested Currency"]
    assert "Investment Date" in seg.portfolio_columns and "Investment Date" not in seg.pipeline_columns


def test_transform_fails_on_unknown_excluded_column(monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_EXCLUDED_COLUMNS", ["Investment Dat"])
    lookups = Lookups(stages={i: s for i, s in enumerate([config.PORTFOLIO_STAGE, *config.STAGE_ORDER])})
    with pytest.raises(TransformError, match="Investment Dat"):
        transform([deal(1, stage_id=0)], FIELDS, lookups)


# Field types as reported by Pipedrive for the real configured columns (preview run).
REAL_TYPES = {"Total Invested": "monetary", "Amount Invested by Fund": "monetary",
              "Current Valuation": "monetary", "Cash Status": "monetary", "Investment Date": "date"}


def test_extra_columns_match_by_key_or_name_and_go_to_their_tabs():
    meta = [
        {"key": "title", "name": "Title", "field_type": "varchar"},
        {"key": "e" * 40, "name": "Burn rate", "field_type": "monetary"},
        {"key": "f" * 40, "name": "Lead Source", "field_type": "varchar"},
    ]
    extras = [
        {"source": "e" * 40, "name": "Monthly Burn", "tabs": [config.TAB_PORTFOLIO]},  # by key, renamed
        {"source": " lead source", "tabs": [config.TAB_PIPELINE, config.TAB_PORTFOLIO]},  # by name
    ]
    fields = resolve_fields(["Title"], meta, extras)
    assert [f.name for f in fields] == ["Title", "Monthly Burn", "Lead Source"]
    assert tab_columns(fields, config.TAB_PORTFOLIO) == ["Title", "Monthly Burn", "Monthly Burn Currency",
                                                          "Lead Source"]


def test_extra_column_problems_are_all_reported(monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_EXCLUDED_COLUMNS", [])
    meta = [{"key": "title", "name": "Title"}, {"key": "x" * 40, "name": "Scope"}]
    extras = [
        {"source": "Nope", "tabs": [config.TAB_PIPELINE]},        # not in Pipedrive
        {"source": "Scope", "tabs": ["Wrong Tab"]},               # bad tab
        {"source": "x" * 40, "name": "Title", "tabs": [config.TAB_PIPELINE]},  # clashes with a column
    ]
    with pytest.raises(TransformError) as exc:
        resolve_fields(["Title"], meta, extras)
    message = str(exc.value)
    assert "'Nope'" in message and "Wrong Tab" in message and "duplicate" in message


def test_pipeline_only_extra_is_not_on_portfolio(monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_EXCLUDED_COLUMNS", [])
    fields = [Field("Title", "title", "varchar"),
              Field("Lead Source", "k", "varchar", tabs=frozenset({config.TAB_PIPELINE}))]
    assert tab_columns(fields, config.TAB_PORTFOLIO) == ["Title"]
    assert tab_columns(fields, config.TAB_PIPELINE) == ["Title", "Lead Source"]


def test_configured_headcount_goes_to_portfolio_only():
    headcount = next(e for e in config.EXTRA_COLUMNS if e.get("name") == "Headcount")
    meta = [{"key": k, "name": n, "field_type": "varchar"} for k, n in (("title", "Title"), ("stage_id", "Stage"))]
    meta.append({"key": headcount["source"], "name": "Headcount", "field_type": "varchar"})
    fields = resolve_fields(["Title", "Stage"], meta, [headcount])
    assert "Headcount" in tab_columns(fields, config.TAB_PORTFOLIO)
    assert "Headcount" not in pipeline_columns(["Title", "Stage"], excluded=[])  # sanity
    assert [f.name for f in fields if f.tabs and config.TAB_PIPELINE in f.tabs] == []


def test_real_pipeline_exclusions_match_real_columns():
    fields = [Field(name, f"k{i}", REAL_TYPES.get(name, "varchar")) for i, name in enumerate(config.COLUMNS)]
    columns = columns_for(fields)
    pipeline = pipeline_columns(columns)  # raises if any configured exclusion is misspelled
    milestones = [f"{n} Milestone {part}" for n in ("1st", "2nd", "3rd", "4th", "Fundable")
                  for part in ("Description", "Status")]
    assert pipeline == ["Title", "Stage", "Scope", "Total Invested", "Total Invested Currency", "Notes",
                        "Technology/ R&D Status", *milestones, "Organization", "Contact person", "Label"]
    assert len(columns) - len(pipeline) == len(config.PIPELINE_EXCLUDED_COLUMNS)
