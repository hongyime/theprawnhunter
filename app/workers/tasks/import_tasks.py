"""
CSV Import Pipeline (MISSING-001)
Scans the /app/imports directory for .csv files, validates and inserts tokens,
then moves processed files to /app/imports/processed/.

Supported CSV format:
    token,chat_id
    1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx,-1001234567890
    9876543210:AAyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy,

Scheduled: every 5 minutes via Celery Beat (system.import_csv)
"""
import csv
import logging
from pathlib import Path

from app.workers.celery_app import app, get_worker_loop

logger = logging.getLogger("import.tasks")

IMPORTS_DIR = Path("/app/imports")
PROCESSED_DIR = IMPORTS_DIR / "processed"


@app.task(name="system.import_csv")
def import_csv():
    """
    Scan imports/ for .csv files, validate tokens, insert to DB,
    and move processed files to imports/processed/.
    """
    return get_worker_loop().run_until_complete(_import_csv_logic())


async def _import_csv_logic() -> str:
    from app.workers.tasks.flow_tasks import get_broadcaster
    from app.workers.tasks.scanner_tasks import _save_credentials_async

    # Ensure imports directory exists
    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    # Recover any .pending files left by a previous crashed run — rename back to .csv
    for stuck in IMPORTS_DIR.glob("*.pending"):
        try:
            stuck.rename(stuck.with_suffix(".csv"))
            logger.info(f"[CSV Import] Recovered stuck pending file: {stuck.name} → {stuck.stem}.csv")
        except OSError as e:
            logger.warning(f"[CSV Import] Could not recover {stuck.name}: {e}")

    csv_files = list(IMPORTS_DIR.glob("*.csv"))
    if not csv_files:
        return "No CSV files to import."

    logger.info(f"[CSV Import] Found {len(csv_files)} file(s) to process.")
    broadcaster = get_broadcaster()
    await broadcaster.send_log(f"📂 **CSV Import**: Processing {len(csv_files)} file(s)...")

    total_imported = 0
    total_files = 0

    for csv_path in csv_files:
        # Atomically claim the file by renaming to .pending
        pending_path = csv_path.with_suffix(".pending")
        try:
            csv_path.rename(pending_path)
        except OSError as e:
            logger.warning(f"[CSV Import] Could not claim {csv_path.name}: {e}")
            continue

        results = []
        try:
            with open(pending_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    token = (row.get("token") or "").strip()
                    chat_id_raw = (row.get("chat_id") or "").strip()
                    chat_id = int(chat_id_raw) if chat_id_raw.lstrip("-").isdigit() else None

                    if token:
                        results.append({
                            "token": token,
                            "chat_id": chat_id,
                            "meta": {"ingested_via": "csv_import", "filename": csv_path.name},
                        })
        except Exception as e:
            logger.error(f"[CSV Import] Failed to read {pending_path.name}: {e}")
            # Move to processed even on read failure to avoid infinite retry
            pending_path.rename(PROCESSED_DIR / pending_path.name)
            continue

        saved = 0
        if results:
            try:
                saved = await _save_credentials_async(results, "csv_import")
                total_imported += saved
            except Exception as e:
                logger.error(f"[CSV Import] Save failed for {pending_path.name}: {e}")

        # Move to processed/
        try:
            pending_path.rename(PROCESSED_DIR / pending_path.name)
            total_files += 1
            logger.info(f"[CSV Import] {pending_path.name}: {saved} credentials imported → processed/")
        except OSError as e:
            logger.warning(f"[CSV Import] Could not move {pending_path.name} to processed/: {e}")

    result = f"CSV import complete. {total_imported} credentials from {total_files} file(s)."
    logger.info(f"[CSV Import] {result}")
    if total_imported > 0:
        await broadcaster.send_log(f"✅ **CSV Import**: {result}")
    return result
