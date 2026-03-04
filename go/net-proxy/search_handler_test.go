package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"time"
	"testing"
)

func TestHandleSearchMethodNotAllowed(t *testing.T) {
	c := newTestCfg()
	req := httptest.NewRequest(http.MethodGet, "/v1/search", nil)
	rec := httptest.NewRecorder()
	handleSearch(rec, req, c)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", rec.Code)
	}
}

func TestHandleSearchInvalidJSON(t *testing.T) {
	c := newTestCfg()
	req := httptest.NewRequest(http.MethodPost, "/v1/search", bytes.NewBufferString("{bad"))
	rec := httptest.NewRecorder()
	handleSearch(rec, req, c)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
}

func TestHandleSearchMissingAPIKey(t *testing.T) {
	c := newTestCfg()
	body, _ := json.Marshal(searchRequest{Query: "hello", Count: 1})
	req := httptest.NewRequest(http.MethodPost, "/v1/search", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	handleSearch(rec, req, c)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
}

func TestHandleSearchDomainDenied(t *testing.T) {
	c := newTestCfg()
	c.DeniedDomains = parseSet("api.search.brave.com")
	body, _ := json.Marshal(searchRequest{Query: "hello", Count: 1, APIKey: "x"})
	req := httptest.NewRequest(http.MethodPost, "/v1/search", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	handleSearch(rec, req, c)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", rec.Code)
	}
}

func TestHandleSearchUpstreamHTTPError(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	defer upstream.Close()

	c := newTestCfg()
	c.SearchBaseURL = upstream.URL
	body, _ := json.Marshal(searchRequest{Query: "hello", Count: 1, APIKey: "x"})
	req := httptest.NewRequest(http.MethodPost, "/v1/search", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	handleSearch(rec, req, c)
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("expected 502, got %d", rec.Code)
	}
}

func TestHandleSearchUpstreamTimeout(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(1200 * time.Millisecond)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"web":{"results":[]}}`))
	}))
	defer upstream.Close()

	c := newTestCfg()
	c.SearchBaseURL = upstream.URL
	c.TimeoutSec = 1
	body, _ := json.Marshal(searchRequest{Query: "hello", Count: 1, APIKey: "x"})
	req := httptest.NewRequest(http.MethodPost, "/v1/search", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	handleSearch(rec, req, c)
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("expected 502, got %d", rec.Code)
	}
}
