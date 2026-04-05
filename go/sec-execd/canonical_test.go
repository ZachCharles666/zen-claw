package main

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

type gatewayFixture struct {
	Name             string         `json:"name"`
	Payload          map[string]any `json:"payload"`
	CanonicalPayload map[string]any `json:"canonical_payload"`
	CanonicalText    string         `json:"canonical_text"`
	RequestHash      string         `json:"request_hash"`
	GatewaySignature string         `json:"gateway_signature"`
}

func loadGatewayFixtures(t *testing.T) []gatewayFixture {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "tests", "fixtures", "gateway_canonicalization_golden.json"))
	if err != nil {
		t.Fatalf("read fixtures: %v", err)
	}
	var fixtures []gatewayFixture
	if err := json.Unmarshal(raw, &fixtures); err != nil {
		t.Fatalf("decode fixtures: %v", err)
	}
	return fixtures
}

func TestCanonicalGatewayPayloadFixtures(t *testing.T) {
	t.Setenv("ZEN_CLAW_HMAC_MASTER_KEY", "fixture-master-key")
	for _, fixture := range loadGatewayFixtures(t) {
		t.Run(fixture.Name, func(t *testing.T) {
			canonical, err := canonicalGatewayPayload(fixture.Payload)
			if err != nil {
				t.Fatalf("canonicalGatewayPayload: %v", err)
			}
			if string(canonical) != fixture.CanonicalText {
				t.Fatalf("canonical text mismatch\nexpected: %s\nactual:   %s", fixture.CanonicalText, string(canonical))
			}
			if got := fmt.Sprintf("%x", sha256.Sum256(canonical)); got != fixture.RequestHash {
				t.Fatalf("request hash mismatch: %s", got)
			}
			if !verifyGatewayPayloadSignature(canonical, fixture.GatewaySignature) {
				t.Fatalf("gateway signature mismatch")
			}
			if got := stableMapHash(fixture.Payload["policy_snapshot"].(map[string]any)); got != fixture.Payload["gateway_meta"].(map[string]any)["policy_snapshot_hash"] {
				t.Fatalf("policy snapshot hash mismatch: %s", got)
			}
		})
	}
}
