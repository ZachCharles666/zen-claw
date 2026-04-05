"use strict";

const fs = require("fs");
const path = require("path");
const {
  canonicalGatewayEnvelopeHash,
  canonicalGatewayEnvelopeText,
  canonicalPolicySnapshotHash,
  getGatewayKey,
  signGatewayEnvelope,
} = require("./canonicalization");

process.env.ZEN_CLAW_HMAC_MASTER_KEY = "fixture-master-key";

const fixtures = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "..", "tests", "fixtures", "gateway_canonicalization_golden.json"), "utf8")
);

for (const fixture of fixtures) {
  const canonicalText = canonicalGatewayEnvelopeText(fixture.payload);
  if (canonicalText !== fixture.canonical_text) {
    throw new Error(`canonical_text mismatch for ${fixture.name}`);
  }
  const requestHash = canonicalGatewayEnvelopeHash(fixture.payload);
  if (requestHash !== fixture.request_hash) {
    throw new Error(`request_hash mismatch for ${fixture.name}`);
  }
  const gatewaySignature = signGatewayEnvelope(fixture.payload, getGatewayKey());
  if (gatewaySignature !== fixture.gateway_signature) {
    throw new Error(`gateway_signature mismatch for ${fixture.name}`);
  }
  const policySnapshotHash = canonicalPolicySnapshotHash(fixture.payload.policy_snapshot || {});
  if (policySnapshotHash !== fixture.payload.gateway_meta.policy_snapshot_hash) {
    throw new Error(`policy_snapshot_hash mismatch for ${fixture.name}`);
  }
}

process.stdout.write("gateway canonicalization fixtures OK\n");
