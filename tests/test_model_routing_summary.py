"""Tests for _build_model_routing_deep_summary and GET /api/v1/model-routing/summary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient as _TC
    from zen_claw.dashboard.server import api_app, generate_api_key, store_api_key
    import zen_claw.dashboard.server as _srv_mod

    _FASTAPI_AVAILABLE = api_app is not None
except Exception:
    _FASTAPI_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_MS = 1_700_000_000_000  # 2023-11-14 ~22:13 UTC, a reference epoch


def _make_rows():
    """3 events for model_counts / reason / intent / channel / pivot tests."""
    return [
        {
            "at_ms": _BASE_MS + 3_600_000 * 2,  # bucket 3
            "selected_model": "deepseek-chat",
            "reason": "default",
            "intent_name": "chat",
            "channel": "feishu",
        },
        {
            "at_ms": _BASE_MS + 3_600_000,  # bucket 2
            "selected_model": "deepseek-chat",
            "reason": "intent_override",
            "intent_name": "code",
            "channel": "feishu",
        },
        {
            "at_ms": _BASE_MS,  # bucket 1
            "selected_model": "gpt-4o",
            "reason": "default",
            "intent_name": "chat",
            "channel": "webchat",
        },
    ]


def _skip_no_fastapi():
    if not _FASTAPI_AVAILABLE:
        pytest.skip("fastapi not available")


# ---------------------------------------------------------------------------
# Unit tests — _build_model_routing_deep_summary
# ---------------------------------------------------------------------------


class TestDeepSummaryEmpty:
    def test_deep_summary_empty_rows(self) -> None:
        """Empty input → total=0, all lists empty, trend empty."""
        from zen_claw.dashboard.server import _build_model_routing_deep_summary

        result = _build_model_routing_deep_summary([])
        assert result["total"] == 0
        assert result["models"] == []
        assert result["reasons"] == []
        assert result["intents"] == []
        assert result["channels"] == []
        assert result["pivot"] == {}
        assert result["trend"] == []


class TestDeepSummaryCounts:
    def test_deep_summary_model_counts(self) -> None:
        """3 events → models contains correct counts."""
        from zen_claw.dashboard.server import _build_model_routing_deep_summary

        result = _build_model_routing_deep_summary(_make_rows())
        model_map = {m["model"]: m["count"] for m in result["models"]}
        assert model_map["deepseek-chat"] == 2
        assert model_map["gpt-4o"] == 1

    def test_deep_summary_reason_counts(self) -> None:
        """reasons contains correct counts."""
        from zen_claw.dashboard.server import _build_model_routing_deep_summary

        result = _build_model_routing_deep_summary(_make_rows())
        reason_map = {r["reason"]: r["count"] for r in result["reasons"]}
        assert reason_map["default"] == 2
        assert reason_map["intent_override"] == 1

    def test_deep_summary_intent_counts(self) -> None:
        """intents contains correct counts."""
        from zen_claw.dashboard.server import _build_model_routing_deep_summary

        result = _build_model_routing_deep_summary(_make_rows())
        intent_map = {i["intent"]: i["count"] for i in result["intents"]}
        assert intent_map["chat"] == 2
        assert intent_map["code"] == 1

    def test_deep_summary_channel_counts(self) -> None:
        """channels contains correct counts."""
        from zen_claw.dashboard.server import _build_model_routing_deep_summary

        result = _build_model_routing_deep_summary(_make_rows())
        channel_map = {c["channel"]: c["count"] for c in result["channels"]}
        assert channel_map["feishu"] == 2
        assert channel_map["webchat"] == 1


# ---------------------------------------------------------------------------
# Pivot tests
# ---------------------------------------------------------------------------


class TestDeepSummaryPivot:
    def test_deep_summary_pivot_structure(self) -> None:
        """pivot contains reasons / intents / channels sub-keys per model."""
        from zen_claw.dashboard.server import _build_model_routing_deep_summary

        result = _build_model_routing_deep_summary(_make_rows())
        pivot = result["pivot"]
        assert "deepseek-chat" in pivot
        entry = pivot["deepseek-chat"]
        assert "reasons" in entry
        assert "intents" in entry
        assert "channels" in entry

    def test_deep_summary_pivot_correctness(self) -> None:
        """deepseek-chat reasons pivot matches hand-calculated values."""
        from zen_claw.dashboard.server import _build_model_routing_deep_summary

        result = _build_model_routing_deep_summary(_make_rows())
        ds_reasons = {r["reason"]: r["count"] for r in result["pivot"]["deepseek-chat"]["reasons"]}
        # deepseek-chat has "default" once and "intent_override" once
        assert ds_reasons.get("default") == 1
        assert ds_reasons.get("intent_override") == 1


# ---------------------------------------------------------------------------
# Trend tests
# ---------------------------------------------------------------------------


class TestDeepSummaryTrend:
    def test_deep_summary_trend_hour(self) -> None:
        """bucket_unit=hour → bucket_ms=3600000, trend list non-empty."""
        from zen_claw.dashboard.server import _build_model_routing_deep_summary

        result = _build_model_routing_deep_summary(_make_rows(), bucket_unit="hour")
        assert result["bucket_unit"] == "hour"
        assert result["bucket_ms"] == 3_600_000
        assert len(result["trend"]) > 0

    def test_deep_summary_trend_day(self) -> None:
        """bucket_unit=day → bucket_ms=86400000."""
        from zen_claw.dashboard.server import _build_model_routing_deep_summary

        result = _build_model_routing_deep_summary(_make_rows(), bucket_unit="day")
        assert result["bucket_unit"] == "day"
        assert result["bucket_ms"] == 86_400_000

    def test_deep_summary_trend_week(self) -> None:
        """bucket_unit=week → bucket_ms=604800000."""
        from zen_claw.dashboard.server import _build_model_routing_deep_summary

        result = _build_model_routing_deep_summary(_make_rows(), bucket_unit="week")
        assert result["bucket_unit"] == "week"
        assert result["bucket_ms"] == 604_800_000

    def test_deep_summary_trend_models_in_bucket(self) -> None:
        """Each trend bucket contains a models list."""
        from zen_claw.dashboard.server import _build_model_routing_deep_summary

        result = _build_model_routing_deep_summary(_make_rows(), bucket_unit="hour")
        for bucket in result["trend"]:
            assert "bucket_at_ms" in bucket
            assert "count" in bucket
            assert "models" in bucket
            assert isinstance(bucket["models"], list)


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


def _write_routing_log(data_dir: Path, rows: list[dict]) -> None:
    """Write rows to the expected model_routing log path."""
    log_path = data_dir / "dashboard" / "model_routing.log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


class TestSummaryAPI:
    def test_summary_api_returns_200(self, tmp_path: Path, monkeypatch) -> None:
        """GET /api/v1/model-routing/summary → 200, response contains pivot and trend."""
        _skip_no_fastapi()
        import zen_claw.config.loader as loader_mod

        _write_routing_log(tmp_path, _make_rows())
        monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/model-routing/summary", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        data = resp.json()
        assert "pivot" in data
        assert "trend" in data
        assert data["total"] == 3

    def test_summary_api_model_filter(self, tmp_path: Path, monkeypatch) -> None:
        """?model=deepseek-chat filters to only deepseek-chat events → total < full."""
        _skip_no_fastapi()
        import zen_claw.config.loader as loader_mod

        _write_routing_log(tmp_path, _make_rows())
        monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get(
            "/api/v1/model-routing/summary?model=deepseek-chat",
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Only 2 out of 3 rows match deepseek-chat
        assert data["total"] == 2

    def test_summary_api_bucket_unit_day(self, tmp_path: Path, monkeypatch) -> None:
        """?bucket_unit=day → response bucket_ms=86400000."""
        _skip_no_fastapi()
        import zen_claw.config.loader as loader_mod

        _write_routing_log(tmp_path, _make_rows())
        monkeypatch.setattr(loader_mod, "get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(_srv_mod, "_api_keys_file", lambda: tmp_path / "api_keys.json")
        raw, _ = generate_api_key()
        store_api_key(raw)

        client = _TC(api_app, raise_server_exceptions=False)
        resp = client.get(
            "/api/v1/model-routing/summary?bucket_unit=day",
            headers={"X-API-Key": raw},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["bucket_ms"] == 86_400_000
        assert data["bucket_unit"] == "day"
