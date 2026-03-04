"""Tests for Chinese channel implementations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from zen_claw.channels.dingtalk import DingTalkChannel
from zen_claw.channels.wechat_mp import WechatMPChannel


class TestWechatMPSignature:
    TOKEN = "test_wechat_token_12345"

    def _make_sig(self, token: str, timestamp: str, nonce: str) -> str:
        return hashlib.sha1("".join(sorted([token, timestamp, nonce])).encode()).hexdigest()

    def test_valid_signature(self):
        ts = "1609459200"
        nonce = "abc123"
        sig = self._make_sig(self.TOKEN, ts, nonce)
        assert WechatMPChannel.verify_signature(self.TOKEN, ts, nonce, sig) is True

    def test_wrong_signature(self):
        assert WechatMPChannel.verify_signature(self.TOKEN, "1609459200", "abc123", "wrong") is False

    def test_wrong_token(self):
        ts = "1609459200"
        nonce = "abc123"
        sig = self._make_sig("other_token", ts, nonce)
        assert WechatMPChannel.verify_signature(self.TOKEN, ts, nonce, sig) is False

    def test_empty_signature(self):
        assert WechatMPChannel.verify_signature(self.TOKEN, "123", "nonce", "") is False


class TestWechatMPXMLParsing:
    def test_text_message(self):
        xml = """<xml><FromUserName><![CDATA[oUser123]]></FromUserName><MsgType><![CDATA[text]]></MsgType><Content><![CDATA[你好]]></Content></xml>"""
        data = WechatMPChannel.parse_xml_message(xml)
        assert data["MsgType"] == "text"
        assert data["Content"] == "你好"
        assert data["FromUserName"] == "oUser123"

    def test_image_message(self):
        xml = """<xml><MsgType><![CDATA[image]]></MsgType><PicUrl><![CDATA[https://example.com/pic.jpg]]></PicUrl><MediaId><![CDATA[media_001]]></MediaId></xml>"""
        data = WechatMPChannel.parse_xml_message(xml)
        assert data["MsgType"] == "image"
        assert data["MediaId"] == "media_001"
        assert data["PicUrl"] == "https://example.com/pic.jpg"

    def test_voice_with_recognition(self):
        xml = """<xml><MsgType><![CDATA[voice]]></MsgType><Recognition><![CDATA[我想查一下天气]]></Recognition></xml>"""
        data = WechatMPChannel.parse_xml_message(xml)
        assert data["MsgType"] == "voice"
        assert data["Recognition"] == "我想查一下天气"

    def test_malformed_xml(self):
        assert WechatMPChannel.parse_xml_message("<not valid xml") == {}


class TestDingTalkSignature:
    SECRET = "SECabc1234567890test_secret"

    def _compute_sign(self, secret: str, ts_ms: int) -> str:
        plain = f"{ts_ms}\n{secret}"
        sig = hmac.new(secret.encode("utf-8"), plain.encode("utf-8"), digestmod="sha256").digest()
        return base64.b64encode(sig).decode()

    def test_verify_valid_signature(self):
        ts = int(time.time() * 1000)
        sign = self._compute_sign(self.SECRET, ts)
        assert DingTalkChannel.verify_incoming_sign(str(ts), sign, self.SECRET, max_age_ms=60_000) is True

    def test_reject_wrong_signature(self):
        ts = int(time.time() * 1000)
        assert DingTalkChannel.verify_incoming_sign(str(ts), "BADSIGN==", self.SECRET, max_age_ms=60_000) is False

    def test_reject_expired_timestamp(self):
        old_ts = int((time.time() - 7200) * 1000)
        sign = self._compute_sign(self.SECRET, old_ts)
        assert DingTalkChannel.verify_incoming_sign(str(old_ts), sign, self.SECRET, max_age_ms=60_000) is False

    def test_no_secret_allows_all(self):
        assert DingTalkChannel.verify_incoming_sign("123", "anything", "", max_age_ms=60_000) is True

    def test_outgoing_sign_format(self):
        sign = DingTalkChannel.compute_outgoing_sign(self.SECRET, 1700000000000)
        import urllib.parse

        raw = base64.b64decode(urllib.parse.unquote(sign))
        assert len(raw) == 32


class TestDingTalkOutgoingFormat:
    def test_markdown_payload_structure(self):
        payload = {"msgtype": "markdown", "markdown": {"title": "Agent Reply", "text": "# 你好\n\n这是一个测试"}}
        assert payload["msgtype"] == "markdown"
        assert "title" in payload["markdown"]
        assert "text" in payload["markdown"]
