# SPRING Reporting

Standalone reporting layer for the Airtable → Trello and WhatsApp services.

## What it produces

- Weekly Service Report
- Monthly Management Report
- Quarterly Impact Report
- PDF with KPI cards, bar charts, donut/funnel graphics and reporting notes
- CSV summary for audit/data use
- Optional XLSX workbook using `artifact_tool`
- SQLite historical snapshot/event database
- Historical WhatsApp message/attachment events pulled from `whatsapp_service.db`
- Trello card/comment activity collected through the existing Trello API credentials
- Option A Outlook reporting: Power Automate Outlook comments already written to Trello are classified and counted as Outlook events
- Unified communication metrics across WhatsApp and Outlook without double-counting Trello workflow comments

## Four-source reporting model

Airtable remains the canonical client/case source. WhatsApp events come directly from the existing `whatsapp_service.db`. Trello cards and comments are collected through the same Trello API credentials used by the operational WhatsApp → Trello service. Outlook uses **Option A**: the reporting collector recognises the `📧⬇️ **Incoming Outlook**` and `📧⬆️ **Outgoing Outlook**` comments written to Trello by Power Automate, so no second Outlook authentication system is introduced.

The reporting database keeps source-specific records plus a conservative Airtable client link. Exact phone matches are preferred, followed by exact full-name matches; ambiguous matches remain unlinked instead of being guessed.

## Why there is a database

The Trello sync currently limits normal client synchronisation to recent Airtable records. Reporting should not rely on that retention window. This package keeps append-only client snapshots and dated referral/support/housing events so that future monthly and quarterly trend reports are based on history.

Run the collector at least daily for useful trend history.

## Airtable mappings

The collector uses the real field names already identified in the SPRING Airtable schema, including:

- `SPRING - Issue^`
- `Risk of Homelessness.^`
- `MEARS Eviction Date`
- `Date of change in housing situation`
- `What type of tenancy?`
- `Secure tenancy (12months or more)`
- `(If changed) Secure tenancy? (12 Months or more)`
- `CoSS - Date support provided`
- `CAS Contact with Client (Phone/Email) date (2026)`
- `SAVTE - Client contacted (date)`
- `SCC Client attends support meeting (date)`
- `SOLACE Client attends support meeting (date)`
- referral-date fields for CoSS, CAS, New Beginnings, SAVTE, SCC, SOLACE and Ukraine Therapeutic Project.

Housing provider/outcome/referral-outcome/overall-outcome fields are configurable because the supplied Airtable field list did not establish a single unambiguous provider/outcome field. Do not infer provider identity from unrelated fields.

## External-source configuration

The reporting layer reuses the existing Trello API credentials and board ID, but its optional list scope is independent of the operational WhatsApp sync:

```text
TRELLO_API_KEY=...
TRELLO_API_TOKEN=...
TRELLO_BOARD_ID=651c07f8f94359a91f17bec5
REPORT_TRELLO_LIST_IDS=...    # optional; empty means scan the board
REPORT_SYNC_EXTERNAL_SOURCES=true
```

Power Automate does not require any new credential in this Option A design. It only needs to keep writing the standard Outlook comments to the matching Trello cards.

## Run locally

From the project root:

```bash
source /home/springvolunteer/.env
source /home/springvolunteer/pyenv/bin/activate
python -m reporting.reporting weekly
python -m reporting.reporting monthly
python -m reporting.reporting quarterly
```

Skip Excel when needed:

```bash
python -m reporting.reporting weekly --no-xlsx
```

## Outputs

Generated client/service data is stored outside the Git repository:

```text
/home/springvolunteer/Airtable2Trello/
  reports/
    weekly/
    monthly/
    quarterly/
  reporting_data/
    reporting.db
```

## Suggested cron schedule

A daily collector/reporting run is preferable for history. Once the database is collecting snapshots, reports can be generated separately. Example:

```cron
15 2 * * * /home/springvolunteer/airtable-trello-drive/run_reporting.sh daily >> /home/springvolunteer/Airtable2Trello/reporting.log 2>&1
0 7 * * 1 /home/springvolunteer/airtable-trello-drive/run_reporting.sh weekly >> /home/springvolunteer/Airtable2Trello/reporting.log 2>&1
15 7 1 * * /home/springvolunteer/airtable-trello-drive/run_reporting.sh monthly >> /home/springvolunteer/Airtable2Trello/reporting.log 2>&1
30 7 1 1,4,7,10 * /home/springvolunteer/airtable-trello-drive/run_reporting.sh quarterly >> /home/springvolunteer/Airtable2Trello/reporting.log 2>&1
```

The `daily` mode is provided by `python -m reporting.daily_snapshot`; schedule it daily to preserve historical snapshots and dated events.

## Generated data is local-only

The SQLite database and generated PDF/CSV/XLSX reports contain service data and are intentionally stored under `/home/springvolunteer/Airtable2Trello/`, outside the Git repository. They are therefore not part of the Git commit.

## First deployment

```bash
cd /home/springvolunteer/airtable-trello-drive
source /home/springvolunteer/.env
source /home/springvolunteer/pyenv/bin/activate
python -m compileall -q reporting
python -m reporting.daily_snapshot
python -m reporting.reporting weekly --no-xlsx
```

After `artifact_tool` is available in the runtime, omit `--no-xlsx` for the Excel workbook as well.
