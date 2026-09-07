"""Safety contract for automated outbound finding alerts."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_finding_alerts_default_to_disabled():
    from app.core.config import Settings

    field = Settings.model_fields["FINDING_ALERTS_ENABLED"]
    assert field.default is False

    template = (ROOT / ".env.template").read_text(encoding="utf-8")
    assert "FINDING_ALERTS_ENABLED=False" in template


def test_alert_workers_check_gate_before_delivery_code():
    source = (ROOT / "app" / "workers" / "tasks" / "flow_tasks.py").read_text(
        encoding="utf-8"
    )
    for name in ("_route_finding_alerts_logic", "_weekly_finding_alerts_logic"):
        start = source.index(f"async def {name}")
        end = source.find("\nasync def ", start + 1)
        body = source[start : end if end != -1 else len(source)]
        gate = body.find("FINDING_ALERTS_ENABLED")
        delivery_import = body.find("from app.services.finding_alerts")
        assert gate != -1, f"{name} has no outbound authorization gate"
        assert gate < delivery_import, f"{name} checks the gate after delivery code"
