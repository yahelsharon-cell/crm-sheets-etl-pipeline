"""Pipedrive API client: auth, retries, pagination.

The token is sent in the x-api-token header, never as a query parameter, so it
cannot leak into logs or exception messages that include the URL.

Run directly for a read-only preview (field mapping, fill counts, deals per tab):

    python -m pipedrive_export.pipedrive
    python -m pipedrive_export.pipedrive --list-fields   # every deal field's name, key, type
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import requests

from pipedrive_export import config, transform  # config loads .env

log = logging.getLogger(__name__)


class PipedriveError(RuntimeError):
    pass


class PipedriveClient:
    def __init__(self, token: str, base_url: str = config.PIPEDRIVE_DEFAULT_BASE_URL):
        if not token:
            raise PipedriveError(f"Missing Pipedrive token (set {config.PIPEDRIVE_TOKEN_ENV}).")
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"x-api-token": token, "Accept": "application/json"})

    @classmethod
    def from_env(cls) -> PipedriveClient:
        return cls(
            token=os.environ.get(config.PIPEDRIVE_TOKEN_ENV, "").strip(),
            base_url=os.environ.get(config.PIPEDRIVE_BASE_URL_ENV, config.PIPEDRIVE_DEFAULT_BASE_URL),
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET with retries: honors Retry-After on 429, exponential backoff on 5xx."""
        url = f"{self._base_url}{path}"
        for attempt in range(config.PIPEDRIVE_MAX_RETRIES + 1):
            try:
                resp = self._session.get(url, params=params, timeout=config.PIPEDRIVE_TIMEOUT_SECONDS)
            except requests.ConnectionError as exc:
                if attempt == config.PIPEDRIVE_MAX_RETRIES:
                    raise PipedriveError(f"GET {path}: connection failed") from exc
                self._sleep(path, attempt, reason="connection error")
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == config.PIPEDRIVE_MAX_RETRIES:
                    raise PipedriveError(f"GET {path}: HTTP {resp.status_code} after {attempt} retries")
                self._sleep(path, attempt, reason=f"HTTP {resp.status_code}",
                            retry_after=resp.headers.get("Retry-After"))
                continue

            if resp.status_code == 401:
                raise PipedriveError(f"GET {path}: HTTP 401, the API token is invalid or revoked")
            if not resp.ok:
                raise PipedriveError(f"GET {path}: HTTP {resp.status_code}: {resp.text[:300]}")

            body = resp.json()
            if not body.get("success", False):
                raise PipedriveError(f"GET {path}: API reported failure: {body.get('error')}")
            return body

        raise AssertionError("unreachable")

    @staticmethod
    def _sleep(path: str, attempt: int, reason: str, retry_after: str | None = None) -> None:
        try:
            delay = float(retry_after) if retry_after else 2.0 ** attempt
        except ValueError:
            delay = 2.0 ** attempt
        log.warning("GET %s: %s, retrying in %.1fs", path, reason, delay)
        time.sleep(delay)

    # --- Pagination: callers see a flat stream of records for both API versions.

    def paginate_v2(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        params = {**(params or {}), "limit": config.PIPEDRIVE_PAGE_SIZE}
        while True:
            body = self.get(path, params)
            yield from body.get("data") or []
            cursor = (body.get("additional_data") or {}).get("next_cursor")
            if not cursor:
                return
            params["cursor"] = cursor

    def paginate_v1(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        params = {**(params or {}), "start": 0, "limit": config.PIPEDRIVE_PAGE_SIZE}
        while True:
            body = self.get(path, params)
            yield from body.get("data") or []
            pagination = (body.get("additional_data") or {}).get("pagination") or {}
            if not pagination.get("more_items_in_collection"):
                return
            params["start"] = pagination["next_start"]

    # --- Endpoints

    def current_user(self) -> dict[str, Any]:
        return self.get("/api/v1/users/me")["data"]

    def deal_fields(self) -> list[dict[str, Any]]:
        """Field metadata: name, key (hash for custom fields), field_type, options."""
        return list(self.paginate_v1("/api/v1/dealFields"))

    def pipeline_id(self, name: str) -> int:
        pipelines = list(self.paginate_v1("/api/v1/pipelines"))
        matches = [p for p in pipelines if transform.normalize(p["name"]) == transform.normalize(name)]
        if len(matches) != 1:
            raise PipedriveError(f"Expected exactly one pipeline named {name!r}, found {len(matches)}. "
                                 f"Pipelines: {[p['name'] for p in pipelines]}")
        return matches[0]["id"]

    def stages(self, pipeline_id: int) -> dict[int, str]:
        """Stage ID -> name for one pipeline."""
        return {s["id"]: s["name"] for s in self.paginate_v1("/api/v1/stages", {"pipeline_id": pipeline_id})}

    def deals(self, pipeline_id: int, status: str) -> Iterator[dict[str, Any]]:
        return self.paginate_v2("/api/v2/deals", {"pipeline_id": pipeline_id, "status": status})

    def names(self, path: str, ids: set[int]) -> dict[int, str]:
        """ID -> name for organizations or persons, fetched 100 IDs at a time."""
        ordered, names = sorted(ids), {}
        for i in range(0, len(ordered), 100):
            chunk = ",".join(str(x) for x in ordered[i:i + 100])
            for rec in self.paginate_v2(path, {"ids": chunk}):
                names[rec["id"]] = (rec.get("name") or "").strip()
        return names

    def users(self) -> dict[int, str]:
        return {u["id"]: u["name"] for u in self.paginate_v1("/api/v1/users")}


@dataclass(frozen=True)
class Extract:
    deals: list[dict[str, Any]]
    fields: list[transform.Field]
    lookups: transform.Lookups


def extract(client: PipedriveClient) -> Extract:
    """Everything transform needs: open deals in the configured pipeline plus lookups."""
    fields = transform.resolve_fields(config.COLUMNS, client.deal_fields(), config.EXTRA_COLUMNS)
    pipeline_id = client.pipeline_id(config.PIPELINE_NAME)
    stages = client.stages(pipeline_id)
    deals = list(client.deals(pipeline_id, config.DEAL_STATUS))
    log.info("Fetched %d %s deals from pipeline %r (%d stages)",
             len(deals), config.DEAL_STATUS, config.PIPELINE_NAME, len(stages))

    ids = transform.referenced_ids(deals, fields)
    lookups = transform.Lookups(
        stages=stages,
        orgs=client.names("/api/v2/organizations", ids["org"]) if ids["org"] else {},
        persons=client.names("/api/v2/persons", ids["people"]) if ids["people"] else {},
        users=client.users() if ids["user"] else {},
    )
    return Extract(deals=deals, fields=fields, lookups=lookups)


def list_fields(client: PipedriveClient) -> None:
    """Every deal field: name, full API key and type, for filling in config.EXTRA_COLUMNS."""
    fields = sorted(client.deal_fields(), key=lambda f: transform.normalize(f.get("name", "")))
    print(f"{'Name':<45} {'Key (use as source)':<42} Type")
    for f in fields:
        print(f"{f.get('name', '').strip():<45} {f['key']:<42} {f.get('field_type', '')}")


def main() -> int:
    """Preview: extract + transform, print the field mapping and per-tab counts. Writes nothing.
    With --list-fields: print all Pipedrive deal fields instead."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        client = PipedriveClient.from_env()
        if "--list-fields" in sys.argv[1:]:
            list_fields(client)
            return 0
        me = client.current_user()
        # Visibility check: a non-admin token may see only part of the data.
        log.info("Connected to company %r as %s user",
                 me.get("company_name"), "an admin" if me.get("is_admin") else "a NON-admin")
        data = extract(client)
        segments = transform.transform(data.deals, data.fields, data.lookups)
    except (PipedriveError, transform.TransformError) as exc:
        log.error("%s", exc)
        return 1

    records = segments.portfolio + segments.pipeline
    print(f"\nOpen deals in {config.PIPELINE_NAME!r}: {len(records)}")
    print("\nField mapping (column -> Pipedrive key [type], filled rows):")
    for f in data.fields:
        for col in f.columns:
            filled = sum(1 for r in records if r.values[col] is not None)
            print(f"  {col:<40} {f.key[:12]:<12} [{f.field_type}]  {filled}/{len(records)}")

    print(f"\n{config.TAB_PORTFOLIO}: {len(segments.portfolio)} deals, {len(segments.portfolio_columns)} columns")
    print(f"{config.TAB_PIPELINE}: {len(segments.pipeline)} deals, {len(segments.pipeline_columns)} columns")
    for stage, n in Counter(r.stage for r in segments.pipeline).items():  # already in sort order
        print(f"  {stage}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
