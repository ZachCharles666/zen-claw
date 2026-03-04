package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

type cfg struct {
	BindAddress    string
	AllowedDomains map[string]struct{}
	DeniedDomains  map[string]struct{}
	MaxBodyBytes   int64
	TimeoutSec     int
	MaxRedirects   int
	SearchBaseURL  string
}

type fetchRequest struct {
	URL      string `json:"url"`
	MaxBytes int64  `json:"max_bytes,omitempty"`
}

type fetchResponse struct {
	OK          bool   `json:"ok"`
	Status      int    `json:"status,omitempty"`
	FinalURL    string `json:"final_url,omitempty"`
	ContentType string `json:"content_type,omitempty"`
	Truncated   bool   `json:"truncated,omitempty"`
	Body        string `json:"body,omitempty"`
	ErrorCode   string `json:"error_code,omitempty"`
	Error       string `json:"error,omitempty"`
}

type searchRequest struct {
	Query  string `json:"query"`
	Count  int    `json:"count"`
	APIKey string `json:"api_key"`
}

type searchResult struct {
	Title       string `json:"title"`
	URL         string `json:"url"`
	Description string `json:"description,omitempty"`
}

type searchResponse struct {
	OK        bool           `json:"ok"`
	Results   []searchResult `json:"results,omitempty"`
	ErrorCode string         `json:"error_code,omitempty"`
	Error     string         `json:"error,omitempty"`
}

type auditPayload struct {
	Event       string `json:"event"`
	TraceID     string `json:"trace_id,omitempty"`
	PolicyCode  string `json:"policy_code,omitempty"`
	PolicyScope string `json:"policy_scope,omitempty"`
	ErrorKind   string `json:"error_kind,omitempty"`
	Retryable   *bool  `json:"retryable,omitempty"`
	Message     string `json:"message,omitempty"`
}

func main() {
	c := loadCfg()
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/v1/fetch", func(w http.ResponseWriter, r *http.Request) {
		handleFetch(w, r, c)
	})
	mux.HandleFunc("/v1/search", func(w http.ResponseWriter, r *http.Request) {
		handleSearch(w, r, c)
	})

	// WriteTimeout must exceed TimeoutSec so outbound fetches can complete
	// before the server closes the response writer.
	writeTimeout := time.Duration(c.TimeoutSec+30) * time.Second
	srv := &http.Server{
		Addr:              c.BindAddress,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      writeTimeout,
		IdleTimeout:       120 * time.Second,
	}
	log.Printf("net-proxy listening on %s", c.BindAddress)
	log.Fatal(srv.ListenAndServe())
}

func loadCfg() cfg {
	return cfg{
		BindAddress:    getenv("NET_PROXY_BIND", "127.0.0.1:4499"),
		AllowedDomains: parseSet(os.Getenv("NET_PROXY_ALLOW_DOMAINS")),
		DeniedDomains:  parseSet(os.Getenv("NET_PROXY_DENY_DOMAINS")),
		MaxBodyBytes:   int64(getenvInt("NET_PROXY_MAX_BODY_BYTES", 200000)),
		TimeoutSec:     getenvInt("NET_PROXY_TIMEOUT_SEC", 20),
		MaxRedirects:   getenvInt("NET_PROXY_MAX_REDIRECTS", 5),
		SearchBaseURL:  getenv("NET_PROXY_SEARCH_BASE_URL", "https://api.search.brave.com/res/v1/web/search"),
	}
}

func handleFetch(w http.ResponseWriter, r *http.Request, c cfg) {
	traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, fetchResponse{
			OK:        false,
			ErrorCode: "method_not_allowed",
			Error:     "POST required",
		})
		logAudit(auditPayload{
			Event:       "net.fetch.denied",
			TraceID:     traceID,
			PolicyCode:  "method_not_allowed",
			PolicyScope: "net_proxy",
			ErrorKind:   "parameter",
			Message:     "POST required",
		})
		return
	}

	bodyBytes, err := readBodyBytes(r, 1<<20)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, fetchResponse{
			OK:        false,
			ErrorCode: "invalid_body",
			Error:     err.Error(),
		})
		return
	}
	var req fetchRequest
	if err := json.Unmarshal(bodyBytes, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, fetchResponse{
			OK:        false,
			ErrorCode: "invalid_json",
			Error:     err.Error(),
		})
		logAudit(auditPayload{
			Event:       "net.fetch.denied",
			TraceID:     traceID,
			PolicyCode:  "invalid_json",
			PolicyScope: "net_proxy",
			ErrorKind:   "parameter",
			Message:     err.Error(),
		})
		return
	}
	target, err := url.Parse(strings.TrimSpace(req.URL))
	if err != nil || target.Scheme == "" || target.Host == "" {
		writeJSON(w, http.StatusBadRequest, fetchResponse{
			OK:        false,
			ErrorCode: "url_invalid",
			Error:     "invalid URL",
		})
		logAudit(auditPayload{
			Event:       "net.fetch.denied",
			TraceID:     traceID,
			PolicyCode:  "url_invalid",
			PolicyScope: "net_proxy",
			ErrorKind:   "parameter",
			Message:     "invalid URL",
		})
		return
	}
	if target.Scheme != "http" && target.Scheme != "https" {
		writeJSON(w, http.StatusBadRequest, fetchResponse{
			OK:        false,
			ErrorCode: "scheme_not_allowed",
			Error:     "only http/https are allowed",
		})
		logAudit(auditPayload{
			Event:       "net.fetch.denied",
			TraceID:     traceID,
			PolicyCode:  "scheme_not_allowed",
			PolicyScope: "net_proxy",
			ErrorKind:   "permission",
			Message:     "only http/https are allowed",
		})
		return
	}
	host := strings.ToLower(target.Hostname())
	if denied(host, c.DeniedDomains) {
		writeJSON(w, http.StatusForbidden, fetchResponse{
			OK:        false,
			ErrorCode: "domain_denied",
			Error:     "domain denied by policy",
		})
		logAudit(auditPayload{
			Event:       "net.fetch.denied",
			TraceID:     traceID,
			PolicyCode:  "domain_denied",
			PolicyScope: "net_proxy",
			ErrorKind:   "permission",
			Message:     host,
		})
		return
	}
	if len(c.AllowedDomains) > 0 && !allowed(host, c.AllowedDomains) {
		writeJSON(w, http.StatusForbidden, fetchResponse{
			OK:        false,
			ErrorCode: "domain_not_allowlisted",
			Error:     "domain not in allowlist",
		})
		logAudit(auditPayload{
			Event:       "net.fetch.denied",
			TraceID:     traceID,
			PolicyCode:  "domain_not_allowlisted",
			PolicyScope: "net_proxy",
			ErrorKind:   "permission",
			Message:     host,
		})
		return
	}

	maxBytes := c.MaxBodyBytes
	if req.MaxBytes > 0 && req.MaxBytes < maxBytes {
		maxBytes = req.MaxBytes
	}

	client := &http.Client{
		Timeout: time.Duration(c.TimeoutSec) * time.Second,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= c.MaxRedirects {
				return fmt.Errorf("too many redirects")
			}
			h := strings.ToLower(req.URL.Hostname())
			if denied(h, c.DeniedDomains) {
				return fmt.Errorf("redirect target denied")
			}
			if len(c.AllowedDomains) > 0 && !allowed(h, c.AllowedDomains) {
				return fmt.Errorf("redirect target not allowlisted")
			}
			return nil
		},
	}

	outReq, _ := http.NewRequest(http.MethodGet, target.String(), nil)
	outReq.Header.Set("User-Agent", "nano-claw-net-proxy/0.1")
	resp, err := client.Do(outReq)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, fetchResponse{
			OK:        false,
			ErrorCode: "fetch_failed",
			Error:     err.Error(),
		})
		retryable := true
		logAudit(auditPayload{
			Event:       "net.fetch.failed",
			TraceID:     traceID,
			PolicyCode:  "fetch_failed",
			PolicyScope: "net_proxy",
			ErrorKind:   "retryable",
			Retryable:   &retryable,
			Message:     err.Error(),
		})
		return
	}
	defer resp.Body.Close()

	body, truncated, err := readLimited(resp.Body, maxBytes)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, fetchResponse{
			OK:        false,
			ErrorCode: "read_failed",
			Error:     err.Error(),
		})
		retryable := false
		logAudit(auditPayload{
			Event:       "net.fetch.failed",
			TraceID:     traceID,
			PolicyCode:  "read_failed",
			PolicyScope: "net_proxy",
			ErrorKind:   "runtime",
			Retryable:   &retryable,
			Message:     err.Error(),
		})
		return
	}

	writeJSON(w, http.StatusOK, fetchResponse{
		OK:          true,
		Status:      resp.StatusCode,
		FinalURL:    resp.Request.URL.String(),
		ContentType: resp.Header.Get("Content-Type"),
		Truncated:   truncated,
		Body:        body,
	})
	logAudit(auditPayload{
		Event:       "net.fetch.allowed",
		TraceID:     traceID,
		PolicyScope: "net_proxy",
		Message:     resp.Request.URL.Hostname(),
	})
}

func handleSearch(w http.ResponseWriter, r *http.Request, c cfg) {
	traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
	if r.Method != http.MethodPost {
		writeJSONSearch(w, http.StatusMethodNotAllowed, searchResponse{
			OK:        false,
			ErrorCode: "method_not_allowed",
			Error:     "POST required",
		})
		logAudit(auditPayload{
			Event:       "net.search.denied",
			TraceID:     traceID,
			PolicyCode:  "method_not_allowed",
			PolicyScope: "net_proxy",
			ErrorKind:   "parameter",
			Message:     "POST required",
		})
		return
	}

	bodyBytes, err := readBodyBytes(r, 1<<20)
	if err != nil {
		writeJSONSearch(w, http.StatusBadRequest, searchResponse{
			OK:        false,
			ErrorCode: "invalid_body",
			Error:     err.Error(),
		})
		return
	}
	var req searchRequest
	if err := json.Unmarshal(bodyBytes, &req); err != nil {
		writeJSONSearch(w, http.StatusBadRequest, searchResponse{
			OK:        false,
			ErrorCode: "invalid_json",
			Error:     err.Error(),
		})
		logAudit(auditPayload{
			Event:       "net.search.denied",
			TraceID:     traceID,
			PolicyCode:  "invalid_json",
			PolicyScope: "net_proxy",
			ErrorKind:   "parameter",
			Message:     err.Error(),
		})
		return
	}

	if strings.TrimSpace(req.Query) == "" {
		writeJSONSearch(w, http.StatusBadRequest, searchResponse{
			OK:        false,
			ErrorCode: "query_required",
			Error:     "query is required",
		})
		logAudit(auditPayload{
			Event:       "net.search.denied",
			TraceID:     traceID,
			PolicyCode:  "query_required",
			PolicyScope: "net_proxy",
			ErrorKind:   "parameter",
			Message:     "query is required",
		})
		return
	}
	if strings.TrimSpace(req.APIKey) == "" {
		writeJSONSearch(w, http.StatusBadRequest, searchResponse{
			OK:        false,
			ErrorCode: "brave_api_key_missing",
			Error:     "api_key is required",
		})
		logAudit(auditPayload{
			Event:       "net.search.denied",
			TraceID:     traceID,
			PolicyCode:  "brave_api_key_missing",
			PolicyScope: "net_proxy",
			ErrorKind:   "parameter",
			Message:     "api_key is required",
		})
		return
	}
	if req.Count <= 0 {
		req.Count = 5
	}
	if req.Count > 10 {
		req.Count = 10
	}

	host := "api.search.brave.com"
	if denied(host, c.DeniedDomains) {
		writeJSONSearch(w, http.StatusForbidden, searchResponse{
			OK:        false,
			ErrorCode: "domain_denied",
			Error:     "domain denied by policy",
		})
		logAudit(auditPayload{
			Event:       "net.search.denied",
			TraceID:     traceID,
			PolicyCode:  "domain_denied",
			PolicyScope: "net_proxy",
			ErrorKind:   "permission",
			Message:     host,
		})
		return
	}
	if len(c.AllowedDomains) > 0 && !allowed(host, c.AllowedDomains) {
		writeJSONSearch(w, http.StatusForbidden, searchResponse{
			OK:        false,
			ErrorCode: "domain_not_allowlisted",
			Error:     "domain not in allowlist",
		})
		logAudit(auditPayload{
			Event:       "net.search.denied",
			TraceID:     traceID,
			PolicyCode:  "domain_not_allowlisted",
			PolicyScope: "net_proxy",
			ErrorKind:   "permission",
			Message:     host,
		})
		return
	}

	client := &http.Client{Timeout: time.Duration(c.TimeoutSec) * time.Second}
	base, err := url.Parse(c.SearchBaseURL)
	if err != nil || base.Scheme == "" || base.Host == "" {
		writeJSONSearch(w, http.StatusBadGateway, searchResponse{
			OK:        false,
			ErrorCode: "search_base_url_invalid",
			Error:     "invalid search base URL",
		})
		logAudit(auditPayload{
			Event:       "net.search.failed",
			TraceID:     traceID,
			PolicyCode:  "search_base_url_invalid",
			PolicyScope: "net_proxy",
			ErrorKind:   "runtime",
			Message:     "invalid search base URL",
		})
		return
	}
	q := base.Query()
	q.Set("q", req.Query)
	q.Set("count", fmt.Sprintf("%d", req.Count))
	base.RawQuery = q.Encode()
	outReq, _ := http.NewRequest(http.MethodGet, base.String(), nil)
	outReq.Header.Set("Accept", "application/json")
	outReq.Header.Set("X-Subscription-Token", req.APIKey)
	resp, err := client.Do(outReq)
	if err != nil {
		writeJSONSearch(w, http.StatusBadGateway, searchResponse{
			OK:        false,
			ErrorCode: "search_failed",
			Error:     err.Error(),
		})
		retryable := true
		logAudit(auditPayload{
			Event:       "net.search.failed",
			TraceID:     traceID,
			PolicyCode:  "search_failed",
			PolicyScope: "net_proxy",
			ErrorKind:   "retryable",
			Retryable:   &retryable,
			Message:     err.Error(),
		})
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		writeJSONSearch(w, http.StatusBadGateway, searchResponse{
			OK:        false,
			ErrorCode: "search_http_error",
			Error:     fmt.Sprintf("upstream returned %d", resp.StatusCode),
		})
		retryable := true
		logAudit(auditPayload{
			Event:       "net.search.failed",
			TraceID:     traceID,
			PolicyCode:  "search_http_error",
			PolicyScope: "net_proxy",
			ErrorKind:   "retryable",
			Retryable:   &retryable,
			Message:     fmt.Sprintf("upstream returned %d", resp.StatusCode),
		})
		return
	}

	var raw map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&raw); err != nil {
		writeJSONSearch(w, http.StatusBadGateway, searchResponse{
			OK:        false,
			ErrorCode: "search_parse_failed",
			Error:     err.Error(),
		})
		retryable := false
		logAudit(auditPayload{
			Event:       "net.search.failed",
			TraceID:     traceID,
			PolicyCode:  "search_parse_failed",
			PolicyScope: "net_proxy",
			ErrorKind:   "runtime",
			Retryable:   &retryable,
			Message:     err.Error(),
		})
		return
	}

	results := make([]searchResult, 0, req.Count)
	if web, ok := raw["web"].(map[string]any); ok {
		if items, ok := web["results"].([]any); ok {
			for _, it := range items {
				obj, ok := it.(map[string]any)
				if !ok {
					continue
				}
				results = append(results, searchResult{
					Title:       toString(obj["title"]),
					URL:         toString(obj["url"]),
					Description: toString(obj["description"]),
				})
				if len(results) >= req.Count {
					break
				}
			}
		}
	}

	writeJSONSearch(w, http.StatusOK, searchResponse{OK: true, Results: results})
	logAudit(auditPayload{
		Event:       "net.search.allowed",
		TraceID:     traceID,
		PolicyScope: "net_proxy",
		Message:     fmt.Sprintf("results=%d", len(results)),
	})
}

func readLimited(r io.Reader, max int64) (string, bool, error) {
	limited := io.LimitReader(r, max+1)
	buf, err := io.ReadAll(limited)
	if err != nil {
		return "", false, err
	}
	if int64(len(buf)) > max {
		return string(buf[:max]), true, nil
	}
	return string(buf), false, nil
}

func parseSet(v string) map[string]struct{} {
	res := map[string]struct{}{}
	for _, p := range strings.Split(v, ",") {
		p = strings.ToLower(strings.TrimSpace(p))
		if p != "" {
			res[p] = struct{}{}
		}
	}
	return res
}

func denied(host string, deny map[string]struct{}) bool {
	_, ok := deny[host]
	return ok
}

func allowed(host string, allow map[string]struct{}) bool {
	_, ok := allow[host]
	return ok
}

func writeJSON(w http.ResponseWriter, status int, body fetchResponse) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeJSONSearch(w http.ResponseWriter, status int, body searchResponse) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func logAudit(payload auditPayload) {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(payload)
	// Write audit JSON lines to stdout without log prefix so downstream tooling can parse reliably.
	_, _ = fmt.Fprintln(os.Stdout, strings.TrimSpace(b.String()))
}

func getenv(name, def string) string {
	v := strings.TrimSpace(os.Getenv(name))
	if v == "" {
		return def
	}
	return v
}

func getenvInt(name string, def int) int {
	v := strings.TrimSpace(os.Getenv(name))
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

func toString(v any) string {
	s, _ := v.(string)
	return s
}

func readBodyBytes(r *http.Request, maxBytes int64) ([]byte, error) {
	defer r.Body.Close()
	limited := io.LimitReader(r.Body, maxBytes+1)
	b, err := io.ReadAll(limited)
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > maxBytes {
		return nil, fmt.Errorf("body too large")
	}
	return b, nil
}

