# Archived one-off operator scripts

These scripts were moved out of the active `scripts/` directory because they are
manual recovery and inspection tools, not part of the normal deployment flow.
They are retained for reference and may require operator review before use.

The three media backfills are dry-run by default. Pass `--apply` only when a
database update or Telegram broadcast is intended. `get_account_names.py` is a
read-only local account/session inspection utility.
