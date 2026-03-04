"use strict";

const http = require("http");
const fs = require("fs");
const path = require("path");
const { randomUUID } = require("crypto");
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
  if (allow.length === 0) return true;
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

const bind = parseBind(process.env.BROWSER_SIDECAR_BIND);
const approvalToken = String(process.env.BROWSER_SIDECAR_TOKEN || "");
const defaultAllow = parseDomainList(process.env.BROWSER_SIDECAR_ALLOW_DOMAINS);
const defaultDeny = parseDomainList(process.env.BROWSER_SIDECAR_DENY_DOMAINS);
const defaultMaxSteps = Math.max(1, Number(process.env.BROWSER_SIDECAR_MAX_STEPS || "20"));
const defaultTimeoutMs = Math.max(1000, Number(process.env.BROWSER_SIDECAR_TIMEOUT_SEC || "30") * 1000);
const stateDir = String(process.env.BROWSER_SIDECAR_STATE_DIR || "/tmp/browser-sessions");

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
  // Verify approval token on all non-health endpoints (HIGH-001).
  // If BROWSER_SIDECAR_TOKEN is not set the check is skipped for backward compatibility.
  if (approvalToken) {
    const reqToken = String(req.headers["x-approval-token"] || "");
    if (reqToken !== approvalToken) {
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
    const payload = safeJsonParse(body);
    if (!payload || typeof payload !== "object") {
      return sendJson(res, 400, { ok: false, error: "invalid json", error_code: "invalid_json" });
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
