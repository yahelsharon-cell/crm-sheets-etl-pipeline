"""Static configuration. Every name a human might need to change lives here.

Tab titles and the live file name are a public contract: bookmarks, formulas and
dashboards reference them. Renaming one is a breaking change.

Deployment-specific values (folder IDs, field keys, logo) come from environment
variables; see .env.example.
"""

import os

from dotenv import load_dotenv

# Local runs read settings from .env; existing environment variables (e.g. in CI) win.
# Loaded here, before the os.getenv calls below, since every module imports config first.
load_dotenv()

# --- Google Drive / Sheets -------------------------------------------------

SPREADSHEET_NAME = "Portfolio & Pipeline"

# Must live in a Shared Drive: service accounts have no My Drive quota.
# (`or`, not a getenv default: an unset GitHub secret arrives as an empty string.)
TARGET_FOLDER_ID = os.getenv("TARGET_FOLDER_ID") or "YOUR_TARGET_FOLDER_ID"
ARCHIVE_FOLDER_ID = os.getenv("ARCHIVE_FOLDER_ID") or "YOUR_ARCHIVE_FOLDER_ID"

# Full drive scope (not drive.file): drive.file cannot see a spreadsheet a human created.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

# "Which month is it" is decided in this zone, not UTC (the cron runs in UTC).
TIMEZONE = "Asia/Jerusalem"

# Archive copies are named "<SPREADSHEET_NAME> <YYYY-MM>", where the month is the
# month the archived data was generated in.
ARCHIVE_NAME_FORMAT = "{base} {period}"

# --- Tabs ------------------------------------------------------------------

TAB_DASHBOARD = "Dashboard"
TAB_DEVELOPMENT = "Development Dashboard"
TAB_PORTFOLIO = "Portfolio Overview"
TAB_PIPELINE = "Pipeline"
TAB_METRICS = "_metrics"
TAB_HISTORY = "_history"

# Old tab title -> new title. An existing tab with the old title is renamed in place
# (keeping its ID, formatting and anything pointing at it) instead of a new tab being
# created next to a stale one.
TAB_RENAMES = {"Other Companies": TAB_PIPELINE}

# Position of each tab when it is first created. Existing tabs are never moved.
TAB_ORDER = [TAB_DASHBOARD, TAB_DEVELOPMENT, TAB_PORTFOLIO, TAB_PIPELINE, TAB_METRICS, TAB_HISTORY]
HIDDEN_TABS = {TAB_METRICS, TAB_HISTORY}

# Tabs holding one row per deal.
DATA_TABS = [TAB_PORTFOLIO, TAB_PIPELINE]

# --- Sheet formatting (applied every run) ----------------------------------

FROZEN_ROWS = 1  # header
FROZEN_COLUMNS = 1  # Title

# Display formats per column kind (see transform.column_kinds). Values are never
# changed by formatting: a holding of 15 is stored as 15 and shown as "15%".
NUMBER_FORMATS = {
    "money": {"type": "NUMBER", "pattern": "#,##0"},
    "number": {"type": "NUMBER", "pattern": "#,##0"},
    "percent": {"type": "NUMBER", "pattern": '0"%"'},
    "date": {"type": "DATE", "pattern": "yyyy-mm-dd"},
}

# Developer metadata key (invisible to users) recording when the live sheet was
# last written. Used to name archive copies and to detect same-month reruns.
GENERATED_AT_METADATA_KEY = "pipedrive_export.generated_at"

# --- Pre-write sanity check ------------------------------------------------

# Refuse to overwrite the live sheet if total rows fall by more than this fraction
# versus the previous run (override with --force after checking Pipedrive).
MAX_ROW_DROP = 0.30

# --- Pipedrive -------------------------------------------------------------

PIPEDRIVE_TOKEN_ENV = "PIPEDRIVE_API_KEY"

# Override with your company domain (https://<company>.pipedrive.com) if needed.
PIPEDRIVE_BASE_URL_ENV = "PIPEDRIVE_BASE_URL"
PIPEDRIVE_DEFAULT_BASE_URL = "https://api.pipedrive.com"

PIPEDRIVE_PAGE_SIZE = 500  # v2 maximum
PIPEDRIVE_TIMEOUT_SECONDS = 30
PIPEDRIVE_MAX_RETRIES = 5

# Only open deals in this pipeline are exported. Won/lost deals are ignored.
PIPELINE_NAME = "Deal Flow"
DEAL_STATUS = "open"

# --- Columns ---------------------------------------------------------------

# One row per deal. Names exactly as shown in the Pipedrive UI; matching is
# case-insensitive and ignores surrounding whitespace. Order here = column order.
# A name that is not found in Pipedrive stops the run.
COLUMNS = [
    "Title",
    "Stage",
    "Scope",
    "Total Invested",
    "Amount Invested by Fund",
    "Investment Date",
    "Notes",
    "Phase in Incubator",
    "Fund Direct Holding",
    "Current Valuation",
    "Valuation Quotation",
    "Business/ Commercial Status",
    "Technology/ R&D Status",
    "Cash Status",
    "1st Milestone Description",
    "1st Milestone Status",
    "2nd Milestone Description",
    "2nd Milestone Status",
    "3rd Milestone Description",
    "3rd Milestone Status",
    "4th Milestone Description",
    "4th Milestone Status",
    "Fundable Milestone Description",
    "Fundable Milestone Status",
    "Runway/ End Period",
    "Investor A Holdings",
    "Investor B Holdings",
    "Investor C Holdings",
    "Investor D Holdings",
    "Organization",
    "Contact person",
    "Label",
]

# --- Extra columns (add new Pipedrive fields here) --------------------------
#
# One line per field, added after COLUMNS. To find a field's exact name, key and type:
#     python -m pipedrive_export.pipedrive --list-fields
#
#   "source": the field's name as shown in Pipedrive, OR its API key (the 40-character
#             code for custom fields, e.g. "0b962deb586d..."). A key keeps working even if
#             someone renames the field in Pipedrive.
#   "name":   (optional) column header in the sheet; defaults to the Pipedrive name.
#   "tabs":   which data tabs show it: TAB_PORTFOLIO, TAB_PIPELINE, or both.
#
# Like COLUMNS, a source that is not found in Pipedrive stops the run. Money fields
# automatically get a "<name> Currency" column next to them.
EXTRA_COLUMNS: list[dict] = [
    # Headcount: a text field in Pipedrive. Set PIPEDRIVE_HEADCOUNT_FIELD_KEY to its API key
    # so a rename can't break it; otherwise it is matched by name.
    {"source": os.getenv("PIPEDRIVE_HEADCOUNT_FIELD_KEY") or "Headcount", "name": "Headcount",
     "tabs": ["Portfolio Overview"]},
    # Examples:
    # {"source": "Founder Email", "tabs": ["Portfolio Overview", "Pipeline"]},
    # {"source": "PASTE_40_CHARACTER_FIELD_KEY_HERE", "name": "Burn Rate", "tabs": ["Portfolio Overview"]},
    # {"source": "Lead Source", "tabs": ["Pipeline"]},
]

# Pipedrive monetary fields carry their own currency (USD or ILS per deal). Each one
# becomes two adjacent columns: the amount, then "<name> Currency".
CURRENCY_COLUMN_SUFFIX = " Currency"
EXPECTED_CURRENCIES = ("USD", "ILS")  # anything else is logged as a warning

# Percentages, entered in Pipedrive as whole numbers (15 means 15%). The value is
# written unchanged and only displayed with a % sign, so 15 stays 15 in the data.
PERCENT_COLUMNS = [
    "Investor A Holdings",
    "Investor B Holdings",
    "Investor C Holdings",
    "Investor D Holdings",
]

# Portfolio-only columns, left out of the Pipeline tab (still exported on the
# Portfolio Overview tab). Names as they appear in the sheet header, including
# the generated "<name> Currency" columns. A name that matches no column stops the run.
PIPELINE_EXCLUDED_COLUMNS = [
    "Amount Invested by Fund",
    "Amount Invested by Fund Currency",
    "Investment Date",
    "Phase in Incubator",
    "Fund Direct Holding",
    "Current Valuation",
    "Current Valuation Currency",
    "Valuation Quotation",
    "Business/ Commercial Status",
    "Cash Status",
    "Cash Status Currency",
    # (milestone fields are exported to Pipeline too)
    "Runway/ End Period",
    "Investor A Holdings",
    "Investor B Holdings",
    "Investor C Holdings",
    "Investor D Holdings",
]

# Multiple labels on one deal are joined into one cell with this separator.
LABEL_SEPARATOR = "; "

# --- Segmentation ----------------------------------------------------------

# "Stage" is the deal's pipeline stage. Stage == PORTFOLIO_STAGE -> Portfolio tab,
# everything else -> Pipeline tab. Label never affects segmentation.
STAGE_FIELD = "Stage"
TITLE_FIELD = "Title"
LABEL_FIELD = "Label"  # sectors, e.g. "Food As Health"
PORTFOLIO_STAGE = "Portfolio"

# Sort order for the Pipeline tab (then by Title A-Z within a stage).
# A stage listed here that no longer exists in the pipeline stops the run
# (it was probably renamed). A pipeline stage missing from this list is sorted
# last and logged as a warning.
STAGE_ORDER = [
    "Stage Gate V - IIA Filing",
    "Stage Gate IV - Investment Committee",
    "DD Process",
    "Stage Gate III - Partners Feedback",
    "Stage Gate II - Team Meeting",
    "Stage Gate I - Introduction",
    "Stage 0 | New Deal",
    "Monitoring",
    "YBD",
]

# --- Dashboard -------------------------------------------------------------

# Stages left out of every Dashboard figure ("YBD" = companies we will not invest in).
# "Active pipeline" = open deals that are neither Portfolio nor one of these.
DASHBOARD_HIDDEN_STAGES = ["YBD"]

# Money KPIs (portfolio companies only). Key -> column name. Each deal's amount is
# converted with the day's USD/ILS rate, and every KPI is shown as a USD total and an
# ILS total covering all reported companies. Amounts in other currencies are left out.
MONEY_METRICS = {
    "fs_invested": "Amount Invested by Fund",
    "portfolio_valuation": "Total Invested",  # x Direct Holding, see below
}

# "Portfolio valuation" on the Dashboard = Total Invested x Fund Direct Holding.
# The holding is free text in Pipedrive: "15%", "15" and 0.15 all mean 15%; empty or
# unreadable counts as 0. (The "Current Valuation" field is still exported on the
# Portfolio Overview tab; it is just not used on the Dashboard.)
DIRECT_HOLDING_FIELD = "Fund Direct Holding"
ADJUSTED_BY_HOLDING = {"portfolio_valuation"}

# --- Exchange rate ---------------------------------------------------------

# USD/ILS rate for the day of the run, tried in order: Bank of Israel representative
# rate, then a free FX feed. If both fail, FX_FALLBACK_USD_ILS is used and the
# Dashboard says so. The rate used is stored as a value in _metrics and _history.
FX_TIMEOUT_SECONDS = 20
FX_BOI_URL = "https://boi.org.il/PublicApi/GetExchangeRate?key=USD"
FX_BACKUP_URL = "https://open.er-api.com/v6/latest/USD"
FX_FALLBACK_USD_ILS = 3.06  # Bank of Israel rate on 2026-09-30; update occasionally

# Keep this many months of history rows available to the trend charts (10 years).
HISTORY_MAX_MONTHS = 120

# Logo at the top left of the Dashboard; empty = no logo. Must be PNG/JPG/GIF: Google
# Sheets' IMAGE() does not display SVG files.
LOGO_URL = os.getenv("DASHBOARD_LOGO_URL", "").strip()

# Chart colors: a colorblind-validated categorical palette in fixed order.
# Single-series charts use the first color.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]

# Sheet styling
HEADER_BG = "#1f2a37"  # data-tab header rows: dark slate with white text
HEADER_TEXT = "#ffffff"
BOX_BG = "#eef3f8"  # Dashboard metric boxes: pale slate-blue
BOX_BORDER = "#c5d0dc"

# --- Development Dashboard -------------------------------------------------

# Milestone tracker, one row per company: Portfolio companies first (A-Z), then Pipeline
# companies in STAGE_ORDER (then A-Z), like the data tabs. Deals in the stages below are
# left out entirely. Milestones live in Pipedrive: "Company", "Stage" and the Pipedrive
# columns below are refreshed on every run (update them in Pipedrive, not in the sheet).
# The typed columns are for the team's own notes and are kept across runs, matched to
# each company by its hidden deal ID, so re-sorting, a stage change or a renamed company
# is safe. A company that drops off the tracker keeps its row at the bottom, marked, if
# anything was typed for it; otherwise its row is dropped.
DEVELOPMENT_EXCLUDED_STAGES = ["YBD"]
DEVELOPMENT_PIPEDRIVE_COLUMNS = [
    "Phase in Incubator",
    "Runway/ End Period",
    "1st Milestone Description",
    "1st Milestone Status",
    "2nd Milestone Description",
    "2nd Milestone Status",
    "3rd Milestone Description",
    "3rd Milestone Status",
    "4th Milestone Description",
    "4th Milestone Status",
    "Fundable Milestone Description",
    "Fundable Milestone Status",
]
DEVELOPMENT_TRACKING_COLUMNS = ["Comments"]
# Columns whose cells are colored by value (the milestone statuses), and the colors.
# The status text always stays visible, so color is never the only signal.
DEVELOPMENT_STATUS_COLUMNS = [c for c in DEVELOPMENT_PIPEDRIVE_COLUMNS if c.endswith("Milestone Status")]
DEVELOPMENT_STATUS_COLORS = {  # soft tints, so the dark status text stays readable
    "Completed": "#c8ebc8",  # green
    "Ongoing": "#fde0bd",  # orange
    "Not Started": "#f7c9c9",  # red
}
