# Headless CRM-to-Sheets ETL Pipeline

A production-grade, automated ETL pipeline designed to sync deal-flow, portfolio metrics, and co-investment analytics from Pipedrive CRM into live, formatted Google Sheets dashboards. Engineered with an event-driven architecture combining scheduled CI/CD runs, Google Apps Script dispatch triggers, multi-currency foreign exchange normalization, and strict test-driven integrity.

---

## Architecture Overview

```text
                     ┌────────────────────────┐
                     │     Pipedrive CRM      │
                     │       (REST API)       │
                     └───────────┬────────────┘
                                 │
                                 ▼
                     ┌────────────────────────┐
                     │   Python ETL Engine    │
                     │  (Extract & Transform) │
                     └─────┬────────────▲─────┘
                           │            │
            ┌──────────────┘            └──────────────┐
            ▼                                          │
┌────────────────────────┐                  ┌──────────────────┐
│  Google Drive API      │                  │  Execution       │
│  (Archive & Folders)   │                  │  Triggers        │
└────────────────────────┘                  └──────────▲───────┘
            │                                          │
            ▼                                          │
┌────────────────────────┐                  ┌──────────┴───────────────┐
│  Google Sheets API     │                  │ 1. GitHub Actions (Cron) │
│  (Batched Formatting,  │                  │ 2. Google Apps Script    │
│   Formulas & Layouts)  │                  │    (Repository Dispatch) │
└────────────────────────┘                  └──────────────────────────┘
