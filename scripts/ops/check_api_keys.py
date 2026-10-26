"""Test every configured provider credential and report pass/fail.

Exit code is 0 when every *configured* key works, 1 when any of them fails.
"""

from __future__ import annotations

import asyncio
import sys

from app.services.api_key_health import check_all


def main() -> int:
    results = asyncio.run(check_all())
    failing = 0
    for row in results:
        status = "PASS" if row["ok"] else "FAIL"
        if not row["ok"] and row["detail"] != "not configured":
            failing += 1
        print(f"{status} | {row['provider']:16} | {row['detail'][:90]}")
    print(f"\n{len(results)} checks, {failing} failing")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
