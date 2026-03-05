"""Webhook routes for WeChat MP, WeCom, DingTalk and generic triggers."""

from __future__ import annotations

from typing import Any

try:
    from fastapi import APIRouter, Query, Request
    from fastapi.responses import JSONResponse, PlainTextResponse

    _HAS_FASTAPI = True
except Exception:
    _HAS_FASTAPI = False
    APIRouter = object  # type: ignore[assignment]
    Request = object  # type: ignore[assignment]
    PlainTextResponse = None  # type: ignore[assignment]

    def _query_stub(x=""):
        return x

    Query = _query_stub  # type: ignore[assignment]

_wechat_channel = None
_wecom_channel = None
_dingtalk_channel = None
_webhook_trigger_channel = None
_slack_channel = None


def register_channels(wechat=None, wecom=None, dingtalk=None, webhook_trigger=None, slack=None):
    global \
        _wechat_channel, \
        _wecom_channel, \
        _dingtalk_channel, \
        _webhook_trigger_channel, \
        _slack_channel
    _wechat_channel = wechat
    _wecom_channel = wecom
    _dingtalk_channel = dingtalk
    _webhook_trigger_channel = webhook_trigger
    _slack_channel = slack


if _HAS_FASTAPI:
    webhook_router = APIRouter(prefix="/webhook")

    @webhook_router.get("/wechat")
    async def wechat_verify(
        signature: str = Query(""),
        timestamp: str = Query(""),
        nonce: str = Query(""),
        echostr: str = Query(""),
    ):
        if _wechat_channel and _wechat_channel.verify_signature(
            _wechat_channel.config.token, timestamp, nonce, signature
        ):
            return PlainTextResponse(echostr)
        return PlainTextResponse("fail", status_code=403)

    @webhook_router.post("/wechat")
    async def wechat_webhook(
        request: Request,
        signature: str = Query(""),
        timestamp: str = Query(""),
        nonce: str = Query(""),
        msg_signature: str = Query(""),
    ):
        if not _wechat_channel:
            return PlainTextResponse("success")
        raw_xml = (await request.body()).decode("utf-8")
        result = await _wechat_channel.handle_webhook(
            raw_xml=raw_xml,
            signature=signature,
            timestamp=timestamp,
            nonce=nonce,
            msg_signature=msg_signature or None,
            encrypted=bool(msg_signature),
        )
        return PlainTextResponse(result)

    @webhook_router.get("/wecom")
    async def wecom_verify(
        msg_signature: str = Query(""),
        timestamp: str = Query(""),
        nonce: str = Query(""),
        echostr: str = Query(""),
    ):
        if _wecom_channel and _wecom_channel.verify_signature(
            timestamp, nonce, msg_signature, echostr
        ):
            plain = _wecom_channel.decrypt_message(echostr)
            return PlainTextResponse(plain or "fail")
        return PlainTextResponse("fail", status_code=403)

    @webhook_router.post("/wecom")
    async def wecom_webhook(
        request: Request,
        msg_signature: str = Query(""),
        timestamp: str = Query(""),
        nonce: str = Query(""),
    ):
        if not _wecom_channel:
            return PlainTextResponse("success")
        raw_xml = (await request.body()).decode("utf-8")
        result = await _wecom_channel.handle_webhook(
            raw_xml=raw_xml,
            timestamp=timestamp,
            nonce=nonce,
            msg_signature=msg_signature,
        )
        return PlainTextResponse(result)

    @webhook_router.post("/dingtalk")
    async def dingtalk_webhook(request: Request):
        if not _dingtalk_channel:
            return {"success": True}
        try:
            body = await request.json()
        except Exception:
            return {"success": False, "reason": "invalid_json"}
        return await _dingtalk_channel.handle_webhook(body)

    @webhook_router.post("/slack")
    async def slack_webhook(request: Request):
        if _slack_channel is None:
            return JSONResponse(
                status_code=503,
                content={"success": False, "reason": "slack_channel_not_enabled"},
            )
        body = await request.body()
        headers = {str(k).lower(): str(v) for k, v in request.headers.items()}
        result = await _slack_channel.handle_http_event(body, headers)
        if result.get("challenge"):
            return PlainTextResponse(str(result["challenge"]))
        if not result.get("ok", False):
            return JSONResponse(
                status_code=403,
                content={"success": False, "reason": result.get("reason", "denied")},
            )
        return JSONResponse(status_code=202, content={"success": True})

    @webhook_router.post("/trigger/{agent_id}")
    async def generic_trigger_webhook(agent_id: str, request: Request):
        if _webhook_trigger_channel is None:
            return JSONResponse(
                status_code=503,
                content={"success": False, "reason": "webhook_trigger_channel_not_enabled"},
            )
        body_bytes = await request.body()
        headers = {str(k).lower(): str(v) for k, v in request.headers.items()}
        client_ip = str(request.client.host) if request.client else ""
        ok, reason = _webhook_trigger_channel.validate_request(
            body=body_bytes,
            headers=headers,
            client_ip=client_ip,
        )
        if not ok:
            return JSONResponse(status_code=403, content={"success": False, "reason": reason})
        payload: Any = {}
        if body_bytes:
            ctype = str(request.headers.get("content-type", "")).lower()
            try:
                if "application/json" in ctype:
                    payload = await request.json()
                elif "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
                    form = await request.form()
                    payload = {str(k): str(v) for k, v in form.items()}
                else:
                    text = body_bytes.decode("utf-8", errors="ignore").strip()
                    if text:
                        payload = {"content": text}
            except Exception:
                payload = {"content": body_bytes.decode("utf-8", errors="ignore")}
        query_map = {str(k): str(v) for k, v in request.query_params.items()}
        await _webhook_trigger_channel.ingest_trigger(
            agent_id=agent_id,
            payload=payload,
            query=query_map,
            client_ip=client_ip,
            metadata={"source": "dashboard.webhooks.trigger"},
        )
        return JSONResponse(status_code=202, content={"success": True, "agent_id": agent_id})
else:
    webhook_router = None
