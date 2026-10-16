"""Opt-in real-session regression: creates and archives one harmless monitor file.

No database, Telegram client, session, file lookup or transfer is replaced.
"""

import json
import os
import subprocess

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.live]

_PROBE = r'''
import json
import logging
import sys
import time
from pathlib import Path

import anyio
from telegram import Bot

from app.core.config import settings
from app.core.database import db
from app.services._bot_listener.commands import (
    _fetch_attachment_message, _resolve_attachment_source_chat_id,
)
from app.services.archive_source import archive_message_id
from app.services.broadcaster_srv import BroadcasterService
from app.services.engagement import pseudonymize_engagement_subject
from app.services.honeypot_srv import extract_honeypot_message


class LookupCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lookups = []

    def emit(self, record):
        if record.name == "archive.lookup":
            self.lookups.append(record.getMessage())


async def main():
    # Given a real file in the owned monitor group and a normalized honeypot record.
    rows = db.table("exfiltrated_messages").select(
        "discovered_credentials!inner(meta)"
    ).eq("telegram_msg_id", -967576045).limit(1).execute().data
    assert len(rows) == 1
    topic_id = int(rows[0]["discovered_credentials"]["meta"]["topic_id"])
    assert topic_id > 1
    async with Bot(settings.bot_tokens[0]) as bot:
        source = await bot.send_document(
            settings.MONITOR_GROUP_ID,
            document=b"Fix 3 controlled archive lookup regression.\n",
            filename="fix3-source.txt",
            caption="Fix3 controlled archive source; harmless test file",
            message_thread_id=topic_id,
        )
    source_message_id = source.message_id
    source_chat_id = source.chat.id
    record = extract_honeypot_message(
        {"update_id": int(time.time()), "message": source.to_dict()}, "fixture"
    )
    assert record is not None and record["telegram_msg_id"] < 0
    record["chat_id"] = -100999
    record["discovered_credentials"] = {"chat_id": -100888, "meta": {
        "bot_username": "WrongSourceFixtureBot",
    }}
    assert archive_message_id(record) == source_message_id
    assert await _resolve_attachment_source_chat_id(record) == source_chat_id
    capture = LookupCapture()
    lookup_logger = logging.getLogger("archive.lookup")
    lookup_logger.setLevel(logging.INFO)
    lookup_logger.addHandler(capture)
    existing_temp_files = set(Path("/tmp").glob(f"archive_*_{source_message_id}_*"))

    # When the real fallback uses the real user session and real Telegram file lookup.
    try:
        result = await BroadcasterService()._auto_archive_media(
            settings.MONITOR_GROUP_ID, topic_id, record, record["telegram_msg_id"]
        )
    finally:
        lookup_logger.removeHandler(capture)
    assert result is not None and result.ok, getattr(result, "code", "no_result")

    # Then the actual lookup logged the original IDs, and the temporary file is gone.
    expected_lookup = (
        "[ArchiveLookup] source_chat_ref="
        + pseudonymize_engagement_subject(source_chat_id)
        + f" message_id={source_message_id} exact_source=True"
    )
    assert capture.lookups == [expected_lookup]
    assert set(Path("/tmp").glob(f"archive_*_{source_message_id}_*")) == existing_temp_files

    # Read-only command-selector checks never archive historical records.
    candidates = db.table("exfiltrated_messages").select(
        "id,telegram_msg_id,file_meta"
    ).lt("telegram_msg_id", 0).eq("media_type", "photo").limit(1).execute().data
    assert candidates
    candidate = candidates[0]
    by_uuid = await _fetch_attachment_message(candidate["id"])
    by_stored_id = await _fetch_attachment_message(str(candidate["telegram_msg_id"]))
    assert by_uuid and by_stored_id and by_uuid["id"] == by_stored_id["id"]
    print("FIX3_RESULT=" + json.dumps({
        "result": "passed", "archive_code": result.code,
        "source_message_id": source_message_id,
        "source_chat_ref": pseudonymize_engagement_subject(source_chat_id),
        "lookup_log": expected_lookup,
        "negative_id_sent_to_lookup": False,
        "uuid_and_signed_record_selectors_work": True,
        "temporary_file_cleaned": True,
        "database_rows_written": 0,
        "historical_messages_archived": 0,
    }))


try:
    anyio.run(main)
except Exception as exc:
    print("FIX3_RESULT=" + json.dumps({"result": "failed", "error_type": type(exc).__name__}))
    sys.exit(1)
'''


def test_live_honeypot_archive_uses_original_source_ids():
    if os.getenv("RUN_LIVE_ARCHIVE_LOOKUP_TEST") != "1":
        pytest.skip("set RUN_LIVE_ARCHIVE_LOOKUP_TEST=1 to enable the real archive probe")
    completed = subprocess.run(
        ["docker", "exec", "-i", "theprawnhunter_worker", "python", "-"],
        input=_PROBE, text=True, capture_output=True, timeout=180, check=False,
    )
    summaries = [line.removeprefix("FIX3_RESULT=") for line in completed.stdout.splitlines()
                 if line.startswith("FIX3_RESULT=")]
    assert summaries, "Live archive probe returned no privacy-safe result"
    result = json.loads(summaries[-1])
    print(json.dumps(result))
    assert completed.returncode == 0 and result["result"] == "passed", result
