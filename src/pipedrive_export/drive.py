"""Google Drive layer: find-or-create the live spreadsheet, and archive snapshots.

The live file is always found by name inside the target folder, so its file ID and
webViewLink (which stakeholders bookmark) survive every run. Creating a new file
happens only when the caller explicitly allows it.

Run directly for a read-only check of folder access (creates nothing):

    python -m pipedrive_export.drive
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

import google.auth
from googleapiclient.discovery import build

from pipedrive_export import config

log = logging.getLogger(__name__)

_FILE_FIELDS = "id, name, webViewLink"


class DriveError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpreadsheetRef:
    id: str
    name: str
    web_view_link: str
    created: bool = False


def _quote(value: str) -> str:
    """Escape a value for a Drive query string literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def archive_file_name(period: str, base: str = config.SPREADSHEET_NAME) -> str:
    """period is 'YYYY-MM', the month the archived data was generated in."""
    return config.ARCHIVE_NAME_FORMAT.format(base=base, period=period)


class DriveClient:
    def __init__(self, credentials):
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    @classmethod
    def from_default_credentials(cls) -> DriveClient:
        credentials, _ = google.auth.default(scopes=config.GOOGLE_SCOPES)
        return cls(credentials)

    def shared_drive_id(self, folder_id: str) -> str:
        """Return the Shared Drive ID containing folder_id; fail if it is not a Shared Drive folder."""
        folder = self._service.files().get(
            fileId=folder_id, fields="id, name, mimeType, driveId, trashed", supportsAllDrives=True,
        ).execute()
        if folder.get("mimeType") != config.FOLDER_MIME_TYPE:
            raise DriveError(f"{folder_id} is not a folder (mimeType {folder.get('mimeType')}).")
        if folder.get("trashed"):
            raise DriveError(f"Folder {folder['name']!r} ({folder_id}) is in the trash.")
        if not folder.get("driveId"):
            raise DriveError(
                f"Folder {folder['name']!r} ({folder_id}) is not in a Shared Drive. "
                "Service accounts cannot create files in My Drive."
            )
        return folder["driveId"]

    def find_spreadsheet(self, name: str, folder_id: str) -> SpreadsheetRef | None:
        """Exact-name lookup in one folder. More than one match is an error, never a guess."""
        drive_id = self.shared_drive_id(folder_id)
        query = (
            f"name = {_quote(name)} and {_quote(folder_id)} in parents "
            f"and mimeType = {_quote(config.SPREADSHEET_MIME_TYPE)} and trashed = false"
        )
        files = self._service.files().list(
            q=query,
            fields=f"files({_FILE_FIELDS})",
            corpora="drive",
            driveId=drive_id,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageSize=10,
        ).execute().get("files", [])

        if len(files) > 1:
            ids = ", ".join(f["id"] for f in files)
            raise DriveError(f"{len(files)} spreadsheets named {name!r} in folder {folder_id}: {ids}. "
                             "Remove the duplicates; refusing to pick one.")
        if not files:
            return None
        f = files[0]
        return SpreadsheetRef(id=f["id"], name=f["name"], web_view_link=f["webViewLink"])

    def open_or_create(self, name: str, folder_id: str, *, allow_create: bool) -> SpreadsheetRef:
        existing = self.find_spreadsheet(name, folder_id)
        if existing:
            log.info("Using existing spreadsheet %r (%s)", existing.name, existing.id)
            return existing
        if not allow_create:
            raise DriveError(f"No spreadsheet named {name!r} in folder {folder_id}, and creation is not allowed.")

        # Drive files.create (not Sheets spreadsheets.create) so the file lands in the Shared Drive.
        f = self._service.files().create(
            body={"name": name, "mimeType": config.SPREADSHEET_MIME_TYPE, "parents": [folder_id]},
            fields=_FILE_FIELDS,
            supportsAllDrives=True,
        ).execute()
        log.warning("Created NEW spreadsheet %r (%s)", f["name"], f["id"])
        return SpreadsheetRef(id=f["id"], name=f["name"], web_view_link=f["webViewLink"], created=True)

    def archive_snapshot(self, source: SpreadsheetRef, period: str,
                         archive_folder_id: str = config.ARCHIVE_FOLDER_ID) -> SpreadsheetRef | None:
        """Copy the live spreadsheet into the archive folder as '<name> <period>'.

        Idempotent: if an archive with that name already exists, nothing is copied and
        None is returned. Any API failure propagates, so the caller aborts before the
        live sheet is cleared.
        """
        name = archive_file_name(period, base=source.name)
        existing = self.find_spreadsheet(name, archive_folder_id)
        if existing:
            log.info("Archive %r already exists (%s); skipping copy", name, existing.id)
            return None

        f = self._service.files().copy(
            fileId=source.id,
            body={"name": name, "parents": [archive_folder_id]},
            fields=_FILE_FIELDS,
            supportsAllDrives=True,
        ).execute()
        log.info("Archived live sheet as %r (%s)", f["name"], f["id"])
        return SpreadsheetRef(id=f["id"], name=f["name"], web_view_link=f["webViewLink"], created=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        drive = DriveClient.from_default_credentials()
        for label, folder_id in (("Target", config.TARGET_FOLDER_ID), ("Archive", config.ARCHIVE_FOLDER_ID)):
            log.info("%s folder OK, Shared Drive %s", label, drive.shared_drive_id(folder_id))
        live = drive.find_spreadsheet(config.SPREADSHEET_NAME, config.TARGET_FOLDER_ID)
    except Exception as exc:  # smoke test: report whatever went wrong
        log.error("%s: %s", type(exc).__name__, exc)
        return 1

    if live:
        print(f"Live spreadsheet found: {live.web_view_link}")
    else:
        print(f"No spreadsheet named {config.SPREADSHEET_NAME!r} yet; the first real run will create it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
