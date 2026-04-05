"use strict";

const { createHash, createHmac } = require("crypto");

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

function stableJsonHash(value) {
  return createHash("sha256").update(stableStringify(value), "utf8").digest("hex");
}

function canonicalPolicySnapshot(value) {
  const snapshot = value && typeof value === "object" ? JSON.parse(JSON.stringify(value)) : {};
  delete snapshot.policy_snapshot_hash;
  return snapshot;
}

function canonicalPolicySnapshotHash(value) {
  return stableJsonHash(canonicalPolicySnapshot(value));
}

function canonicalGatewayEnvelope(payload) {
  const req = payload && typeof payload === "object" ? JSON.parse(JSON.stringify(payload)) : {};
  delete req.gateway_signature;
  if (req.gateway_meta && typeof req.gateway_meta === "object") {
    delete req.gateway_meta.request_hash;
  }
  return req;
}

function canonicalGatewayEnvelopeText(payload) {
  return stableStringify(canonicalGatewayEnvelope(payload));
}

function canonicalGatewayEnvelopeHash(payload) {
  return createHash("sha256").update(canonicalGatewayEnvelopeText(payload), "utf8").digest("hex");
}

function getGatewayKey(env = process.env) {
  const envKey = String(env.ZEN_CLAW_HMAC_MASTER_KEY || "").trim();
  if (!envKey) return null;
  return createHash("sha256").update(envKey, "utf8").digest();
}

function signGatewayEnvelope(payload, gatewayKey) {
  return createHmac("sha256", gatewayKey).update(canonicalGatewayEnvelopeText(payload), "utf8").digest("hex");
}

module.exports = {
  canonicalGatewayEnvelope,
  canonicalGatewayEnvelopeHash,
  canonicalGatewayEnvelopeText,
  canonicalPolicySnapshotHash,
  getGatewayKey,
  signGatewayEnvelope,
  stableJsonHash,
  stableStringify,
};
