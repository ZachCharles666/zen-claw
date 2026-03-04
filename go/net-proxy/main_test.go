package main

import (
	"strings"
	"testing"
)

func TestParseSet(t *testing.T) {
	s := parseSet("example.com, api.example.com ,")
	if len(s) != 2 {
		t.Fatalf("expected 2 entries, got %d", len(s))
	}
	if _, ok := s["example.com"]; !ok {
		t.Fatalf("missing example.com")
	}
}

func TestAllowedDenied(t *testing.T) {
	allow := parseSet("a.com")
	deny := parseSet("b.com")
	if !allowed("a.com", allow) {
		t.Fatalf("expected a.com allowed")
	}
	if denied("a.com", deny) {
		t.Fatalf("did not expect a.com denied")
	}
	if !denied("b.com", deny) {
		t.Fatalf("expected b.com denied")
	}
}

func TestReadLimited(t *testing.T) {
	body, truncated, err := readLimited(strings.NewReader("abcdef"), 3)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if body != "abc" {
		t.Fatalf("unexpected body: %q", body)
	}
	if !truncated {
		t.Fatalf("expected truncated=true")
	}
}
