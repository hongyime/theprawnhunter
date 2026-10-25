#!/usr/bin/env python3
"""Assert the operator docs contain the sections and warnings the plan requires.
Run from anywhere: python scripts/dev/check_ops_docs.py
Exit 0 when every item is present, 1 with a per-item report otherwise.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REQUIRED = {
    ROOT / "docs/ops/outposts.md": [
        "## Activation checklist",
        "## Security model",
        "## Rollback",
        "## Troubleshooting",
    ],
    ROOT / "docs/ops/oracle-always-free-runbook.md": [
        "## Always Free envelope",
        "## Charge traps",
        "## Guardrails",
        "## Trial to Always Free",
        "## Teardown",
        "## Verification commands",
    ],
}

ORACLE_DOC = ROOT / "docs/ops/oracle-always-free-runbook.md"
ORACLE_WARNING = "do not upgrade"

def main() -> int:
    missing: list[str] = []
    for path, headings in REQUIRED.items():
        label = path.relative_to(ROOT)
        if not path.is_file():
            missing.append(f"{label}: file is missing")
            continue
        text = path.read_text(encoding="utf-8")
        for heading in headings:
            if heading not in text:
                missing.append(f"{label}: missing heading {heading!r}")

    oracle_text = ORACLE_DOC.read_text(encoding="utf-8") if ORACLE_DOC.is_file() else ""
    if ORACLE_WARNING not in oracle_text.lower():
        missing.append(f"{ORACLE_DOC.relative_to(ROOT)}: missing warning {ORACLE_WARNING!r}")

    if missing:
        print("FAIL: ops docs are incomplete")
        for item in missing:
            print(f"  - {item}")
        return 1

    print("OK: ops docs contain all required sections and warnings")
    return 0

if __name__ == "__main__":
    sys.exit(main())
