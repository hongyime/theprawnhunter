# Imports Directory

Drop your CSV files here for automatic import on container startup.

## Supported Formats

```csv
token,chat_id
1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx,-1001234567890
9876543210:AAyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy,
```

## How It Works

1. Drop any `.csv` files in this folder
2. Restart Docker: `docker compose restart`
3. Files are renamed to `.pending` and moved to `processed/`

**NOTE**: The CSV import pipeline is now IMPLEMENTED. See `app/workers/tasks/import_tasks.py` and Celery beat schedule (runs every 5 minutes). Files are processed, validated, inserted to DB, and moved to `processed/`.

## Also Accepted

- `fofa_scraper_*.csv` files in project root
- `import_tokens.csv` in project root (legacy)
