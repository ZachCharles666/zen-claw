package main

import (
	"encoding/json"
	"testing"
)

func TestAuditPayloadSchemaFields(t *testing.T) {
	retry := true
	p := auditPayload{
		Event:       "net.search.failed",
		TraceID:     "t-123",
		PolicyCode:  "search_failed",
		PolicyScope: "net_proxy",
		ErrorKind:   "retryable",
		Retryable:   &retry,
		Message:     "timeout",
	}

	raw, err := json.Marshal(p)
	if err != nil {
		t.Fatalf("marshal failed: %v", err)
	}

	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("unmarshal failed: %v", err)
	}
	for _, key := range []string{"event", "trace_id", "policy_code", "policy_scope", "error_kind", "message"} {
		if _, ok := out[key]; !ok {
			t.Fatalf("missing key: %s", key)
		}
	}
}
