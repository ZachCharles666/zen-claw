"""Zero-LLM direct response contracts — C1.

Nine new intent contracts that resolve purely in Python, with no LLM call:

  calculator    — arithmetic: "123 * 456", "15% of 300", "sqrt(256)"
  unit_convert  — "100 km to miles", "50°C to°F", "1 GB in MB"
  text_stats    — "word count of …", "how many chars", "reading time"
  encode        — "base64 encode hello", "url decode %E4%BD%A0%E5%A5%BD"
  hash_digest   — "md5 of hello", "sha256 world"
  json_format   — "format this json {…}", "validate json […]"
  color_convert — "#FF5733 to rgb", "rgb(255,87,51) to hex"
  ip_lookup     — "lookup 8.8.8.8", "查询ip 1.1.1.1"
  timezone_list — "timezones in China", "list US time zones"

Each contract exposes:
  _extract_<name>(content) -> <typed request> | None
  _handle_<name>(<request>)  -> str (the direct reply)

The top-level ``route(content)`` function tries every contract in order and
returns a :class:`DirectResult` on the first match, or ``None``.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import math
import operator
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DirectResult:
    """Resolved intent name and pre-computed reply content."""
    intent_name: str
    content: str


# ═══════════════════════════════════════════════════════════════════════════════
# 1. CALCULATOR
# ═══════════════════════════════════════════════════════════════════════════════

_CALC_TRIGGER = re.compile(
    r"\b(计算|calculate|calc|compute|evaluate|eval|what\s+is|=\?)\b", re.I
)
_PERCENT_OF = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*of\s*(\d+(?:\.\d+)?)", re.I
)
_SQRT_CALL = re.compile(
    r"\bsqrt\s*\(?\s*(\d+(?:\.\d+)?)\s*\)?", re.I
)
# A "bare" math expression: only digits, operators, parens, dots, spaces
_BARE_EXPR = re.compile(r"^\s*[\d\s\+\-\*\/\(\)\.\%\*]+\s*$")

_AST_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _eval_node(node: ast.expr) -> float:  # type: ignore[type-arg]
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp):
        op = _AST_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unsupported op: {node.op}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and (abs(left) > 1e6 or abs(right) > 20):
            raise ValueError("result too large")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _AST_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unsupported unary op: {node.op}")
        return op(_eval_node(node.operand))
    raise ValueError(f"unsupported node: {type(node).__name__}")


def _safe_calc(expr: str) -> float:
    expr = expr.strip().replace("^", "**")
    tree = ast.parse(expr, mode="eval")
    return _eval_node(tree.body)


def _extract_calculator(content: str) -> str | None:
    """Return the expression string to evaluate, or None."""
    # percent-of shorthand: "15% of 300"
    m = _PERCENT_OF.search(content)
    if m:
        return f"{m.group(1)} / 100 * {m.group(2)}"
    # sqrt shorthand
    m = _SQRT_CALL.search(content)
    if m:
        return f"sqrt({m.group(1)})"

    # triggered expression: "calculate 1+2" or "what is 3*4"
    m = _CALC_TRIGGER.search(content)
    if m:
        after = content[m.end():].strip()
        # strip trailing punctuation
        after = re.sub(r"[？?。\.]+$", "", after).strip()
        if after and re.search(r"[\+\-\*\/\%\^]", after):
            return after

    # bare math expression (whole message)
    stripped = content.strip()
    if _BARE_EXPR.match(stripped) and re.search(r"[\+\-\*\/\%]", stripped):
        return stripped

    return None


def _handle_calculator(expr: str) -> str:
    try:
        if expr.startswith("sqrt("):
            n = float(re.search(r"sqrt\((.+)\)", expr).group(1))  # type: ignore
            result: float = math.sqrt(n)
        else:
            result = _safe_calc(expr)
        # Format cleanly: int if whole number
        if result == int(result) and abs(result) < 1e15:
            formatted = str(int(result))
        else:
            formatted = f"{result:.10g}"
        return f"{expr.replace('**', '^')} = {formatted}"
    except Exception as exc:
        return f"Could not evaluate: {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. UNIT CONVERSION
# ═══════════════════════════════════════════════════════════════════════════════

# (from_unit_normalized, to_unit_normalized) → factor  (result = value * factor)
# Temperature is handled separately.
_UNIT_FACTORS: dict[tuple[str, str], float] = {
    # length
    ("km", "miles"): 0.621371, ("miles", "km"): 1.60934,
    ("m", "ft"): 3.28084, ("ft", "m"): 0.3048,
    ("m", "feet"): 3.28084, ("feet", "m"): 0.3048,
    ("cm", "in"): 0.393701, ("in", "cm"): 2.54,
    ("cm", "inches"): 0.393701, ("inches", "cm"): 2.54,
    ("mm", "in"): 0.0393701, ("in", "mm"): 25.4,
    # weight
    ("kg", "lbs"): 2.20462, ("lbs", "kg"): 0.453592,
    ("kg", "lb"): 2.20462, ("lb", "kg"): 0.453592,
    ("g", "oz"): 0.035274, ("oz", "g"): 28.3495,
    ("t", "kg"): 1000.0, ("kg", "t"): 0.001,
    # data storage
    ("b", "kb"): 1 / 1024, ("kb", "b"): 1024.0,
    ("kb", "mb"): 1 / 1024, ("mb", "kb"): 1024.0,
    ("mb", "gb"): 1 / 1024, ("gb", "mb"): 1024.0,
    ("gb", "tb"): 1 / 1024, ("tb", "gb"): 1024.0,
    ("tb", "pb"): 1 / 1024, ("pb", "tb"): 1024.0,
    # speed
    ("kmh", "mph"): 0.621371, ("mph", "kmh"): 1.60934,
    ("km/h", "mph"): 0.621371, ("mph", "km/h"): 1.60934,
    # area
    ("m2", "ft2"): 10.7639, ("ft2", "m2"): 0.0929030,
    ("km2", "miles2"): 0.386102, ("miles2", "km2"): 2.58999,
    ("ha", "acres"): 2.47105, ("acres", "ha"): 0.404686,
}

_TEMP_UNITS = {"c", "°c", "celsius", "f", "°f", "fahrenheit", "k", "kelvin"}

# NOTE: longer / more-specific alternatives MUST appear before shorter ones
# (e.g. "mb" before "m") because Python regex alternation is left-to-right.
_UNIT_PART = (
    r"[tgmkp]b(?:ytes?)?|"           # data: gb, mb, kb, tb, pb (before m/g/t/k)
    r"km/h|km|miles?|mi|mm|cm|mm|m|" # length (km before m, mm before m)
    r"ft|feet|inches?|in|"
    r"kg|lbs?|lb|oz|g|t|"            # weight (kg before g; lb/lbs before b)
    r"mph|"
    r"ha|acres?|km2|m2|ft2|miles?2|"
    r"°?[cfk]|celsius|fahrenheit|kelvin"
)
_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(" + _UNIT_PART + r")"
    r"\s+(?:to|in|转|转换为?|换算成?|→|->)\s*"
    r"(" + _UNIT_PART + r")",
    re.I,
)


def _norm_unit(u: str) -> str:
    u = u.lower().lstrip("°").strip()
    aliases = {"mi": "miles", "mile": "miles", "lb": "lbs", "feet": "ft",
               "foot": "ft", "inch": "in", "inches": "in", "byte": "b",
               "bytes": "b", "celsius": "c", "fahrenheit": "f", "kelvin": "k"}
    return aliases.get(u, u)


def _convert_temperature(value: float, from_u: str, to_u: str) -> float:
    # Normalise to Celsius first
    if from_u == "f":
        c = (value - 32) * 5 / 9
    elif from_u == "k":
        c = value - 273.15
    else:
        c = value
    if to_u == "f":
        return c * 9 / 5 + 32
    if to_u == "k":
        return c + 273.15
    return c


def _extract_unit_convert(content: str) -> tuple[float, str, str] | None:
    m = _UNIT_RE.search(content)
    if not m:
        return None
    value = float(m.group(1))
    from_u = _norm_unit(m.group(2))
    to_u = _norm_unit(m.group(3))
    if from_u == to_u:
        return None
    return value, from_u, to_u


def _handle_unit_convert(value: float, from_u: str, to_u: str) -> str:
    # temperature
    if from_u in _TEMP_UNITS or to_u in _TEMP_UNITS:
        try:
            result = _convert_temperature(value, from_u, to_u)
            label_map = {"c": "°C", "f": "°F", "k": "K"}
            return (f"{value} {label_map.get(from_u, from_u)} = "
                    f"{result:.4g} {label_map.get(to_u, to_u)}")
        except Exception:
            return "Conversion error"
    # factor-based
    factor = _UNIT_FACTORS.get((from_u, to_u))
    if factor is None:
        return f"No conversion table entry for {from_u} → {to_u}"
    result = value * factor
    if result == int(result) and abs(result) < 1e12:
        fmt = str(int(result))
    else:
        fmt = f"{result:.6g}"
    return f"{value:g} {from_u} = {fmt} {to_u}"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. TEXT STATS
# ═══════════════════════════════════════════════════════════════════════════════

_TEXT_STATS_RE = re.compile(
    r"\b(word\s+count|count\s+words?|how\s+many\s+words?|"
    r"char(?:acter)?\s+count|count\s+chars?|how\s+many\s+char(?:acters?)?|"
    r"reading\s+time|字数|字符数|词数|阅读时间)\b",
    re.I,
)
_TEXT_AFTER_COLON = re.compile(r"[：:]\s*(.+)$", re.DOTALL)
_TEXT_OF_IN = re.compile(
    r"\b(?:of|in|for|的?[：:]?)\s+(.+)$", re.I | re.DOTALL
)


def _extract_text_stats(content: str) -> str | None:
    m = _TEXT_STATS_RE.search(content)
    if not m:
        return None
    after = content[m.end():].strip()
    # Try "… of <text>" or ": <text>"
    colon = _TEXT_AFTER_COLON.search(after or content)
    if colon:
        return colon.group(1).strip()
    of_in = _TEXT_OF_IN.search(after or content[m.start():])
    if of_in:
        return of_in.group(1).strip()
    # Fallback: everything after the keyword
    if after:
        return after
    return None


def _handle_text_stats(text: str) -> str:
    char_count = len(text)
    # Word count: split on whitespace, also handle CJK (each CJK char ≈ 1 word)
    cjk_chars = re.findall(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]", text)
    latin_words = len(re.findall(r"\b[a-zA-Z0-9_'-]+\b", text))
    word_count = latin_words + len(cjk_chars)
    # Reading time: ~200 wpm for Latin, ~300 cpm for CJK
    reading_sec = max(1, round((latin_words / 200 + len(cjk_chars) / 300) * 60))
    if reading_sec < 60:
        rt = f"~{reading_sec}s"
    else:
        rt = f"~{reading_sec // 60}m {reading_sec % 60}s"
    return (
        f"Characters : {char_count}\n"
        f"Words      : {word_count}\n"
        f"Reading time: {rt}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. ENCODE / DECODE
# ═══════════════════════════════════════════════════════════════════════════════

_ENCODE_RE = re.compile(
    r"\b(base64|b64|url|percent)[\s\-_]*(encode|decode|编码|解码)\s+(.+)"
    r"|"
    r"\b(encode|decode|编码|解码)\s+(base64|b64|url|percent)[\s:：]+(.+)",
    re.I | re.DOTALL,
)


def _extract_encode(content: str) -> tuple[str, str, str] | None:
    """Return (algo, action, payload) or None."""
    m = _ENCODE_RE.search(content)
    if not m:
        return None
    if m.group(1):  # algo action payload
        algo = m.group(1).lower().strip()
        action = m.group(2).lower().strip()
        payload = m.group(3).strip()
    else:  # action algo payload
        action = m.group(4).lower().strip()
        algo = m.group(5).lower().strip()
        payload = m.group(6).strip()
    action = "encode" if action in {"encode", "编码"} else "decode"
    algo = "url" if algo in {"url", "percent"} else "base64"
    return algo, action, payload


def _handle_encode(algo: str, action: str, payload: str) -> str:
    try:
        if algo == "base64":
            if action == "encode":
                result = base64.b64encode(payload.encode()).decode()
                return f"Base64 encoded: {result}"
            else:
                decoded = base64.b64decode(payload.encode()).decode(errors="replace")
                return f"Base64 decoded: {decoded}"
        else:  # url
            if action == "encode":
                result = urllib.parse.quote(payload, safe="")
                return f"URL encoded: {result}"
            else:
                result = urllib.parse.unquote(payload)
                return f"URL decoded: {result}"
    except Exception as exc:
        return f"Encoding error: {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. HASH DIGEST
# ═══════════════════════════════════════════════════════════════════════════════

_HASH_RE = re.compile(
    r"\b(md5|sha[\-_]?1|sha[\-_]?256|sha[\-_]?512|sha[\-_]?224|sha[\-_]?384)"
    r"(?:\s+(?:of|hash|:|\s))?\s+(.+)",
    re.I | re.DOTALL,
)
_HASH_ALGO_MAP = {
    "md5": "md5", "sha1": "sha1", "sha-1": "sha1",
    "sha256": "sha256", "sha-256": "sha256", "sha_256": "sha256",
    "sha512": "sha512", "sha-512": "sha512",
    "sha224": "sha224", "sha-224": "sha224",
    "sha384": "sha384", "sha-384": "sha384",
}


def _extract_hash(content: str) -> tuple[str, str] | None:
    """Return (algo, payload) or None."""
    m = _HASH_RE.search(content)
    if not m:
        return None
    raw_algo = m.group(1).lower().replace(" ", "").replace("-", "").replace("_", "")
    # normalise
    algo = _HASH_ALGO_MAP.get(raw_algo) or _HASH_ALGO_MAP.get(m.group(1).lower())
    if not algo:
        return None
    payload = m.group(2).strip()
    if not payload:
        return None
    return algo, payload


def _handle_hash(algo: str, payload: str) -> str:
    try:
        h = hashlib.new(algo, payload.encode()).hexdigest()
        return f"{algo.upper()}: {h}"
    except Exception as exc:
        return f"Hash error: {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. JSON FORMAT / VALIDATE
# ═══════════════════════════════════════════════════════════════════════════════

_JSON_TRIGGER = re.compile(
    r"\b(format|pretty[\s\-]?print|validate|beautify|minify|格式化|验证|美化)\b",
    re.I,
)
_JSON_BODY = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


def _extract_json_format(content: str) -> tuple[str, str] | None:
    """Return (action, json_string) or None."""
    trig = _JSON_TRIGGER.search(content)
    body = _JSON_BODY.search(content)
    if not body:
        return None
    action = trig.group(1).lower() if trig else "format"
    return action, body.group(1)


def _handle_json_format(action: str, json_str: str) -> str:
    try:
        parsed = json.loads(json_str)
        if action in {"minify"}:
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    except json.JSONDecodeError as exc:
        return f"Invalid JSON: {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. COLOR CONVERSION
# ═══════════════════════════════════════════════════════════════════════════════

_HEX_COLOR = re.compile(r"#([0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?)")
_RGB_COLOR = re.compile(r"rgb\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)")
_COLOR_TO = re.compile(r"\bto\s+(rgb|hex|rgba|hsl)\b", re.I)


def _extract_color(content: str) -> tuple[str, tuple, str] | None:
    """Return (source_format, color_tuple, target_format) or None."""
    target_m = _COLOR_TO.search(content)
    target = (target_m.group(1).lower() if target_m else None)

    hex_m = _HEX_COLOR.search(content)
    if hex_m:
        h = hex_m.group(1)
        if len(h) == 3:
            h = h[0]*2 + h[1]*2 + h[2]*2
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return "hex", (r, g, b), target or "rgb"

    rgb_m = _RGB_COLOR.search(content)
    if rgb_m:
        r, g, b = int(rgb_m.group(1)), int(rgb_m.group(2)), int(rgb_m.group(3))
        return "rgb", (r, g, b), target or "hex"

    return None


def _rgb_to_hsl(r: int, g: int, b: int) -> tuple[float, float, float]:
    rf, gf, bf = r / 255, g / 255, b / 255
    cmax, cmin = max(rf, gf, bf), min(rf, gf, bf)
    delta = cmax - cmin
    L = (cmax + cmin) / 2
    S = 0.0 if delta == 0 else delta / (1 - abs(2 * L - 1))
    if delta == 0:
        H = 0.0
    elif cmax == rf:
        H = 60 * (((gf - bf) / delta) % 6)
    elif cmax == gf:
        H = 60 * ((bf - rf) / delta + 2)
    else:
        H = 60 * ((rf - gf) / delta + 4)
    return round(H, 1), round(S * 100, 1), round(L * 100, 1)


def _handle_color(source_fmt: str, color: tuple, target: str) -> str:
    r, g, b = color
    if target == "rgb":
        return f"RGB: rgb({r}, {g}, {b})"
    if target == "rgba":
        return f"RGBA: rgba({r}, {g}, {b}, 1.0)"
    if target == "hex":
        return f"HEX: #{r:02X}{g:02X}{b:02X}"
    if target == "hsl":
        h, s, l = _rgb_to_hsl(r, g, b)
        return f"HSL: hsl({h}, {s}%, {l}%)"
    return f"RGB: ({r}, {g}, {b})"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. IP LOOKUP
# ═══════════════════════════════════════════════════════════════════════════════

_IPV4_RE = re.compile(
    r"\b((?:\d{1,3}\.){3}\d{1,3})\b"
)
_IP_TRIGGER = re.compile(
    r"\b(lookup|query|check|whois|location|查询|ip地址|ip查询|what\s+is)\b",
    re.I,
)

# Allow tests / offline environments to inject a custom fetch function
_ip_fetch: Any = None  # callable(url: str) -> dict | None


def _fetch_ip_info(ip: str) -> dict | None:
    """Call ip-api.com JSON API.  Returns None on any error."""
    fetch_fn = _ip_fetch  # allow override in tests
    try:
        if fetch_fn is not None:
            return fetch_fn(ip)
        url = f"http://ip-api.com/json/{urllib.parse.quote(ip)}?fields=status,country,regionName,city,isp,query"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _extract_ip_lookup(content: str) -> str | None:
    """Return the IP address string if this looks like an IP lookup request."""
    trig = _IP_TRIGGER.search(content)
    ip_m = _IPV4_RE.search(content)
    if ip_m and trig:
        return ip_m.group(1)
    return None


def _handle_ip_lookup(ip: str) -> str:
    data = _fetch_ip_info(ip)
    if not data or str(data.get("status", "")).lower() != "success":
        return f"IP: {ip}\n(Could not retrieve location info)"
    return (
        f"IP      : {data.get('query', ip)}\n"
        f"Country : {data.get('country', '?')}\n"
        f"Region  : {data.get('regionName', '?')}\n"
        f"City    : {data.get('city', '?')}\n"
        f"ISP     : {data.get('isp', '?')}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 9. TIMEZONE LIST
# ═══════════════════════════════════════════════════════════════════════════════

_TZ_TRIGGER = re.compile(
    r"\b(timezones?|time\s+zones?|时区)\b", re.I
)
_TZ_COUNTRY = re.compile(
    r"\b(?:in|for|of)\s+([a-zA-Z]{2,20})"  # English: "in China"
    r"|([a-zA-Z\u4e00-\u9fff]{2,20})(?:的?时区|有哪些时区)",  # Chinese: "中国时区"
    re.I,
)

_COUNTRY_TZ: dict[str, list[str]] = {
    "china":     ["Asia/Shanghai", "Asia/Urumqi"],
    "中国":       ["Asia/Shanghai", "Asia/Urumqi"],
    "us":        ["America/New_York", "America/Chicago", "America/Denver",
                  "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu"],
    "usa":       ["America/New_York", "America/Chicago", "America/Denver",
                  "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu"],
    "unitedstates": ["America/New_York", "America/Chicago", "America/Denver",
                     "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu"],
    "uk":        ["Europe/London"],
    "gb":        ["Europe/London"],
    "britain":   ["Europe/London"],
    "germany":   ["Europe/Berlin"],
    "france":    ["Europe/Paris"],
    "japan":     ["Asia/Tokyo"],
    "日本":       ["Asia/Tokyo"],
    "india":     ["Asia/Kolkata"],
    "australia": ["Australia/Sydney", "Australia/Melbourne", "Australia/Perth",
                  "Australia/Darwin", "Australia/Brisbane"],
    "canada":    ["America/Toronto", "America/Vancouver", "America/Winnipeg",
                  "America/Halifax"],
    "brazil":    ["America/Sao_Paulo", "America/Manaus"],
    "russia":    ["Europe/Moscow", "Asia/Yekaterinburg", "Asia/Novosibirsk",
                  "Asia/Vladivostok"],
    "singapore": ["Asia/Singapore"],
    "新加坡":     ["Asia/Singapore"],
    "korea":     ["Asia/Seoul"],
    "韩国":       ["Asia/Seoul"],
}


def _extract_timezone_list(content: str) -> str | None:
    """Return the country/region name, or None."""
    if not _TZ_TRIGGER.search(content):
        return None
    m = _TZ_COUNTRY.search(content)
    if m:
        country = m.group(1) or m.group(2)  # English or Chinese group
        if country:
            return country.strip()
    return None


def _handle_timezone_list(country: str) -> str:
    key = country.lower().replace(" ", "").replace("-", "")
    tzs = _COUNTRY_TZ.get(key) or _COUNTRY_TZ.get(country.lower())
    if not tzs:
        # Try partial match
        for ckey, ctzs in _COUNTRY_TZ.items():
            if key in ckey or ckey in key:
                tzs = ctzs
                break
    if not tzs:
        return f"No time zone data for '{country}'. Known regions: {', '.join(sorted(_COUNTRY_TZ.keys()))}"
    lines = "\n".join(f"  • {tz}" for tz in tzs)
    return f"Time zones in {country}:\n{lines}"


# ═══════════════════════════════════════════════════════════════════════════════
# Public dispatch
# ═══════════════════════════════════════════════════════════════════════════════

def route(content: str) -> DirectResult | None:
    """Try all nine direct contracts in priority order.

    Returns a :class:`DirectResult` on the first match, or ``None`` if the
    message does not match any contract.
    """
    # 1. Calculator (before text stats to avoid "how many chars" vs "2+2")
    calc_expr = _extract_calculator(content)
    if calc_expr is not None:
        return DirectResult("calculator", _handle_calculator(calc_expr))

    # 2. Unit conversion
    uc = _extract_unit_convert(content)
    if uc is not None:
        return DirectResult("unit_convert", _handle_unit_convert(*uc))

    # 3. Encode / decode
    enc = _extract_encode(content)
    if enc is not None:
        return DirectResult("encode", _handle_encode(*enc))

    # 4. Hash
    hsh = _extract_hash(content)
    if hsh is not None:
        return DirectResult("hash_digest", _handle_hash(*hsh))

    # 5. Color conversion
    col = _extract_color(content)
    if col is not None:
        return DirectResult("color_convert", _handle_color(*col))

    # 6. JSON format / validate
    jsf = _extract_json_format(content)
    if jsf is not None:
        return DirectResult("json_format", _handle_json_format(*jsf))

    # 7. IP lookup (needs network — checked after offline contracts)
    ip = _extract_ip_lookup(content)
    if ip is not None:
        return DirectResult("ip_lookup", _handle_ip_lookup(ip))

    # 8. Text stats
    txt = _extract_text_stats(content)
    if txt is not None:
        return DirectResult("text_stats", _handle_text_stats(txt))

    # 9. Timezone list
    tz = _extract_timezone_list(content)
    if tz is not None:
        return DirectResult("timezone_list", _handle_timezone_list(tz))

    return None
