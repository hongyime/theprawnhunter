"""
Behavior tests for HONEYPOT_REDIRECT_AUTHORIZED gate (Plan Item 1).

Verifies:
1. Default configuration is OFF (both HONEYPOT_REDIRECT_AUTHORIZED and
   the resulting outbound behavior).
2. Each of the 4 redirect tasks (sweep, touch2, touch3, proactive) SHORT
   CIRCUITS with {"status": "skipped", "reason": "not_authorized"} when the
   gate is OFF, without touching the DB or sending anything.
3. When the gate is ON, tasks proceed past the check (they hit downstream
   logic — proven by observing DB / bot-token lookups fire).
4. Three-touch code paths are still PRESENT in the source tree (i.e. the
   feature was not deleted, only gated).
"""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).parents[2]
REDIRECT_TASKS = REPO_ROOT / "app" / "workers" / "tasks" / "honeypot_redirect_tasks.py"
FLOW_TASKS = REPO_ROOT / "app" / "workers" / "tasks" / "flow_tasks.py"


# ---------------------------------------------------------------------------
# 1. Default config is OFF
# ---------------------------------------------------------------------------

def test_default_config_authorized_is_off():
    """HONEYPOT_REDIRECT_AUTHORIZED must default to False so a fresh clone
    of the repo never sends outbound redirects until an operator opts in."""
    from app.core.config import Settings

    # Read the class-level default directly to avoid picking up a leaked
    # env-var from the pytest process.
    field = Settings.model_fields["HONEYPOT_REDIRECT_AUTHORIZED"]
    assert field.default is False, (
        "HONEYPOT_REDIRECT_AUTHORIZED must default to False. "
        f"Got: {field.default!r}"
    )


def test_env_template_declares_authorized_false():
    """.env.template must ship HONEYPOT_REDIRECT_AUTHORIZED=false so
    operators see the gate exists and start with it disabled."""
    template = (REPO_ROOT / ".env.template").read_text(encoding="utf-8")
    assert "HONEYPOT_REDIRECT_AUTHORIZED" in template, (
        ".env.template must document HONEYPOT_REDIRECT_AUTHORIZED"
    )
    # Case-insensitive match on the =false side.
    lines = [ln.strip() for ln in template.splitlines()
             if ln.strip().startswith("HONEYPOT_REDIRECT_AUTHORIZED")]
    assert lines, "no HONEYPOT_REDIRECT_AUTHORIZED= line found"
    assert any("=false" in ln.lower() for ln in lines), (
        f"HONEYPOT_REDIRECT_AUTHORIZED must default to false in .env.template. "
        f"Got: {lines!r}"
    )


# ---------------------------------------------------------------------------
# 2. Gate OFF short-circuits each task
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_sweep_skipped_when_not_authorized():
    """flow.honeypot_redirect_sweep must skip when the gate is OFF, without
    touching the DB or dispatching any downstream redirect tasks."""
    from app.workers.tasks import flow_tasks

    fake_db = MagicMock()
    fake_send = MagicMock()

    with patch.object(flow_tasks.settings, "HONEYPOT_REDIRECT_MODE", True), \
         patch.object(flow_tasks.settings, "HONEYPOT_REDIRECT_AUTHORIZED", False), \
         patch.object(flow_tasks, "db", fake_db), \
         patch.object(flow_tasks.app, "send_task", fake_send):
        result = _run(flow_tasks._honeypot_redirect_sweep_logic())

    assert result == {"status": "skipped", "reason": "not_authorized"}, (
        f"sweep must return skipped/not_authorized when gate is OFF. Got: {result!r}"
    )
    fake_db.table.assert_not_called()
    fake_send.assert_not_called()


def test_sweep_skipped_when_mode_off_regardless_of_authorized():
    """HONEYPOT_REDIRECT_MODE=False continues to short-circuit as before —
    the two switches are independent gates."""
    from app.workers.tasks import flow_tasks

    with patch.object(flow_tasks.settings, "HONEYPOT_REDIRECT_MODE", False), \
         patch.object(flow_tasks.settings, "HONEYPOT_REDIRECT_AUTHORIZED", True):
        result = _run(flow_tasks._honeypot_redirect_sweep_logic())

    assert result == {"status": "disabled"}


def test_touch2_skipped_when_not_authorized():
    from app.workers.tasks import honeypot_redirect_tasks as hrt

    fake_db = MagicMock()
    with patch.object(hrt.settings, "HONEYPOT_REDIRECT_AUTHORIZED", False), \
         patch.object(hrt, "db", fake_db):
        result = _run(hrt._redirect_touch2_logic())

    assert result == {"status": "skipped", "reason": "not_authorized"}, result
    fake_db.table.assert_not_called()


def test_touch3_skipped_when_not_authorized():
    from app.workers.tasks import honeypot_redirect_tasks as hrt

    fake_db = MagicMock()
    with patch.object(hrt.settings, "HONEYPOT_REDIRECT_AUTHORIZED", False), \
         patch.object(hrt, "db", fake_db):
        result = _run(hrt._redirect_touch3_logic())

    assert result == {"status": "skipped", "reason": "not_authorized"}, result
    fake_db.table.assert_not_called()


def test_proactive_skipped_when_not_authorized():
    from app.workers.tasks import honeypot_redirect_tasks as hrt

    fake_db = MagicMock()
    with patch.object(hrt.settings, "HONEYPOT_REDIRECT_AUTHORIZED", False), \
         patch.object(hrt, "db", fake_db):
        result = _run(hrt._proactive_outreach_logic())

    assert result == {"status": "skipped", "reason": "not_authorized"}, result
    fake_db.table.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Gate ON: tasks proceed past the check (hit DB / downstream)
# ---------------------------------------------------------------------------

def _make_empty_db_response():
    resp = MagicMock()
    resp.data = []
    return resp


def test_sweep_proceeds_when_authorized():
    """With the gate ON, sweep must move past the guard and start querying
    the DB. We stub the DB to return no rows so the task returns idle."""
    from app.workers.tasks import flow_tasks

    async def fake_async_execute(_query):
        return _make_empty_db_response()

    with patch.object(flow_tasks.settings, "HONEYPOT_REDIRECT_MODE", True), \
         patch.object(flow_tasks.settings, "HONEYPOT_REDIRECT_AUTHORIZED", True), \
         patch.object(flow_tasks, "async_execute", side_effect=fake_async_execute):
        result = _run(flow_tasks._honeypot_redirect_sweep_logic())

    # No rows in DB → sweep returns idle. The critical assertion is that we
    # did NOT get skipped/not_authorized — proving the gate opened.
    assert result.get("status") != "skipped", (
        f"sweep must NOT be skipped when gate is ON. Got: {result!r}"
    )
    assert result == {"status": "idle", "pending": 0}


def test_touch2_proceeds_when_authorized():
    from app.workers.tasks import honeypot_redirect_tasks as hrt

    async def fake_async_execute(_query):
        return _make_empty_db_response()

    with patch.object(hrt.settings, "HONEYPOT_REDIRECT_AUTHORIZED", True), \
         patch.object(hrt, "async_execute", side_effect=fake_async_execute):
        result = _run(hrt._redirect_touch2_logic())

    assert result.get("status") != "skipped"
    # Passed the gate → hit DB → no candidates.
    assert result == {"status": "ok", "sent": 0, "reason": "no_candidates"}


def test_touch3_proceeds_when_authorized():
    from app.workers.tasks import honeypot_redirect_tasks as hrt

    async def fake_async_execute(_query):
        return _make_empty_db_response()

    with patch.object(hrt.settings, "HONEYPOT_REDIRECT_AUTHORIZED", True), \
         patch.object(hrt, "async_execute", side_effect=fake_async_execute):
        result = _run(hrt._redirect_touch3_logic())

    assert result.get("status") != "skipped"
    assert result == {"status": "ok", "sent": 0, "reason": "no_candidates"}


def test_proactive_proceeds_when_authorized():
    from app.workers.tasks import honeypot_redirect_tasks as hrt

    async def fake_async_execute(_query):
        return _make_empty_db_response()

    with patch.object(hrt.settings, "HONEYPOT_REDIRECT_AUTHORIZED", True), \
         patch.object(hrt, "async_execute", side_effect=fake_async_execute):
        result = _run(hrt._proactive_outreach_logic())

    assert result.get("status") != "skipped"
    assert result == {"status": "ok", "sent": 0, "reason": "no_candidates"}


# ---------------------------------------------------------------------------
# 4. Three-touch code is PRESERVED (tasks still registered, logic still present)
# ---------------------------------------------------------------------------

def test_three_touch_tasks_still_registered():
    """Each of the three touch levels + the proactive outreach task must
    still be defined — gating disables execution, it must NOT delete code."""
    redirect_src = REDIRECT_TASKS.read_text(encoding="utf-8")

    for task_name in (
        "flow.honeypot_redirect_touch2",
        "flow.honeypot_redirect_touch3",
        "flow.honeypot_proactive_outreach",
    ):
        assert task_name in redirect_src, (
            f"{task_name} must still be registered in honeypot_redirect_tasks.py "
            f"— gating must not delete the tasks."
        )

    # Initial sweep lives in flow_tasks.py.
    flow_src = FLOW_TASKS.read_text(encoding="utf-8")
    assert "flow.honeypot_redirect_sweep" in flow_src, (
        "flow.honeypot_redirect_sweep must still be registered in flow_tasks.py"
    )


def test_three_touch_logic_bodies_intact():
    """The actual send-message logic for each touch level must still be
    present (checked by looking for the distinctive DB update columns)."""
    src = REDIRECT_TASKS.read_text(encoding="utf-8")

    for column in ("redirect_2_sent_at", "redirect_3_sent_at", "proactive_sent_at"):
        assert column in src, (
            f"{column} must still be referenced in honeypot_redirect_tasks.py "
            f"— gating must not remove the multi-touch state tracking."
        )


def test_gate_check_precedes_side_effects():
    """The HONEYPOT_REDIRECT_AUTHORIZED check must appear BEFORE any DB /
    Redis / bot-token side-effect in every gated function. Static-source
    verification protects against a future refactor that accidentally
    moves the check below a side effect."""
    src = REDIRECT_TASKS.read_text(encoding="utf-8")

    for fn_name in ("_redirect_touch2_logic", "_redirect_touch3_logic", "_proactive_outreach_logic"):
        fn_start = src.find(f"async def {fn_name}")
        assert fn_start != -1, f"{fn_name} not found"
        # Find the end of the function by locating the next `async def` /
        # `def ` at column 0, or EOF.
        next_fn = src.find("\nasync def ", fn_start + 1)
        next_sync = src.find("\ndef ", fn_start + 1)
        candidates = [x for x in (next_fn, next_sync) if x != -1]
        fn_end = min(candidates) if candidates else len(src)
        body = src[fn_start:fn_end]

        gate_pos = body.find("HONEYPOT_REDIRECT_AUTHORIZED")
        db_pos = body.find("db.table(")
        assert gate_pos != -1, f"{fn_name} missing HONEYPOT_REDIRECT_AUTHORIZED check"
        if db_pos != -1:
            assert gate_pos < db_pos, (
                f"{fn_name}: HONEYPOT_REDIRECT_AUTHORIZED check must appear "
                f"BEFORE any db.table() call. gate={gate_pos} db={db_pos}"
            )

    # Same check for the sweep function.
    flow_src = FLOW_TASKS.read_text(encoding="utf-8")
    sweep_start = flow_src.find("async def _honeypot_redirect_sweep_logic")
    assert sweep_start != -1
    next_fn = flow_src.find("\nasync def ", sweep_start + 1)
    sweep_body = flow_src[sweep_start:next_fn if next_fn != -1 else len(flow_src)]
    gate_pos = sweep_body.find("HONEYPOT_REDIRECT_AUTHORIZED")
    db_pos = sweep_body.find("db.table(")
    assert gate_pos != -1
    if db_pos != -1:
        assert gate_pos < db_pos, (
            "sweep: HONEYPOT_REDIRECT_AUTHORIZED check must precede db.table()"
        )
