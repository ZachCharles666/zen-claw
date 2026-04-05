"use strict";

const http = require("http");
const fs = require("fs");
const path = require("path");
const { createHash, createHmac, randomUUID } = require("crypto");
const { chromium } = require("playwright");

function parseBind(bind) {
  const raw = String(bind || "127.0.0.1:4500");
  const idx = raw.lastIndexOf(":");
  if (idx <= 0) return { host: "127.0.0.1", port: 4500 };
  const host = raw.slice(0, idx).trim() || "127.0.0.1";
  const port = Number(raw.slice(idx + 1)) || 4500;
  return { host, port };
}

function parseDomainList(text) {
  if (!text) return [];
  return String(text)
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
}

function isAllowedHost(host, allow, deny) {
  const h = String(host || "").toLowerCase();
  if (!h) return false;
  if (deny.some((d) => h === d || h.endsWith("." + d))) return false;
  if (allow.length === 0) return false;
  return allow.some((a) => h === a || h.endsWith("." + a));
}

function sendJson(res, status, payload) {
  const body = Buffer.from(JSON.stringify(payload));
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": String(body.length),
    "Cache-Control": "no-store",
  });
  res.end(body);
}

function safeJsonParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function stableStringify(value) {
  if (Array.isArray(value)) {
    return "[" + value.map((item) => stableStringify(item)).join(",") + "]";
  }
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return "{" + keys.map((key) => JSON.stringify(key) + ":" + stableStringify(value[key])).join(",") + "}";
  }
  return JSON.stringify(value);
}

function canonicalGatewayEnvelope(payload) {
  const req = payload && typeof payload === "object" ? JSON.parse(JSON.stringify(payload)) : {};
  delete req.gateway_signature;
  if (req.gateway_meta && typeof req.gateway_meta === "object") {
    delete req.gateway_meta.request_hash;
  }
  return req;
}

function getGatewayKey() {
  const envKey = String(process.env.ZEN_CLAW_HMAC_MASTER_KEY || "").trim();
  if (!envKey) return null;
  return createHash("sha256").update(envKey, "utf8").digest();
}

function isStrictGatewayVerification() {
  return String(process.env.ZEN_CLAW_ALLOW_INSECURE_SIDECAR || "").trim() === "";
}

function isApproved(req, method, routePath, rawBody) {
  if (approvalSecret) {
    const traceId = String(req.headers["x-trace-id"] || "").trim();
    const ts = String(req.headers["x-gateway-timestamp"] || req.headers["x-approval-timestamp"] || "").trim();
    const nonce = String(req.headers["x-gateway-nonce"] || "").trim();
    const requestHash = String(req.headers["x-gateway-request-hash"] || "").trim().toLowerCase();
    const gatewayInstance = String(req.headers["x-gateway-instance"] || "").trim();
    const policySnapshotHash = String(req.headers["x-policy-snapshot-hash"] || "").trim();
    const sig = String(req.headers["x-gateway-signature"] || req.headers["x-approval-signature"] || "").trim().toLowerCase();
    if (!traceId || !ts || !sig) return false;
    const tsNum = Number(ts);
    const now = Math.floor(Date.now() / 1000);
    if (!Number.isFinite(tsNum) || tsNum < now - 120 || tsNum > now + 120) return false;
    const bodyHash = createHash("sha256").update(String(rawBody || "")).digest("hex");
    if (requestHash && requestHash !== bodyHash) return false;
    const canonical = nonce || gatewayInstance || policySnapshotHash
      ? [traceId, ts, nonce, String(method || "").toUpperCase().trim(), String(routePath || "").trim(), bodyHash, gatewayInstance, policySnapshotHash].join("\n")
      : [traceId, ts, String(method || "").toUpperCase().trim(), String(routePath || "").trim(), bodyHash].join("\n");
    const expected = createHmac("sha256", approvalSecret).update(canonical).digest("hex");
    return expected === sig;
  }
  if (approvalToken) {
    const reqToken = String(req.headers["x-approval-token"] || "");
    return reqToken === approvalToken;
  }
  return true;
}

function validateGatewayEnvelope(payload) {
  const req = payload && typeof payload === "object" ? payload : {};
  const securityContext = req.security_context && typeof req.security_context === "object" ? req.security_context : {};
  const gatewayMeta = req.gateway_meta && typeof req.gateway_meta === "object" ? req.gateway_meta : {};
  const gatewaySignature = String(req.gateway_signature || "").trim();
  if (!gatewaySignature) {
    return { ok: false, code: "gateway_signature_required", error: "gateway signature is required" };
  }
  const requiredSecurity = [
    "channel",
    "sender_id",
    "chat_id",
    "tenant_id",
    "workspace_id",
    "agent_profile",
    "role",
    "trust_level",
    "origin_surface",
  ];
  const missing = requiredSecurity.filter((key) => !String(securityContext[key] || "").trim());
  if (missing.length) {
    return { ok: false, code: "security_context_missing", error: "security_context missing: " + missing.join(", ") };
  }
  if (!String(gatewayMeta.policy_snapshot_hash || "").trim()) {
    return { ok: false, code: "policy_snapshot_missing", error: "policy snapshot hash is required" };
  }
  if (!String(gatewayMeta.request_hash || "").trim()) {
    return { ok: false, code: "request_hash_missing", error: "gateway request hash is required" };
  }
  const gatewayKey = getGatewayKey();
  if (!gatewayKey && !isStrictGatewayVerification()) {
    return { ok: true };
  }
  if (!gatewayKey) {
    return { ok: false, code: "gateway_key_missing", error: "ZEN_CLAW_HMAC_MASTER_KEY is required in strict zero-trust mode" };
  }
  const canonical = canonicalGatewayEnvelope(req);
  const canonicalText = stableStringify(canonical);
  const canonicalHash = createHash("sha256").update(canonicalText, "utf8").digest("hex");
  if (String(gatewayMeta.request_hash || "").trim().toLowerCase() !== canonicalHash) {
    return { ok: false, code: "request_hash_mismatch", error: "gateway request hash does not match payload" };
  }
  const expectedSignature = createHmac("sha256", gatewayKey).update(canonicalText, "utf8").digest("hex");
  if (expectedSignature !== gatewaySignature.toLowerCase()) {
    return { ok: false, code: "gateway_signature_invalid", error: "gateway signature does not match payload" };
  }
  return { ok: true };
}

const bind = parseBind(process.env.BROWSER_SIDECAR_BIND);
const approvalToken = String(process.env.BROWSER_SIDECAR_TOKEN || "");
const approvalSecret = String(process.env.BROWSER_SIDECAR_SECRET || "");
const defaultAllow = parseDomainList(process.env.BROWSER_SIDECAR_ALLOW_DOMAINS);
const defaultDeny = parseDomainList(process.env.BROWSER_SIDECAR_DENY_DOMAINS);
const defaultMaxSteps = Math.max(1, Number(process.env.BROWSER_SIDECAR_MAX_STEPS || "20"));
const defaultTimeoutMs = Math.max(1000, Number(process.env.BROWSER_SIDECAR_TIMEOUT_SEC || "30") * 1000);
const stateDir = String(process.env.BROWSER_SIDECAR_STATE_DIR || "/tmp/browser-sessions");

if (isStrictGatewayVerification() && !getGatewayKey()) {
  process.stderr.write("browser-sidecar: strict zero-trust mode requires ZEN_CLAW_HMAC_MASTER_KEY\n");
  process.exit(1);
}

try {
  fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });
} catch (e) {
  process.stderr.write("browser-sidecar: failed to create state dir: " + String(e) + "\n");
}

let browser = null;
const sessions = new Map();

function mkSessionId() {
  return randomUUID();
}

async function ensureBrowser() {
  if (!browser) {
    browser = await chromium.launch({
      headless: true,
    });
  }
  return browser;
}

async function getOrCreateSession(sessionId) {
  const id = sessionId || mkSessionId();
  if (sessions.has(id)) return { id, s: sessions.get(id) };
  const b = await ensureBrowser();
  const context = await b.newContext({ acceptDownloads: false });
  const page = await context.newPage();
  const state = { context, page, steps: 0 };
  sessions.set(id, state);
  return { id, s: state };
}

function resolvePolicy(reqPolicy) {
  const allow = Array.isArray(reqPolicy && reqPolicy.allowed_domains)
    ? reqPolicy.allowed_domains.map((x) => String(x).toLowerCase().trim()).filter(Boolean)
    : defaultAllow;
  const deny = Array.isArray(reqPolicy && reqPolicy.blocked_domains)
    ? reqPolicy.blocked_domains.map((x) => String(x).toLowerCase().trim()).filter(Boolean)
    : defaultDeny;
  const maxSteps = Math.max(1, Number((reqPolicy && reqPolicy.max_steps) || defaultMaxSteps));
  return { allow, deny, maxSteps };
}

const server = http.createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/healthz") {
    return sendJson(res, 200, { ok: true });
  }
  if (!approvalSecret && approvalToken) {
    if (!isApproved(req, req.method, req.url, "")) {
      return sendJson(res, 403, { ok: false, error: "unauthorized", error_code: "unauthorized" });
    }
  }
  if (req.method !== "POST" || req.url !== "/v1/browser") {
    return sendJson(res, 404, { ok: false, error: "not found", error_code: "not_found" });
  }

  let body = "";
  req.on("data", (chunk) => {
    body += chunk.toString("utf-8");
    if (body.length > 512 * 1024) req.destroy();
  });

  req.on("end", async () => {
    if (!isApproved(req, req.method, req.url, body)) {
      return sendJson(res, 403, { ok: false, error: "unauthorized", error_code: "unauthorized" });
    }
    const payload = safeJsonParse(body);
    if (!payload || typeof payload !== "object") {
      return sendJson(res, 400, { ok: false, error: "invalid json", error_code: "invalid_json" });
    }
    const envelope = validateGatewayEnvelope(payload);
    if (!envelope.ok) {
      return sendJson(res, 403, { ok: false, error: envelope.error, error_code: envelope.code });
    }

    const action = String(payload.action || "").trim().toLowerCase();
    const actionPayload = payload.payload && typeof payload.payload === "object" ? payload.payload : {};
    const policy = resolvePolicy(payload.policy);

    try {
      if (action === "open") {
        const url = String(actionPayload.url || "").trim();
        if (!url) return sendJson(res, 400, { ok: false, error: "url required", error_code: "url_required" });
        let parsed;
        try {
          parsed = new URL(url);
        } catch {
          return sendJson(res, 400, { ok: false, error: "invalid url", error_code: "url_invalid" });
        }
        if (!["http:", "https:"].includes(parsed.protocol)) {
          return sendJson(res, 403, { ok: false, error: "scheme denied", error_code: "scheme_denied" });
        }
        if (!isAllowedHost(parsed.hostname, policy.allow, policy.deny)) {
          return sendJson(res, 403, { ok: false, error: "domain denied", error_code: "domain_denied" });
        }
        const sid = String(actionPayload.session_id || "").trim();
        const { id, s } = await getOrCreateSession(sid || undefined);
        if (s.steps >= policy.maxSteps) {
          return sendJson(res, 403, { ok: false, error: "step limit exceeded", error_code: "step_limit_exceeded" });
        }
        await s.page.goto(url, { timeout: defaultTimeoutMs, waitUntil: "domcontentloaded" });
        s.steps += 1;
        return sendJson(res, 200, {
          ok: true,
          action: "open",
          session_id: id,
          final_url: s.page.url(),
          title: await s.page.title(),
        });
      }

      if (action === "extract") {
        const sid = String(actionPayload.session_id || "").trim();
        if (!sid || !sessions.has(sid)) {
          return sendJson(res, 404, { ok: false, error: "session not found", error_code: "session_not_found" });
        }
        const s = sessions.get(sid);
        if (s.steps >= policy.maxSteps) {
          return sendJson(res, 403, { ok: false, error: "step limit exceeded", error_code: "step_limit_exceeded" });
        }
        const selector = String(actionPayload.selector || "").trim();
        const maxChars = Math.max(100, Number(actionPayload.max_chars || 10000));
        let text;
        if (selector) {
          const el = s.page.locator(selector).first();
          text = await el.innerText({ timeout: defaultTimeoutMs });
        } else {
          text = await s.page.locator("body").innerText({ timeout: defaultTimeoutMs });
        }
        s.steps += 1;
        if (text.length > maxChars) text = text.slice(0, maxChars);
        return sendJson(res, 200, { ok: true, action: "extract", session_id: sid, text });
      }

      if (action === "screenshot") {
        const sid = String(actionPayload.session_id || "").trim();
        if (!sid || !sessions.has(sid)) {
          return sendJson(res, 404, { ok: false, error: "session not found", error_code: "session_not_found" });
        }
        const s = sessions.get(sid);
        if (s.steps >= policy.maxSteps) {
          return sendJson(res, 403, { ok: false, error: "step limit exceeded", error_code: "step_limit_exceeded" });
        }
        const fullPage = Boolean(actionPayload.full_page);
        const png = await s.page.screenshot({ fullPage });
        s.steps += 1;
        return sendJson(res, 200, {
          ok: true,
          action: "screenshot",
          session_id: sid,
          mime: "image/png",
          image_base64: png.toString("base64"),
        });
      }

      if (action === "click") {
        const sid = String(actionPayload.session_id || "").trim();
        if (!sid || !sessions.has(sid)) {
          return sendJson(res, 404, { ok: false, error: "session not found", error_code: "session_not_found" });
        }
        const selector = String(actionPayload.selector || "").trim();
        if (!selector) {
          return sendJson(res, 400, { ok: false, error: "selector required", error_code: "selector_required" });
        }
        const s = sessions.get(sid);
        if (s.steps >= policy.maxSteps) {
          return sendJson(res, 403, { ok: false, error: "step limit exceeded", error_code: "step_limit_exceeded" });
        }
        await s.page.locator(selector).first().click({ timeout: defaultTimeoutMs });
        s.steps += 1;
        return sendJson(res, 200, { ok: true, action: "click", session_id: sid, selector });
      }

      if (action === "type") {
        const sid = String(actionPayload.session_id || "").trim();
        if (!sid || !sessions.has(sid)) {
          return sendJson(res, 404, { ok: false, error: "session not found", error_code: "session_not_found" });
        }
        const selector = String(actionPayload.selector || "").trim();
        if (!selector) {
          return sendJson(res, 400, { ok: false, error: "selector required", error_code: "selector_required" });
        }
        const text = String(actionPayload.text || "");
        const clear = Boolean(actionPayload.clear !== false);
        const s = sessions.get(sid);
        if (s.steps >= policy.maxSteps) {
          return sendJson(res, 403, { ok: false, error: "step limit exceeded", error_code: "step_limit_exceeded" });
        }
        const el = s.page.locator(selector).first();
        if (clear) {
          await el.fill("", { timeout: defaultTimeoutMs });
        }
        await el.type(text, { timeout: defaultTimeoutMs });
        s.steps += 1;
        return sendJson(res, 200, {
          ok: true,
          action: "type",
          session_id: sid,
          selector,
          typed_chars: text.length,
        });
      }

      if (action === "save_session") {
        const sid = String(actionPayload.session_id || "").trim();
        if (!sid || !sessions.has(sid)) {
          return sendJson(res, 404, {
            ok: false,
            error: "session not found",
            error_code: "session_not_found",
          });
        }
        const s = sessions.get(sid);
        const safeSid = sid.replace(/[^a-zA-Z0-9_-]/g, "_");
        const stateFile = path.join(stateDir, safeSid + ".json");
        try {
          await s.context.storageState({ path: stateFile });
          try {
            fs.chmodSync(stateFile, 0o600);
          } catch (_) {}
          return sendJson(res, 200, {
            ok: true,
            action: "save_session",
            session_id: sid,
            path: stateFile,
          });
        } catch (err) {
          return sendJson(res, 500, {
            ok: false,
            error: String(err && err.message ? err.message : err),
            error_code: "save_session_failed",
          });
        }
      }

      if (action === "load_session") {
        let stateFile = String(actionPayload.state_file || "").trim();
        if (!stateFile) {
          const sid = String(actionPayload.session_id || "").trim();
          if (!sid) {
            return sendJson(res, 400, {
              ok: false,
              error: "state_file or session_id required",
              error_code: "missing_state_reference",
            });
          }
          const safeSid = sid.replace(/[^a-zA-Z0-9_-]/g, "_");
          stateFile = path.join(stateDir, safeSid + ".json");
        }
        if (!fs.existsSync(stateFile)) {
          return sendJson(res, 404, {
            ok: false,
            error: "state file not found: " + stateFile,
            error_code: "state_file_not_found",
          });
        }
        try {
          const b = await ensureBrowser();
          const context = await b.newContext({
            acceptDownloads: false,
            storageState: stateFile,
          });
          const page = await context.newPage();
          const newId = mkSessionId();
          const state = { context, page, steps: 0 };
          sessions.set(newId, state);
          return sendJson(res, 200, {
            ok: true,
            action: "load_session",
            session_id: newId,
            state_file: stateFile,
          });
        } catch (err) {
          return sendJson(res, 500, {
            ok: false,
            error: String(err && err.message ? err.message : err),
            error_code: "load_session_failed",
          });
        }
      }

      return sendJson(res, 400, { ok: false, error: "unknown action", error_code: "unknown_action" });
    } catch (err) {
      return sendJson(res, 500, {
        ok: false,
        error: String(err && err.message ? err.message : err),
        error_code: "browser_action_failed",
      });
    }
  });
});

server.listen(bind.port, bind.host, () => {
  process.stdout.write(
    JSON.stringify({
      event: "browser_sidecar.start",
      bind: `${bind.host}:${bind.port}`,
      max_steps: defaultMaxSteps,
    }) + "\n"
  );
});

async function shutdown() {
  for (const [, s] of sessions) {
    try { await s.context.close(); } catch {}
  }
  sessions.clear();
  if (browser) {
    try { await browser.close(); } catch {}
  }
  process.exit(0);
}

process.on("SIGINT", () => { shutdown(); });
process.on("SIGTERM", () => { shutdown(); });
