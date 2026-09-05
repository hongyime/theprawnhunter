"""Opt-in live Supabase read/write/delete round-trip."""

import hashlib
import os
from uuid import uuid4

import pytest

from supabase import create_client

pytestmark = [pytest.mark.integration, pytest.mark.live]


def test_supabase_read_write_round_trip():
    if os.getenv("ALLOW_SUPABASE_WRITE") != "1":
        pytest.skip("set ALLOW_SUPABASE_WRITE=1 to enable the live database probe")

    url = os.getenv("LIVE_SUPABASE_URL")
    service_role_key = os.getenv("LIVE_SUPABASE_SERVICE_ROLE_KEY")
    if not url or not service_role_key:
        pytest.fail(
            "LIVE_SUPABASE_URL and LIVE_SUPABASE_SERVICE_ROLE_KEY are required "
            "when the live database probe is enabled"
        )

    client = create_client(url, service_role_key)
    nonce = uuid4().hex
    token_hash = hashlib.sha256(f"qa-live-probe-{nonce}".encode()).hexdigest()
    new_id = None

    try:
        inserted = (
            client.table("discovered_credentials")
            .insert(
                {
                    "bot_token": f"QA_LIVE_PROBE_{nonce}",
                    "token_hash": token_hash,
                    "source": "QA_LIVE_PROBE",
                    "status": "pending",
                }
            )
            .execute()
        )
        assert inserted.data, "insert returned no row"
        new_id = inserted.data[0]["id"]

        selected = (
            client.table("discovered_credentials")
            .select("id,token_hash")
            .eq("id", new_id)
            .execute()
        )
        assert len(selected.data) == 1
        assert selected.data[0]["token_hash"] == token_hash
    finally:
        if new_id is not None:
            client.table("discovered_credentials").delete().eq("id", new_id).execute()
