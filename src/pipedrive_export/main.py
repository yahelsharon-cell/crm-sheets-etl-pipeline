"""Run: Pipedrive -> transform -> archive if needed -> write live sheet.

Runs monthly on a schedule, and on demand (GitHub "Run workflow" or the Sheet's
CRM Sync menu). The first run in a new month always archives the previous
month's sheet first; --archive additionally keeps a dated copy on a mid-month run.

    python -m pipedrive_export.main --dry-run          # extract + transform only, no Google writes
    python -m pipedrive_export.main --target <SHEET_ID>  # write to a scratch spreadsheet, no archive
    python -m pipedrive_export.main                    # the real run
    python -m pipedrive_export.main --archive          # also snapshot the current sheet before writing
    python -m pipedrive_export.main --create           # first run only: allow creating the live file
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from collections import Counter
from collections.abc import Sequence
from zoneinfo import ZoneInfo

import google.auth

from pipedrive_export import config, dashboard, development, fx, metrics, pipedrive, transform
from pipedrive_export.drive import DriveClient, DriveError, SpreadsheetRef
from pipedrive_export.sheets import SheetsPublisher, SheetState, TabPayload

log = logging.getLogger("pipedrive_export")


class SanityError(RuntimeError):
    pass


def sanity_check(new_counts: dict[str, int], previous: SheetState, previous_counts: dict[str, int],
                 max_drop: float = config.MAX_ROW_DROP) -> None:
    """Refuse to overwrite the live sheet with data that looks broken."""
    if new_counts.get(config.TAB_PORTFOLIO, 0) == 0:
        raise SanityError(f"{config.TAB_PORTFOLIO} would be empty; refusing to write.")
    if previous.generated_at is None:
        return  # first run: nothing to compare with
    before = sum(previous_counts.get(t, 0) for t in new_counts)
    after = sum(new_counts.values())
    if before and after < before * (1 - max_drop):
        raise SanityError(f"Rows would drop from {before} to {after} (more than {max_drop:.0%}); "
                          "refusing to write. Check Pipedrive, then rerun with --force if it is real.")


def archive_period(previous_generated_at: dt.datetime | None, now: dt.datetime,
                   on_demand: bool = False) -> str | None:
    """Label for the archive copy of the data currently in the sheet, or None to skip.

    * Data from an earlier month: always archived as 'YYYY-MM', whatever triggered the
      run, so a month is never lost (e.g. if the scheduled run failed and a manual run
      is the first one of the month).
    * Data from this month: skipped, unless on_demand, which keeps a dated copy
      'YYYY-MM-DD HH.MM' (named after when that data was generated).
    * Sheet never written: nothing to archive.
    """
    if previous_generated_at is None:
        return None
    before = previous_generated_at.astimezone(now.tzinfo)
    if (before.year, before.month) != (now.year, now.month):
        return f"{before.year:04d}-{before.month:02d}"
    if on_demand:
        return before.strftime("%Y-%m-%d %H.%M")
    return None


def _payload(columns: list[str], records: list[transform.Record], kinds: dict[str, str]) -> TabPayload:
    return TabPayload(header=columns, rows=[[r.values[c] for c in columns] for r in records], kinds=kinds)


def run(args: argparse.Namespace) -> None:
    now = dt.datetime.now(ZoneInfo(config.TIMEZONE))

    # Extract + transform
    data = pipedrive.extract(pipedrive.PipedriveClient.from_env())
    segments = transform.transform(data.deals, data.fields, data.lookups)
    kinds = transform.column_kinds(data.fields)
    payloads = {
        config.TAB_PORTFOLIO: _payload(segments.portfolio_columns, segments.portfolio, kinds),
        config.TAB_PIPELINE: _payload(segments.pipeline_columns, segments.pipeline, kinds),
    }
    counts = {title: len(p.rows) for title, p in payloads.items()}
    log.info("Rows per tab: %s", counts)
    log.info("%s by stage: %s", config.TAB_PIPELINE, dict(Counter(r.stage for r in segments.pipeline)))

    # Metrics (aggregates only; no company names are logged)
    rate = fx.fetch_usd_ils(now.date())
    ctx = metrics.Context(portfolio=segments.portfolio, pipeline=segments.pipeline, now=now, fx=rate)
    metrics_grid = metrics.render_metrics(ctx)
    log.info("Metrics: %s", {k: v for k, v in metrics.compute_scalars(ctx).items() if k != "generated_at"})
    if args.dry_run:
        log.info("Dry run: nothing written.")
        return

    # Open the spreadsheet
    credentials, _ = google.auth.default(scopes=config.GOOGLE_SCOPES)
    drive = DriveClient(credentials)
    if args.target:
        ref = SpreadsheetRef(id=args.target, name="(target)",
                             web_view_link=f"https://docs.google.com/spreadsheets/d/{args.target}")
    else:
        ref = drive.open_or_create(config.SPREADSHEET_NAME, config.TARGET_FOLDER_ID, allow_create=args.create)
    publisher = SheetsPublisher(credentials, ref.id)

    tab_ids, _ = publisher.ensure_tabs(config.TAB_ORDER, hidden=config.HIDDEN_TABS, renames=config.TAB_RENAMES)
    previous = publisher.read_state()

    if args.force:
        log.warning("--force: skipping sanity check")
    else:
        previous_counts = publisher.count_data_rows(config.DATA_TABS)
        log.info("Rows currently in sheet: %s", previous_counts)
        sanity_check(counts, previous, previous_counts)

    # Archive the current sheet before overwriting it (live file only).
    if args.target:
        log.info("--target: archive skipped")
    else:
        period = archive_period(previous.generated_at, now, on_demand=args.archive)
        if period:
            drive.archive_snapshot(ref, period)
        else:
            log.info("Archive skipped: sheet not written before, or already written this month "
                     "(use --archive for an extra copy)")

    history = metrics.upsert_history(publisher.read_values(config.TAB_HISTORY), metrics.history_row(ctx))
    # Development Dashboard: merge, so what the team typed is carried over per company.
    existing_dev = publisher.read_values(config.TAB_DEVELOPMENT)
    dev = development.merge(existing_dev, segments.portfolio, segments.pipeline)
    all_payloads = {
        **payloads,
        config.TAB_DEVELOPMENT: TabPayload(header=dev[0], rows=dev[1:], kinds={}),
        config.TAB_METRICS: TabPayload(header=metrics_grid[0], rows=metrics_grid[1:], kinds={},
                                       min_rows=len(metrics_grid)),
        config.TAB_HISTORY: TabPayload(header=history[0], rows=history[1:], kinds={},
                                       min_rows=config.HISTORY_MAX_MONTHS + 1),
    }
    publisher.write_tabs(tab_ids, all_payloads)
    publisher.apply_standard_formatting(tab_ids, payloads, now, previous)
    publisher.batch_update(development.format_requests(
        tab_ids[config.TAB_DEVELOPMENT], dev[0], publisher.conditional_formats(config.TAB_DEVELOPMENT)))

    # Build the Dashboard only while it is empty (new tab, or a previous build failed).
    # Once built it belongs to people: runs never touch it again.
    if publisher.is_empty(config.TAB_DASHBOARD):
        publisher.batch_update(dashboard.build_requests(tab_ids, history_header=history[0]))
        log.info("Built new %r tab", config.TAB_DASHBOARD)
    log.info("Done: %s", ref.web_view_link)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="extract and transform only; no Google calls")
    parser.add_argument("--target", metavar="SPREADSHEET_ID", help="write to this scratch spreadsheet instead")
    parser.add_argument("--create", action="store_true", help="allow creating the live spreadsheet if missing")
    parser.add_argument("--force", action="store_true", help="skip the pre-write sanity check")
    parser.add_argument("--archive", action="store_true",
                        help="keep a dated archive copy even mid-month (a new month is always archived)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        run(args)
    except (pipedrive.PipedriveError, transform.TransformError, metrics.MetricsError, DriveError,
            SanityError) as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
