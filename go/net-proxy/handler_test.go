package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestCfg() cfg {
	return cfg{
		BindAddress:    "127.0.0.1:4499",
		AllowedDomains: map[string]struct{}{},
		DeniedDomains:  map[string]struct{}{},
		MaxBodyBytes:   1024,
		TimeoutSec:     1,
		MaxRedirects:   1,
	}
}

func TestHandleFetchMethodNotAllowed(t *testing.T) {
	c := newTestCfg()
	req := httptest.NewRequest(http.MethodGet, "/v1/fetch", nil)
	rec := httptest.NewRecorder()

	handleFetch(rec, req, c)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", rec.Code)
	}
}

func TestHandleFetchInvalidJSON(t *testing.T) {
	c := newTestCfg()
	req := httptest.NewRequest(http.MethodPost, "/v1/fetch", bytes.NewBufferString("{bad"))
	rec := httptest.NewRecorder()

	handleFetch(rec, req, c)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
}

func TestHandleFetchInvalidURL(t *testing.T) {
	c := newTestCfg()
	body, _ := json.Marshal(fetchRequest{URL: "not-a-url"})
	req := httptest.NewRequest(http.MethodPost, "/v1/fetch", bytes.NewReader(body))
	rec := httptest.NewRecorder()

	handleFetch(rec, req, c)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
}

func TestHandleFetchSchemeNotAllowed(t *testing.T) {
	c := newTestCfg()
	body, _ := json.Marshal(fetchRequest{URL: "ftp://example.com"})
	req := httptest.NewRequest(http.MethodPost, "/v1/fetch", bytes.NewReader(body))
	rec := httptest.NewRecorder()

	handleFetch(rec, req, c)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
}

func TestHandleFetchDomainDenied(t *testing.T) {
	c := newTestCfg()
	c.DeniedDomains = parseSet("example.com")
	body, _ := json.Marshal(fetchRequest{URL: "https://example.com"})
	req := httptest.NewRequest(http.MethodPost, "/v1/fetch", bytes.NewReader(body))
	rec := httptest.NewRecorder()

	handleFetch(rec, req, c)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", rec.Code)
	}
}

func TestHandleFetchDomainNotAllowlisted(t *testing.T) {
	c := newTestCfg()
	c.AllowedDomains = parseSet("allowed.example")
	body, _ := json.Marshal(fetchRequest{URL: "https://example.com"})
	req := httptest.NewRequest(http.MethodPost, "/v1/fetch", bytes.NewReader(body))
	rec := httptest.NewRecorder()

	handleFetch(rec, req, c)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", rec.Code)
	}
}

func TestHandleFetchRedirectTargetDenied(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("target"))
	}))
	defer target.Close()

	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusFound)
	}))
	defer redirector.Close()

	c := newTestCfg()
	c.DeniedDomains = parseSet("127.0.0.1")

	srcURL := strings.Replace(redirector.URL, "127.0.0.1", "localhost", 1)
	body, _ := json.Marshal(fetchRequest{URL: srcURL})
	req := httptest.NewRequest(http.MethodPost, "/v1/fetch", bytes.NewReader(body))
	rec := httptest.NewRecorder()

	handleFetch(rec, req, c)
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("expected 502 on denied redirect target, got %d", rec.Code)
	}
}

func TestHandleFetchTruncatesBodyByMaxBytes(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("abcdefghijklmnopqrstuvwxyz"))
	}))
	defer srv.Close()

	c := newTestCfg()
	c.MaxBodyBytes = 10

	body, _ := json.Marshal(fetchRequest{URL: srv.URL})
	req := httptest.NewRequest(http.MethodPost, "/v1/fetch", bytes.NewReader(body))
	rec := httptest.NewRecorder()

	handleFetch(rec, req, c)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var out fetchResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("invalid json response: %v", err)
	}
	if !out.Truncated {
		t.Fatalf("expected truncated=true")
	}
	if len(out.Body) != 10 {
		t.Fatalf("expected body length 10, got %d", len(out.Body))
	}
}
