"""ensure_tabs against an in-memory fake of the spreadsheet (no Google calls)."""

import pytest

from pipedrive_export import config
from pipedrive_export.sheets import (
    HEADER_CHAR_PX, HEADER_EXTRA_PX, MAX_COLUMN_PX, MIN_COLUMN_PX, SheetsPublisher, SheetState,
    header_column_width,
)


@pytest.mark.parametrize("header", ["Technology/ R&D Status", "Business/ Commercial Status",
                                    "Amount Invested by Fund Currency", "Fundable Milestone Description"])
def test_header_width_fits_longest_word(header):
    longest = max(len(w) for w in header.split())
    assert header_column_width(header) >= longest * HEADER_CHAR_PX + HEADER_EXTRA_PX


def test_header_width_bounds():
    assert header_column_width("ID") == MIN_COLUMN_PX
    assert header_column_width("x" * 200) == MAX_COLUMN_PX
    assert header_column_width(None) == MIN_COLUMN_PX


class FakePublisher(SheetsPublisher):
    def __init__(self, tabs):
        self.tabs = dict(tabs)  # title -> sheetId, in tab order
        self.requests = []

    def read_state(self):
        return SheetState(dict(self.tabs), None, None)

    def batch_update(self, requests):
        self.requests += requests
        for r in requests:
            if "updateSheetProperties" in r:
                props = r["updateSheetProperties"]["properties"]
                old = next(t for t, i in self.tabs.items() if i == props["sheetId"])
                self.tabs[props["title"]] = self.tabs.pop(old)
            elif "addSheet" in r:
                self.tabs[r["addSheet"]["properties"]["title"]] = 100 + len(self.tabs)
        return {}


def ensure(pub):
    return pub.ensure_tabs(config.TAB_ORDER, hidden=config.HIDDEN_TABS, renames=config.TAB_RENAMES)


def test_old_tab_is_renamed_in_place_not_duplicated():
    pub = FakePublisher({config.TAB_DASHBOARD: 5, config.TAB_DEVELOPMENT: 6, config.TAB_PORTFOLIO: 1,
                         "Other Companies": 2, config.TAB_METRICS: 3, config.TAB_HISTORY: 4})
    ids, created = ensure(pub)
    assert ids[config.TAB_PIPELINE] == 2  # same sheet, so its formatting and ID are kept
    assert "Other Companies" not in pub.tabs
    assert created == set()
    assert not any("addSheet" in r for r in pub.requests)


def test_rename_is_skipped_once_done():
    pub = FakePublisher({config.TAB_DASHBOARD: 5, config.TAB_DEVELOPMENT: 6, config.TAB_PORTFOLIO: 1,
                         config.TAB_PIPELINE: 2, config.TAB_METRICS: 3, config.TAB_HISTORY: 4})
    ensure(pub)
    assert pub.requests == []


def test_missing_tabs_are_added_hidden_where_configured():
    pub = FakePublisher({config.TAB_PORTFOLIO: 1, "Other Companies": 2})
    ids, created = ensure(pub)
    assert ids[config.TAB_PIPELINE] == 2
    assert created == {config.TAB_DASHBOARD, config.TAB_DEVELOPMENT, config.TAB_METRICS, config.TAB_HISTORY}
    added = {r["addSheet"]["properties"]["title"]: r["addSheet"]["properties"]
             for r in pub.requests if "addSheet" in r}
    assert added[config.TAB_DASHBOARD]["index"] == 0 and not added[config.TAB_DASHBOARD]["hidden"]
    assert added[config.TAB_DEVELOPMENT]["index"] == 1 and not added[config.TAB_DEVELOPMENT]["hidden"]
    assert added[config.TAB_METRICS]["hidden"] and added[config.TAB_HISTORY]["hidden"]
