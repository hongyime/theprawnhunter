"""Look up a domain, IP, URL or file hash on VirusTotal.

Usage:
    python scripts/ops/virustotal_lookup.py example.com
    python scripts/ops/virustotal_lookup.py https://example.com/path
    python scripts/ops/virustotal_lookup.py 44d88612fea8a8f36de82e1278abb02f
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.services.virustotal_srv import virustotal


def classify(value: str) -> str:
    lowered = value.lower()
    if lowered.startswith(("http://", "https://")):
        return "url"
    if len(value) in (32, 40, 64) and all(c in "0123456789abcdef" for c in lowered):
        return "file"
    if value.count(".") == 3 and all(part.isdigit() for part in value.split(".")):
        return "ip"
    return "domain"


async def run(value: str) -> dict | None:
    kind = classify(value)
    if kind == "url":
        return await virustotal.url_report(value)
    if kind == "file":
        return await virustotal.file_report(value)
    if kind == "ip":
        return await virustotal.ip_report(value)
    return await virustotal.domain_report(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("value")
    args = parser.parse_args(argv)

    if not virustotal.enabled:
        print("VIRUSTOTAL_API_KEY is not configured")
        return 2
    verdict = asyncio.run(run(args.value))
    if verdict is None:
        print("no verdict (unknown, quota exhausted, or API error)")
        return 1
    print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
