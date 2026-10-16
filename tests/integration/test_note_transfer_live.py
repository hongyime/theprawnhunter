"""Opt-in live transfer regression; creates only fresh harmless monitor-group media."""

import base64
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.live]

_PROBE = r'''
import base64
import hashlib
import json
import os
import re
import sys
import time
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import anyio
from telegram import Bot

from app.core.config import settings
from app.core.database import db
from app.services._user_agent.common import ARCHIVE_TMP_DIR, ARCHIVE_TMP_PREFIX
from app.services.broadcaster_srv import BroadcasterService
from app.services.honeypot_srv import extract_honeypot_message
from app.services.note_transfer import NoteTransfer, relay_note_media

PHASE = "preflight"


async def main():
    global PHASE
    rows = db.table("exfiltrated_messages").select(
        "discovered_credentials!inner(meta)"
    ).eq("telegram_msg_id", -967576045).limit(1).execute().data
    assert len(rows) == 1
    topic_id = int(rows[0]["discovered_credentials"]["meta"]["topic_id"])
    source_token = settings.bot_tokens[0]
    broadcaster = BroadcasterService()
    results = []
    temporary_pattern = f"{ARCHIVE_TMP_PREFIX}note_{os.getpid()}_*"
    before = set(Path(ARCHIVE_TMP_DIR).glob(temporary_pattern))
    try:
        async with Bot(source_token) as source:
            me = await source.get_me()
            member = await source.get_chat_member(settings.MONITOR_GROUP_ID, me.id)
            assert member.status in {"administrator", "creator"}
            with TemporaryDirectory(prefix="prawn-fixture-") as directory:
                for index, kind in enumerate(("voice", "video_note", "sticker")):
                    PHASE = f"source_upload_{kind}"
                    suffix = {"voice": ".ogg", "video_note": ".mp4", "sticker": ".webp"}[kind]
                    path = Path(directory) / f"source{suffix}"
                    path.write_bytes(base64.b64decode(FIXTURES[kind]))
                    if kind == "voice":
                        message = await source.send_voice(settings.MONITOR_GROUP_ID, path, message_thread_id=topic_id)
                    elif kind == "video_note":
                        message = await source.send_video_note(settings.MONITOR_GROUP_ID, path, message_thread_id=topic_id)
                    else:
                        message = await source.send_sticker(settings.MONITOR_GROUP_ID, path, message_thread_id=topic_id)
                    assert getattr(message, kind) is not None
                    record = extract_honeypot_message({
                        "update_id": int(time.time()) + index, "message": message.to_dict(),
                    }, "controlled-owned-source")
                    assert record is not None and record["media_type"] == kind
                    record["sender_name"] = "Fix2LiveCheck"
                    record["content"] = f"Fix2 controlled {kind} transfer"
                    metadata = deepcopy(record["file_meta"])
                    PHASE = f"relay_{kind}"
                    target = broadcaster._get_bot_instance(
                        broadcaster.bot_tokens[index % len(broadcaster.bot_tokens)]
                    )
                    summary = (
                        f"[ID: {record['telegram_msg_id']}] [From: Fix2LiveCheck]\n"
                        + record["content"] + f"\n\n[{kind} Media Detected]"
                    )
                    delivered = await relay_note_media(source, target, NoteTransfer(
                        kind, None, metadata["file_id"], settings.MONITOR_GROUP_ID, topic_id, summary
                    ))
                    assert record["file_meta"] == metadata
                    arrived = getattr(delivered.media, kind)
                    assert arrived is not None and arrived.file_size > 0
                    assert delivered.summary.text == summary
                    PHASE = f"verify_{kind}"
                    original_file = await source.get_file(metadata["file_id"])
                    original_bytes = await original_file.download_as_bytearray()
                    target_file = await target.get_file(arrived.file_id)
                    target_bytes = await target_file.download_as_bytearray()
                    assert hashlib.sha256(original_bytes).digest() == hashlib.sha256(target_bytes).digest()
                    assert set(Path(ARCHIVE_TMP_DIR).glob(temporary_pattern)) == before
                    results.append({"kind": kind, "source_message_id": message.message_id,
                                    "media_message_id": delivered.media.message_id,
                                    "summary_message_id": delivered.summary.message_id,
                                    "file_arrived": True, "content_matches": True, "temp_cleaned": True})
    finally:
        for bot in broadcaster._bots.values():
            await bot.request.shutdown()
    print("FIX2_RESULT=" + json.dumps({"result": "passed", "transfers": results,
          "live_path": "shared real relay with owned administrator source",
          "database_rows_written": 0, "historical_messages_rebroadcast": 0, "transcription": False}))


try:
    anyio.run(main)
except Exception as exc:
    detail = re.sub(r"https?://\S+", "[URL]", str(exc))
    detail = re.sub(r"\b\d+:[A-Za-z0-9_-]+", "[TOKEN]", detail)
    detail = re.sub(r"[A-Za-z0-9_+/=-]{20,}", "[REDACTED]", detail)
    print("FIX2_RESULT=" + json.dumps({"result": "failed", "error_type": type(exc).__name__,
          "phase": PHASE, "sanitized_detail": detail[:200]}))
    sys.exit(1)
'''


def test_live_voice_video_note_and_sticker_transfer():
    if os.getenv("RUN_LIVE_NOTE_TRANSFER_TEST") != "1":
        pytest.skip("set RUN_LIVE_NOTE_TRANSFER_TEST=1 to create and transfer controlled media")
    tool = subprocess.run(
        ["uv", "run", "--no-project", "--with", "imageio-ffmpeg", "python", "-c",
         "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert tool.returncode == 0, "Temporary fixture generator unavailable"
    ffmpeg = tool.stdout.strip()
    with TemporaryDirectory(prefix="prawn-live-fixture-") as directory:
        root = Path(directory)
        commands = {
            "voice": ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "1",
                      "-c:a", "libopus", "-b:a", "16k", str(root / "voice.ogg")],
            "video_note": ["-f", "lavfi", "-i", "color=c=blue:s=256x256:r=15", "-t", "1",
                           "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                           str(root / "video_note.mp4")],
            "sticker": ["-f", "lavfi", "-i", "color=c=blue:s=512x512", "-frames:v", "1",
                        "-c:v", "libwebp", "-lossless", "1", str(root / "sticker.webp")],
        }
        fixtures = {}
        for kind, arguments in commands.items():
            generated = subprocess.run([ffmpeg, "-y", "-loglevel", "error", *arguments],
                                       capture_output=True, timeout=30, check=False)
            assert generated.returncode == 0, f"Could not generate controlled {kind} fixture"
            fixtures[kind] = base64.b64encode(Path(arguments[-1]).read_bytes()).decode()
        completed = subprocess.run(
            ["docker", "exec", "-i", "theprawnhunter_worker", "python", "-"],
            input="FIXTURES = " + repr(fixtures) + "\n" + _PROBE,
            capture_output=True, text=True, timeout=240, check=False,
        )
    summaries = [line.removeprefix("FIX2_RESULT=") for line in completed.stdout.splitlines()
                 if line.startswith("FIX2_RESULT=")]
    assert summaries, "Live transfer probe returned no privacy-safe result"
    result = json.loads(summaries[-1])
    print(json.dumps(result))
    assert completed.returncode == 0 and result["result"] == "passed", result
