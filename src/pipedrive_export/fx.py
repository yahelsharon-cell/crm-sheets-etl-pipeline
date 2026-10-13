"""USD/ILS exchange rate for the day of the run.

Sources, in order: Bank of Israel representative rate, a free FX feed, and finally
config.FX_FALLBACK_USD_ILS. The rate is fetched once per run and written into
_metrics as a plain value, so archived copies keep the rate they were built with.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import requests

from pipedrive_export import config

log = logging.getLogger(__name__)

FALLBACK_SOURCE = "fallback rate in config.py"


@dataclass(frozen=True)
class FxRate:
    usd_ils: float  # ILS per 1 USD
    as_of: str  # YYYY-MM-DD the source's rate is for
    source: str

    def convert(self, amount: float, currency: str) -> tuple[float, float] | None:
        """(USD, ILS) for an amount in USD or ILS; None for any other currency."""
        if currency == "USD":
            return amount, amount * self.usd_ils
        if currency == "ILS":
            return amount / self.usd_ils, amount
        return None


def _local_date(ts: dt.datetime) -> str:
    return ts.astimezone(ZoneInfo(config.TIMEZONE)).date().isoformat()


def _bank_of_israel(get: Callable[..., Any]) -> tuple[float, str]:
    body = get(config.FX_BOI_URL, timeout=config.FX_TIMEOUT_SECONDS).json()
    rate = float(body["currentExchangeRate"]) / float(body.get("unit") or 1)
    return rate, _local_date(dt.datetime.fromisoformat(body["lastUpdate"]))


def _er_api(get: Callable[..., Any]) -> tuple[float, str]:
    body = get(config.FX_BACKUP_URL, timeout=config.FX_TIMEOUT_SECONDS).json()
    if body.get("result") != "success":
        raise ValueError(f"result={body.get('result')!r}")
    ts = dt.datetime.fromtimestamp(int(body["time_last_update_unix"]), tz=dt.timezone.utc)
    return float(body["rates"]["ILS"]), _local_date(ts)


SOURCES: list[tuple[str, Callable[[Callable[..., Any]], tuple[float, str]]]] = [
    ("Bank of Israel", _bank_of_israel),
    ("open.er-api.com", _er_api),
]


def _get(url: str, timeout: float) -> requests.Response:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp


def fetch_usd_ils(today: dt.date, get: Callable[..., Any] = _get) -> FxRate:
    for name, source in SOURCES:
        try:
            rate, as_of = source(get)
        except Exception as exc:  # any failure: try the next source
            log.warning("Exchange rate from %s failed: %s", name, exc)
            continue
        if not 1 < rate < 10:  # guards against a unit mix-up, e.g. ILS per USD inverted
            log.warning("Exchange rate from %s looks wrong (%s); ignoring", name, rate)
            continue
        log.info("USD/ILS %.4f from %s (as of %s)", rate, name, as_of)
        return FxRate(rate, as_of, name)

    log.warning("All exchange rate sources failed; using fallback %s", config.FX_FALLBACK_USD_ILS)
    return FxRate(config.FX_FALLBACK_USD_ILS, today.isoformat(), FALLBACK_SOURCE)
