# SPRING reporting deployment

Copy the `reporting/` directory and `run_reporting.sh` to:

`/home/springvolunteer/airtable-trello-drive/`

Generated reports and the historical reporting database are kept outside the Git repository under:

`/home/springvolunteer/Airtable2Trello/`

The reporting process uses the existing `/home/springvolunteer/.env` for Airtable credentials and optional staff filtering.

## Dependencies

Activate the existing virtual environment and install:

```bash
source /home/springvolunteer/pyenv/bin/activate
pip install -r reporting/requirements_reporting.txt
```

The XLSX exporter uses `artifact_tool` in environments where it is available. PDF and CSV generation do not depend on it.

## Configuration

Optional additions to `/home/springvolunteer/.env`:

```bash
REPORT_DB_PATH=/home/springvolunteer/Airtable2Trello/reporting_data/reporting.db
REPORT_OUTPUT_DIR=/home/springvolunteer/Airtable2Trello/reports
WHATSAPP_DB_PATH=/home/springvolunteer/Airtable2Trello/whatsapp_service.db
REPORT_ALLOWED_STAFF=
REPORT_HOUSING_PROVIDER_FIELD=Housing Provider / Organisation
REPORT_HOUSING_OUTCOME_FIELD=Housing Outcome
REPORT_REFERRAL_OUTCOME_FIELD=Referral Outcome
REPORT_OVERALL_OUTCOME_FIELD=Overall Case Outcome
```

## First run

```bash
cd /home/springvolunteer/airtable-trello-drive
source /home/springvolunteer/.env
source /home/springvolunteer/pyenv/bin/activate
python -m reporting.daily_snapshot
python -m reporting.reporting weekly --no-xlsx
```

## Suggested scheduling

Daily historical snapshot:

```cron
15 2 * * * /home/springvolunteer/airtable-trello-drive/run_reporting.sh daily >> /home/springvolunteer/Airtable2Trello/reporting.log 2>&1
```

Weekly report:

```cron
0 7 * * 1 /home/springvolunteer/airtable-trello-drive/run_reporting.sh weekly >> /home/springvolunteer/Airtable2Trello/reporting.log 2>&1
```

Monthly report:

```cron
15 7 1 * * /home/springvolunteer/airtable-trello-drive/run_reporting.sh monthly >> /home/springvolunteer/Airtable2Trello/reporting.log 2>&1
```

Quarterly report:

```cron
30 7 1 1,4,7,10 * /home/springvolunteer/airtable-trello-drive/run_reporting.sh quarterly >> /home/springvolunteer/Airtable2Trello/reporting.log 2>&1
```

## Git safety

Generated reports and the reporting database are ignored. Commit the reporting Python package and launch scripts, not client data.
