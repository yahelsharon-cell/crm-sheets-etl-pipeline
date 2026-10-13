"""Development Dashboard merge: the team's typed columns must survive every run."""

import pytest

from pipedrive_export import config, development
from pipedrive_export.development import COMPANY, DEAL_ID, NO_LONGER_TRACKED, STAGE
from pipedrive_export.sheets import header_column_width
from pipedrive_export.transform import Record, TransformError, segment

PHASE = config.DEVELOPMENT_PIPEDRIVE_COLUMNS[0]


def rec(deal_id, title, phase=None, stage=config.PORTFOLIO_STAGE):
    values = {c: None for c in config.COLUMNS}
    values.update({config.TITLE_FIELD: title, config.STAGE_FIELD: stage, PHASE: phase})
    return Record(deal_id=deal_id, values=values)


def as_dicts(table):
    header, *rows = table
    return [dict(zip(header, row)) for row in rows]


def readback(table):
    """What the Sheets API returns: blanks as "" and trailing blanks trimmed."""
    out = []
    for row in table:
        row = ["" if v is None else v for v in row]
        while row and row[-1] == "":
            row.pop()
        out.append(row)
    return out


def test_first_run_lists_portfolio_with_empty_tracking_columns():
    table = development.merge([], [rec(2, "Beta", "Phase B"), rec(1, "alpha", "Phase A")])
    header = table[0]
    assert header[:2] == [COMPANY, STAGE] and header[-1] == DEAL_ID
    assert header[2:2 + len(config.DEVELOPMENT_PIPEDRIVE_COLUMNS)] == config.DEVELOPMENT_PIPEDRIVE_COLUMNS
    rows = as_dicts(table)
    assert [r[COMPANY] for r in rows] == ["alpha", "Beta"]
    assert rows[0][PHASE] == "Phase A" and rows[0]["Comments"] is None
    assert all(len(row) == len(header) for row in table)
    assert "1st Milestone Status" in header and "Fundable Milestone Description" in header


def test_portfolio_first_then_pipeline_by_stage_order_without_ybd():
    records = [
        rec(1, "Zeta Foods"), rec(2, "Acorn"),                                    # Portfolio
        rec(3, "Bravo", stage="DD Process"), rec(4, "Alpha", stage="DD Process"),
        rec(5, "Yolk", stage="Stage Gate V - IIA Filing"),                         # first listed stage
        rec(6, "Kale", stage="Monitoring"),
        rec(7, "Nope", stage="YBD"), rec(8, "Also no", stage="YBD"),              # excluded
        rec(9, "Newbie", stage="Brand New Stage"),                                 # unlisted: last
    ]
    seg = segment(records, [], [])  # same split and order as the data tabs
    rows = as_dicts(development.merge([], seg.portfolio, seg.pipeline))
    assert [(r[COMPANY], r[STAGE]) for r in rows] == [
        ("Acorn", config.PORTFOLIO_STAGE), ("Zeta Foods", config.PORTFOLIO_STAGE),
        ("Yolk", "Stage Gate V - IIA Filing"),
        ("Alpha", "DD Process"), ("Bravo", "DD Process"),
        ("Kale", "Monitoring"),
        ("Newbie", "Brand New Stage"),
    ]


def test_notes_follow_a_company_from_pipeline_into_portfolio():
    first = development.merge([], [], [rec(4, "Alpha", stage="DD Process")])
    first[1][first[0].index("Comments")] = "IC next week"
    second = development.merge(readback(first), [rec(4, "Alpha")], [])  # promoted to Portfolio
    row = as_dicts(second)[0]
    assert (row[STAGE], row["Comments"]) == (config.PORTFOLIO_STAGE, "IC next week")


def test_milestones_come_from_pipedrive():
    r = rec(1, "Alpha")
    r.values.update({"1st Milestone Description": "Pilot with retailer", "1st Milestone Status": "Ongoing"})
    row = as_dicts(development.merge([], [r]))[0]
    assert (row["1st Milestone Description"], row["1st Milestone Status"]) == ("Pilot with retailer", "Ongoing")


def test_typed_values_follow_their_company_when_a_new_company_sorts_in_between():
    first = development.merge([], [rec(1, "Alpha"), rec(3, "Charlie")])
    first[2][first[0].index("Comments")] = "Needs intro to buyer"  # typed for Charlie
    second = development.merge(readback(first), [rec(1, "Alpha"), rec(2, "Bravo"), rec(3, "Charlie")])
    rows = {r[COMPANY]: r for r in as_dicts(second)}
    assert rows["Charlie"]["Comments"] == "Needs intro to buyer"
    assert rows["Bravo"]["Comments"] is None


def test_pipedrive_columns_are_refreshed_but_tracking_is_kept():
    first = development.merge([], [rec(1, "Alpha", "Phase 1")])
    first[1][first[0].index("Comments")] = "Pilot signed"
    second = development.merge(readback(first), [rec(1, "Alpha (renamed)", "Phase 2")])
    row = as_dicts(second)[0]
    assert (row[COMPANY], row[PHASE], row["Comments"]) == ("Alpha (renamed)", "Phase 2", "Pilot signed")


def test_company_dropping_off_keeps_its_notes_at_the_bottom():
    first = development.merge([], [rec(1, "Alpha"), rec(2, "Bravo")], [rec(3, "Charlie", stage="DD Process")])
    first[2][first[0].index("Comments")] = "Exited"  # Bravo has notes
    # Bravo left; Charlie moved to YBD (excluded) with nothing typed
    second = development.merge(readback(first), [rec(1, "Alpha")], [rec(3, "Charlie", stage="YBD")])
    rows = as_dicts(second)
    assert [r[COMPANY] for r in rows] == ["Alpha", "Bravo" + NO_LONGER_TRACKED]
    assert rows[1]["Comments"] == "Exited" and rows[1][PHASE] is None and rows[1][STAGE] is None
    third = development.merge(readback(second), [rec(1, "Alpha")])
    assert as_dicts(third)[1][COMPANY] == "Bravo" + NO_LONGER_TRACKED  # marker not repeated


def test_columns_added_by_the_team_are_kept():
    first = development.merge([], [rec(1, "Alpha")])
    header = first[0][:-1] + ["Owner", DEAL_ID]  # someone inserts an "Owner" column before Deal ID
    row = first[1][:-1] + ["Dana", 1]
    second = development.merge(readback([header, row]), [rec(1, "Alpha")])
    assert "Owner" in second[0] and as_dicts(second)[0]["Owner"] == "Dana"
    assert second[0][-1] == DEAL_ID


def test_unknown_pipedrive_column_fails(monkeypatch):
    monkeypatch.setattr(config, "DEVELOPMENT_PIPEDRIVE_COLUMNS", ["No Such Field"])
    with pytest.raises(TransformError, match="No Such Field"):
        development.merge([], [rec(1, "Alpha")])


def _added_rules(requests):
    return [r["addConditionalFormatRule"]["rule"] for r in requests if "addConditionalFormatRule" in r]


def test_status_colors_green_orange_red_on_all_status_columns():
    header = development.merge([], [rec(1, "Alpha")])[0]
    rules = _added_rules(development.format_requests(7, header))
    colors = {rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"]:
              rule["booleanRule"]["format"]["backgroundColorStyle"]["rgbColor"] for rule in rules}
    assert list(colors) == ["Completed", "Ongoing", "Not Started"]
    completed, ongoing, not_started = colors["Completed"], colors["Ongoing"], colors["Not Started"]
    assert completed["green"] > completed["red"] and completed["green"] > completed["blue"]  # green
    assert ongoing["red"] > ongoing["green"] > ongoing["blue"]  # orange
    assert not_started["red"] > not_started["green"] and not_started["red"] > not_started["blue"]  # red
    status_cols = {header.index(c) for c in config.DEVELOPMENT_STATUS_COLUMNS}
    assert len(status_cols) == 5
    for rule in rules:
        assert {rg["startColumnIndex"] for rg in rule["ranges"]} == status_cols
        assert all("endRowIndex" not in rg for rg in rule["ranges"])  # covers rows added later


def test_status_rules_are_replaced_not_stacked_and_other_rules_kept():
    header = development.merge([], [rec(1, "Alpha")])[0]
    ours = _added_rules(development.format_requests(7, header))  # as if added by an earlier run
    someone_elses = {"ranges": [{"sheetId": 7, "startRowIndex": 1, "startColumnIndex": 1, "endColumnIndex": 2}],
                     "booleanRule": {"condition": {"type": "NOT_BLANK"}}}
    existing = [ours[0], someone_elses, *ours[1:]]
    requests = development.format_requests(7, header, existing)
    deleted = [r["deleteConditionalFormatRule"]["index"] for r in requests if "deleteConditionalFormatRule" in r]
    assert deleted == [3, 2, 0]  # ours only, highest index first
    assert len(_added_rules(requests)) == len(config.DEVELOPMENT_STATUS_COLORS)


def test_status_rules_from_an_older_column_layout_are_cleaned_up():
    header = development.merge([], [rec(1, "Alpha")])[0]
    old = _added_rules(development.format_requests(7, header))
    for rule in old:  # same rules, one column further left (before the Stage column existed)
        rule["ranges"] = [{**rg, "startColumnIndex": rg["startColumnIndex"] - 1,
                           "endColumnIndex": rg["endColumnIndex"] - 1} for rg in rule["ranges"]]
    requests = development.format_requests(7, header, old)
    assert sum(1 for r in requests if "deleteConditionalFormatRule" in r) == len(old)


def test_development_columns_fit_their_header_words():
    header = development.merge([], [rec(1, "Alpha")])[0]
    widths = {r["updateDimensionProperties"]["range"]["startIndex"]: r["updateDimensionProperties"]["properties"]
              ["pixelSize"] for r in development.format_requests(7, header)
              if r.get("updateDimensionProperties", {}).get("fields") == "pixelSize"}
    for i, name in enumerate(header[:-1]):
        assert widths[i] >= header_column_width(name)
    hide = [r for r in development.format_requests(7, header)
            if r.get("updateDimensionProperties", {}).get("fields") == "hiddenByUser"]
    assert hide and hide[0]["updateDimensionProperties"]["range"]["startIndex"] == len(header) - 1
