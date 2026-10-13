import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from pipedrive_export import config
from pipedrive_export.main import SanityError, archive_period, sanity_check
from pipedrive_export.sheets import SheetState, _cell

TZ = ZoneInfo(config.TIMEZONE)
NOW = dt.datetime(2026, 9, 1, 6, 0, tzinfo=TZ)


def state(generated_at=None):
    return SheetState(tabs={config.TAB_PORTFOLIO: 1, config.TAB_PIPELINE: 2},
                      generated_at=generated_at, generated_at_metadata_id=None)


def counts(portfolio, pipeline):
    return {config.TAB_PORTFOLIO: portfolio, config.TAB_PIPELINE: pipeline}


def test_archive_period_is_month_of_previous_data():
    assert archive_period(dt.datetime(2026, 8, 1, 6, 0, tzinfo=TZ), NOW) == "2026-08"


def test_new_month_is_archived_even_on_a_manual_run_without_archive():
    # A manual run that is the first of the month must not lose last month's snapshot.
    assert archive_period(dt.datetime(2026, 8, 20, 9, 0, tzinfo=TZ), NOW, on_demand=False) == "2026-08"


def test_mid_month_rerun_is_not_archived_by_default():
    assert archive_period(dt.datetime(2026, 9, 1, 5, 0, tzinfo=TZ), NOW) is None


def test_mid_month_archive_on_demand_is_dated():
    prev = dt.datetime(2026, 9, 1, 5, 7, tzinfo=TZ)
    assert archive_period(prev, NOW, on_demand=True) == "2026-09-01 05.07"


def test_archive_period_uses_local_time_not_utc():
    # 22:30 UTC on Aug 31 is already Sep 1 in Jerusalem: same month as NOW -> a rerun.
    prev = dt.datetime(2026, 8, 31, 22, 30, tzinfo=dt.timezone.utc)
    assert archive_period(prev, NOW) is None


def test_archive_period_skips_first_run():
    assert archive_period(None, NOW) is None
    assert archive_period(None, NOW, on_demand=True) is None


def test_sanity_refuses_empty_portfolio():
    with pytest.raises(SanityError):
        sanity_check(counts(0, 50), state(), {})


def test_sanity_refuses_large_drop():
    prev = state(generated_at=NOW)
    before = counts(10, 90)  # 100 data rows
    sanity_check(counts(10, 60), prev, before)  # 30% drop: allowed
    with pytest.raises(SanityError, match="100 to 69"):
        sanity_check(counts(10, 59), prev, before)


def test_sanity_same_count_passes():
    # Regression: 141 rows before and after must pass (grid padding is no longer counted).
    sanity_check(counts(10, 131), state(generated_at=NOW), counts(10, 131))


def test_sanity_skips_drop_check_on_first_run():
    sanity_check(counts(1, 1), state(generated_at=None), {})


def test_cell_encoding():
    assert _cell(None) == ""
    assert _cell(0) == 0
    assert _cell(dt.date(1900, 1, 1)) == 2
    assert _cell("=not a formula") == "=not a formula"
