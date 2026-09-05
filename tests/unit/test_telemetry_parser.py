from app.services.telemetry_parser import TelemetryEntityParser


def test_parse_payload_extracts_and_deduplicates_structured_indicators():
    indicators = TelemetryEntityParser.parse_payload(
        (
            "Visit https://gateway.remote.net/panel and api.remote.net. "
            "Wallet 0x1111111111111111111111111111111111111111 "
            "again https://gateway.remote.net/panel"
        ),
        {
            "entities": [
                {"type": "text_link", "url": "https://docs.remote.io/start"},
                {"type": "url"},
            ]
        },
    )

    assert indicators.count(
        {"type": "canonical_url", "value": "https://gateway.remote.net/panel"}
    ) == 1
    assert {"type": "canonical_url", "value": "https://docs.remote.io/start"} in indicators
    assert {"type": "network_domain", "value": "api.remote.net"} in indicators
    assert {
        "type": "wallet_address",
        "value": "0x1111111111111111111111111111111111111111",
    } in indicators


def test_parse_payload_handles_empty_content():
    assert TelemetryEntityParser.parse_payload("") == []


def test_parse_payload_canonicalizes_url_variants_before_deduplication():
    indicators = TelemetryEntityParser.parse_payload(
        "See https://Example.com/Path/#frag and https://example.com/Path",
        {
            "entities": [
                {"type": "text_link", "url": "HTTPS://EXAMPLE.com/Path/#other"},
            ]
        },
    )

    assert indicators.count(
        {"type": "canonical_url", "value": "https://example.com/Path"}
    ) == 1


def test_parse_payload_extracts_contact_and_network_indicators_once():
    indicators = TelemetryEntityParser.parse_payload(

            "Contact Admin@Example.COM or @support_bot at +65 9123 4567. "
            "Internal host 192.168.1.10; repeat admin@example.com and @support_bot."

    )

    assert indicators.count(
        {"type": "email_address", "value": "admin@example.com"}
    ) == 1
    assert indicators.count(
        {"type": "telegram_username", "value": "@support_bot"}
    ) == 1
    assert {"type": "telegram_username", "value": "@Example"} not in indicators
    assert {"type": "phone_number", "value": "+65 9123 4567"} in indicators
    assert {"type": "ip_address", "value": "192.168.1.10"} in indicators


def test_parse_payload_rejects_malformed_contact_and_ip_values():
    indicators = TelemetryEntityParser.parse_payload(
        "Ignore a@b, @abc, 999.999.999.999, and +00 123 456."
    )

    assert not any(
        indicator["type"] in {
            "email_address",
            "telegram_username",
            "phone_number",
            "ip_address",
        }
        for indicator in indicators
    )
