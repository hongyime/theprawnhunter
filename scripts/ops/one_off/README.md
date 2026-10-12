# One-off operator scripts

These scripts are manual recovery, credential-maintenance, and inspection tools;
they are not part of the normal deployment flow.

Use the single media-backfill CLI for all three historical modes:

```bash
python scripts/ops/backfill_media.py all --dry-run
python scripts/ops/backfill_media.py since --since 2026-07-20T00:00:00Z --dry-run
python scripts/ops/backfill_media.py telethon --dry-run
```

Every mode defaults to dry-run. `--apply` explicitly enables its database
updates; in `telethon` mode it can send media to Telegram. The old filenames in
this directory remain compatibility wrappers to the corresponding CLI modes.
`get_account_names.py` is a read-only local account/session inspection utility.
