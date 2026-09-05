import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class TelemetryEntityParser:
    URL_PATTERN = re.compile(r"https?://[^\s<>'\"`)\]}]+", re.IGNORECASE)
    DOMAIN_PATTERN = re.compile(
        r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
        r"(?:com|net|org|io|ai|app|dev|co|xyz|top|site|online|store|tech|ru|cn|pw|cc|me|tv|biz|info|ws|ga|cf|ml|gq|dpdns\.org|duckdns\.org)\b",
        re.IGNORECASE,
    )
    CRYPTO_PATTERN = re.compile(
        r"\b(?:0x[a-fA-F0-9]{40}|[13][a-km-zA-HJ-NP-Z1-9]{25,34}|T[A-Za-z1-9]{33}|bc1[q-z0-9]{39,59})\b"
    )
    EMAIL_PATTERN = re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
        re.IGNORECASE
    )
    USERNAME_PATTERN = re.compile(r"@([a-zA-Z0-9_]{5,32})")
    PHONE_PATTERN = re.compile(
        r"\b\+?[1-9]\d{1,2}[\s-]?\(?\d{2,4}\)?[\s-]?\d{3,4}[\s-]?\d{3,4}\b"
    )
    IP_PATTERN = re.compile(
        r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"
    )

    @staticmethod
    def canonicalize_url(value: str) -> str:
        clean = (value or "").strip().strip("'\"]}>.,")
        if not clean:
            return ""

        try:
            parsed = urlsplit(clean)
        except ValueError:
            return clean.split("#", 1)[0].rstrip("/")

        if not parsed.scheme or not parsed.netloc:
            return clean.split("#", 1)[0].rstrip("/")

        userinfo = ""
        if parsed.username:
            userinfo = parsed.username
            if parsed.password:
                userinfo += f":{parsed.password}"
            userinfo += "@"

        host = (parsed.hostname or "").lower()
        port = ""
        try:
            if parsed.port is not None:
                port = f":{parsed.port}"
        except ValueError:
            host = parsed.netloc.lower()

        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme.lower(), f"{userinfo}{host}{port}", path, parsed.query, ""))

    @classmethod
    def parse_payload(cls, content: str, raw_payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        indicators: list[dict[str, Any]] = []
        if not content:
            return indicators

        for url in cls.URL_PATTERN.findall(content):
            canonical_url = cls.canonicalize_url(url)
            if canonical_url:
                indicators.append({"type": "canonical_url", "value": canonical_url})

        for domain in cls.DOMAIN_PATTERN.findall(content):
            indicators.append({"type": "network_domain", "value": domain.strip().lower()})

        for wallet in cls.CRYPTO_PATTERN.findall(content):
            indicators.append({"type": "wallet_address", "value": wallet.strip()})

        for email in cls.EMAIL_PATTERN.findall(content):
            indicators.append({"type": "email_address", "value": email.strip().lower()})

        for username in cls.USERNAME_PATTERN.findall(content):
            indicators.append({"type": "telegram_username", "value": f"@{username}"})

        for phone in cls.PHONE_PATTERN.findall(content):
            indicators.append({"type": "phone_number", "value": phone.strip()})

        for ip in cls.IP_PATTERN.findall(content):
            indicators.append({"type": "ip_address", "value": ip.strip()})
        if raw_payload and isinstance(raw_payload, dict):
            entities = raw_payload.get("entities", []) or []
            for ent in entities:
                if ent.get("type") == "text_link" and ent.get("url"):
                    canonical_url = cls.canonicalize_url(ent.get("url"))
                    if canonical_url:
                        indicators.append({"type": "canonical_url", "value": canonical_url})

        seen = set()
        deduped = []
        for ind in indicators:
            key = (ind["type"], ind["value"])
            if key not in seen and len(ind["value"]) > 3:
                seen.add(key)
                deduped.append(ind)
        return deduped
