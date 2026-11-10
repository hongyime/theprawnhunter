"""Opt-in fresh capture: verifies marked storage and the delivered text preview."""

import json
import os
import subprocess

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.live]

_PROBE = r'''
import json
import sys
import time

import anyio
import httpx
from telegram import Bot
from telegram.request import HTTPXRequest

from app.core.config import settings
from app.core.database import db
from app.workers.tasks.flows import honeypot as flow

PHASE = "preflight"


async def main():
    global PHASE
    source = db.table("exfiltrated_messages").select("credential_id").eq(
        "telegram_msg_id", -967576045
    ).limit(1).execute().data
    assert len(source) == 1
    credential_id = source[0]["credential_id"]
    uid = int(time.time())
    assert not db.table("honeypot_updates").select("id").eq(
        "credential_id", credential_id
    ).eq("update_id", uid).execute().data
    full_text = "Fix5 controlled preview check. " + "x" * 3450 + " END-FIX5-FULL-TEXT"
    payload = {"update_id": uid, "message": {
        "message_id": 0, "from": {"first_name": "Fix5LiveCheck"},
        "chat": {"id": 0}, "text": full_text,
    }}
    PHASE = "capture"
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        response = await client.post(
            f"http://api:8001/honeypot/receive/{credential_id}",
            headers={"X-Telegram-Bot-Api-Secret-Token": settings.HONEYPOT_SECRET}, json=payload,
        )
        assert response.status_code == 200
    rows = db.table("exfiltrated_messages").select("id,content,file_meta").eq(
        "credential_id", credential_id
    ).eq("telegram_msg_id", -uid).execute().data
    assert len(rows) == 1
    row = rows[0]
    assert row["content"] == full_text[:2000]
    assert row["file_meta"]["content_truncated"] is True
    assert row["file_meta"]["content_original_length"] == len(full_text)
    confirmations = []
    requests = []

    def observe_reply(owner):
        async def observe(response):
            method = response.request.url.path.rsplit("/", 1)[-1]
            if method not in {"sendMessage", "sendDocument"}:
                return
            await response.aread()
            assert method == "sendMessage", "honeypot evidence must not be sent as a document"
            reply = response.json()
            if not reply.get("ok"):
                return
            result = reply["result"]
            if method == "sendMessage":
                text = result["text"]
                assert text.startswith(f"[ID: {-uid}] [From: Fix5LiveCheck]\n")
                assert "Preview only" in text and "evidence JSON file" in text
                assert "END-FIX5-FULL-TEXT" not in text
                assert len(text.encode("utf-16-le")) // 2 <= 1024
                confirmations.append(result["message_id"])
        return observe

    broadcaster = flow.get_broadcaster()
    for token in broadcaster.bot_tokens:
        request = HTTPXRequest(httpx_kwargs={"event_hooks": {"response": [observe_reply(token)]}})
        requests.append(request)
        broadcaster._bots[token] = Bot(token, request=request, get_updates_request=request)
    PHASE = "broadcast"
    try:
        result = await flow._broadcast_single_honeypot_message(credential_id, -uid)
        stored = db.table("exfiltrated_messages").select(
            "content,file_meta,is_broadcasted,broadcast_message_id"
        ).eq("id", row["id"]).execute().data[0]
        assert stored["is_broadcasted"] and confirmations == [stored["broadcast_message_id"]]
        assert stored["file_meta"]["content_truncated"] is True
        assert stored["file_meta"]["honeypot_evidence_file_status"] == "deferred"
        print("FIX5_RESULT=" + json.dumps({
            "result": "passed", "update_id": uid,
            "broadcast_message_id": confirmations[0],
            "original_length": len(full_text), "stored_length": len(stored["content"]),
            "stored_truncation_flag": stored["file_meta"]["content_truncated"],
            "preview_notice_confirmed_by_telegram": True, "evidence_document_sent": False,
            "historical_messages_rebroadcast": 0,
        }))
    finally:
        for request in requests:
            await request.shutdown()


try:
    anyio.run(main)
except Exception as exc:
    print("FIX5_RESULT=" + json.dumps({"result": "failed", "phase": PHASE, "error_type": type(exc).__name__}))
    sys.exit(1)
'''


def test_live_over_limit_message_has_preview_without_evidence_document():
    if os.getenv("RUN_LIVE_PREVIEW_TEST") != "1":
        pytest.skip("set RUN_LIVE_PREVIEW_TEST=1 to capture one fresh controlled message")
    completed = subprocess.run(
        ["docker", "exec", "-i", "theprawnhunter_worker", "python", "-"],
        input=_PROBE, text=True, capture_output=True, timeout=180, check=False,
    )
    summaries = [line.removeprefix("FIX5_RESULT=") for line in completed.stdout.splitlines()
                 if line.startswith("FIX5_RESULT=")]
    assert summaries, "Live preview probe returned no privacy-safe result"
    result = json.loads(summaries[-1])
    print(json.dumps(result))
    assert completed.returncode == 0 and result["result"] == "passed", result
