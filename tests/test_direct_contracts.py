"""Tests for C1 — nine zero-LLM direct response contracts."""

from __future__ import annotations

import zen_claw.agent.direct_contracts as dc
from zen_claw.agent.direct_contracts import (
    _extract_calculator,
    _extract_color,
    _extract_encode,
    _extract_hash,
    _extract_ip_lookup,
    _extract_json_format,
    _extract_text_stats,
    _extract_timezone_list,
    _extract_unit_convert,
    _handle_ip_lookup,
    route,
)

# ── Calculator ────────────────────────────────────────────────────────────────

def test_calc_extract_triggered():
    assert _extract_calculator("calculate 3 * 7") is not None


def test_calc_integer_result():
    result = route("calculate 3 * 7")
    assert result is not None
    assert result.intent_name == "calculator"
    assert "21" in result.content


def test_calc_percent_of():
    result = route("15% of 200")
    assert result is not None
    assert result.intent_name == "calculator"
    assert "30" in result.content


def test_calc_sqrt():
    result = route("sqrt(256)")
    assert result is not None
    assert "16" in result.content


def test_calc_float_division():
    result = route("calculate 10 / 3")
    assert result is not None
    assert "3.333" in result.content or "3.33" in result.content


def test_calc_no_match_plain_text():
    assert _extract_calculator("hello world how are you") is None


# ── Unit Conversion ───────────────────────────────────────────────────────────

def test_unit_km_to_miles():
    result = route("100 km to miles")
    assert result is not None
    assert result.intent_name == "unit_convert"
    assert "62" in result.content


def test_unit_celsius_to_fahrenheit():
    result = route("100 °C to °F")
    assert result is not None
    assert "212" in result.content


def test_unit_gb_to_mb():
    result = route("2 GB to MB")
    assert result is not None
    assert "2048" in result.content


def test_unit_kg_to_lbs():
    result = route("70 kg to lbs")
    assert result is not None
    assert "154" in result.content


def test_unit_no_match():
    assert _extract_unit_convert("just a regular sentence") is None


# ── Text Stats ────────────────────────────────────────────────────────────────

def test_text_stats_word_count():
    result = route("word count of: hello world foo bar")
    assert result is not None
    assert result.intent_name == "text_stats"
    assert "Words" in result.content


def test_text_stats_char_count():
    result = route("how many characters in: test")
    assert result is not None
    assert "Characters" in result.content
    assert "4" in result.content


def test_text_stats_reading_time():
    result = route("reading time of: " + "word " * 200)
    assert result is not None
    assert "Reading time" in result.content


def test_text_stats_no_match():
    assert _extract_text_stats("what is the weather") is None


# ── Encode / Decode ───────────────────────────────────────────────────────────

def test_encode_base64_encode():
    result = route("base64 encode hello")
    assert result is not None
    assert result.intent_name == "encode"
    assert "aGVsbG8=" in result.content


def test_encode_base64_decode():
    result = route("base64 decode aGVsbG8=")
    assert result is not None
    assert "hello" in result.content


def test_encode_url_encode():
    result = route("url encode hello world")
    assert result is not None
    assert "hello%20world" in result.content or "hello+world" in result.content.lower()


def test_encode_url_decode():
    result = route("url decode hello%20world")
    assert result is not None
    assert "hello world" in result.content


def test_encode_no_match():
    assert _extract_encode("please send this message") is None


# ── Hash Digest ───────────────────────────────────────────────────────────────

def test_hash_md5():
    result = route("md5 of hello")
    assert result is not None
    assert result.intent_name == "hash_digest"
    assert "5d41402abc4b2a76b9719d911017c592" in result.content


def test_hash_sha256():
    result = route("sha256 world")
    assert result is not None
    assert "SHA256" in result.content
    assert len(result.content) > 20  # contains a hash


def test_hash_sha1():
    result = route("sha1 of test")
    assert result is not None
    assert "SHA1" in result.content


def test_hash_no_match():
    assert _extract_hash("please hash") is None  # no algorithm specified


# ── JSON Format ───────────────────────────────────────────────────────────────

def test_json_format_pretty():
    result = route('format this json {"a":1,"b":2}')
    assert result is not None
    assert result.intent_name == "json_format"
    assert '"a"' in result.content
    assert "\n" in result.content  # indented


def test_json_validate_valid():
    result = route('validate json [1, 2, 3]')
    assert result is not None
    assert "1" in result.content


def test_json_validate_invalid():
    result = route('validate json {bad json}')
    assert result is not None
    assert "Invalid" in result.content or "invalid" in result.content.lower()


def test_json_no_match():
    assert _extract_json_format("just some text") is None


# ── Color Conversion ──────────────────────────────────────────────────────────

def test_color_hex_to_rgb():
    result = route("#FF5733 to rgb")
    assert result is not None
    assert result.intent_name == "color_convert"
    assert "255" in result.content
    assert "87" in result.content


def test_color_rgb_to_hex():
    result = route("rgb(255, 87, 51) to hex")
    assert result is not None
    assert "FF" in result.content.upper()


def test_color_3digit_hex():
    result = route("#FFF to rgb")
    assert result is not None
    assert "255" in result.content


def test_color_no_match():
    assert _extract_color("what color is the sky") is None


# ── IP Lookup ─────────────────────────────────────────────────────────────────

def test_ip_lookup_extract_valid_ipv4():
    assert _extract_ip_lookup("lookup 8.8.8.8") == "8.8.8.8"
    assert _extract_ip_lookup("query ip 1.1.1.1") == "1.1.1.1"


def test_ip_lookup_no_trigger_no_match():
    # IP without trigger keyword should not match
    assert _extract_ip_lookup("server address is 192.168.1.1") is None


def test_ip_lookup_with_mock(monkeypatch):
    """Verify handle function formats the API response correctly."""
    mock_data = {
        "status": "success", "query": "8.8.8.8",
        "country": "United States", "regionName": "California",
        "city": "Mountain View", "isp": "Google LLC",
    }
    monkeypatch.setattr(dc, "_ip_fetch", lambda ip: mock_data)
    result = _handle_ip_lookup("8.8.8.8")
    assert "Google LLC" in result
    assert "United States" in result


# ── Timezone List ─────────────────────────────────────────────────────────────

def test_timezone_china():
    result = route("timezones in China")
    assert result is not None
    assert result.intent_name == "timezone_list"
    assert "Asia/Shanghai" in result.content


def test_timezone_us():
    result = route("list time zones in US")
    assert result is not None
    assert "America/New_York" in result.content


def test_timezone_unknown_country():
    result = route("timezones in Narnia")
    assert result is not None
    assert "No time zone data" in result.content


def test_timezone_no_match():
    assert _extract_timezone_list("what time is it") is None


# ── No-match passthrough ──────────────────────────────────────────────────────

def test_route_returns_none_for_plain_message():
    assert route("hello, how are you today?") is None
