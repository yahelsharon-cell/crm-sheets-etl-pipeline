"""Exchange rate fetching, with the HTTP calls mocked (tests never touch the network)."""

import datetime as dt

import pytest

from pipedrive_export import config, fx

TODAY = dt.date(2026, 9, 30)

BOI_BODY = {"key": "USD", "currentExchangeRate": 3.063, "unit": 1, "lastUpdate": "2026-09-30T09:22:02.95Z"}
ER_BODY = {"result": "success", "rates": {"ILS": 3.0676}, "time_last_update_unix": 1790726551}  # 2026-09-30 UTC


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def fake_get(responses):
    """responses: url -> body, or an exception to raise."""
    calls = []

    def get(url, timeout):
        calls.append(url)
        result = responses[url]
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)

    get.calls = calls
    return get


def test_uses_bank_of_israel_first():
    get = fake_get({config.FX_BOI_URL: BOI_BODY, config.FX_BACKUP_URL: ER_BODY})
    rate = fx.fetch_usd_ils(TODAY, get=get)
    assert rate == fx.FxRate(3.063, "2026-09-30", "Bank of Israel")
    assert get.calls == [config.FX_BOI_URL]


def test_falls_back_to_backup_feed():
    get = fake_get({config.FX_BOI_URL: ConnectionError("down"), config.FX_BACKUP_URL: ER_BODY})
    rate = fx.fetch_usd_ils(TODAY, get=get)
    assert (rate.usd_ils, rate.source) == (3.0676, "open.er-api.com")


def test_falls_back_to_config_rate_when_all_sources_fail(caplog):
    get = fake_get({config.FX_BOI_URL: TimeoutError(), config.FX_BACKUP_URL: {"result": "error"}})
    rate = fx.fetch_usd_ils(TODAY, get=get)
    assert rate == fx.FxRate(config.FX_FALLBACK_USD_ILS, "2026-09-30", fx.FALLBACK_SOURCE)
    assert "fallback" in caplog.text


def test_rejects_implausible_rate():
    get = fake_get({config.FX_BOI_URL: {**BOI_BODY, "currentExchangeRate": 0.33}, config.FX_BACKUP_URL: ER_BODY})
    assert fx.fetch_usd_ils(TODAY, get=get).source == "open.er-api.com"


def test_boi_unit_is_respected():
    get = fake_get({config.FX_BOI_URL: {**BOI_BODY, "currentExchangeRate": 30.63, "unit": 10}})
    assert fx.fetch_usd_ils(TODAY, get=get).usd_ils == pytest.approx(3.063)


def test_convert():
    rate = fx.FxRate(4.0, "2026-09-30", "test")
    assert rate.convert(100, "USD") == (100, 400)
    assert rate.convert(400, "ILS") == (100, 400)
    assert rate.convert(100, "EUR") is None
