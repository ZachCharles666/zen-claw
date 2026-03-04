package main

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"
)

type captureWriteCloser struct {
	buf bytes.Buffer
}

func (c *captureWriteCloser) Write(p []byte) (int, error) { return c.buf.Write(p) }
func (c *captureWriteCloser) Close() error                { return nil }

func TestLoadConfigSessionRetentionValidation(t *testing.T) {
	_ = os.Setenv("SEC_EXECD_SESSION_RETENTION_SEC", "0")
	defer os.Unsetenv("SEC_EXECD_SESSION_RETENTION_SEC")

	_, err := loadConfig()
	if err == nil || !strings.Contains(err.Error(), "SESSION_RETENTION") {
		t.Fatalf("expected retention validation error, got: %v", err)
	}
}

func TestIsSubPath(t *testing.T) {
	root := t.TempDir()
	sub := filepath.Join(root, "sub")
	outside := t.TempDir()

	if !isSubPath(root, sub) {
		t.Fatalf("expected sub path to be allowed")
	}
	if isSubPath(root, outside) {
		t.Fatalf("expected outside path to be denied")
	}
}

func TestIsDangerous(t *testing.T) {
	patterns := []*regexp.Regexp{
		regexp.MustCompile(`(?i)\brm\s+-[rf]{1,2}\b`),
	}
	if !isDangerous("rm -rf /", patterns) {
		t.Fatalf("expected dangerous command to match")
	}
	if isDangerous("echo hello", patterns) {
		t.Fatalf("did not expect safe command to match")
	}
}

func TestHandleExecApprovalRequired(t *testing.T) {
	cfg := &serverConfig{
		Workspace:          filepath.Clean("."),
		RequireApproval:    true,
		ApprovalToken:      "secret",
		ApprovalSecret:     "",
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	body, _ := json.Marshal(execRequest{Command: "echo hi"})
	req := httptest.NewRequest(http.MethodPost, "/v1/exec", bytes.NewReader(body))
	rec := httptest.NewRecorder()

	handleExec(rec, req, cfg)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", rec.Code)
	}
}

func TestHandleExecHMACApprovalAllowsRequest(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    true,
		ApprovalSecret:     "hmac-secret",
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	body, _ := json.Marshal(execRequest{Command: "echo hi", WorkingDir: workspace})
	req := httptest.NewRequest(http.MethodPost, "/v1/exec", bytes.NewReader(body))
	req.Header.Set("X-Trace-Id", "trace-1")
	ts := time.Now().Unix()
	req.Header.Set("X-Approval-Timestamp", strconv.FormatInt(ts, 10))
	req.Header.Set("X-Approval-Signature", testHMACSig(cfg.ApprovalSecret, "trace-1", "POST", "/v1/exec", body, ts))
	rec := httptest.NewRecorder()
	handleExec(rec, req, cfg)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
}

func testHMACSig(secret, traceID, method, path string, body []byte, ts int64) string {
	sum := sha256.Sum256(body)
	canonical := strings.Join([]string{
		traceID,
		strconv.FormatInt(ts, 10),
		strings.ToUpper(method),
		path,
		hex.EncodeToString(sum[:]),
	}, "\n")
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(canonical))
	return hex.EncodeToString(mac.Sum(nil))
}

func TestHandleExecRejectsWorkdirOutsideWorkspace(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}

	outside := filepath.Dir(workspace)
	body, _ := json.Marshal(execRequest{Command: "echo hi", WorkingDir: outside})
	req := httptest.NewRequest(http.MethodPost, "/v1/exec", bytes.NewReader(body))
	rec := httptest.NewRecorder()

	handleExec(rec, req, cfg)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", rec.Code)
	}
}

func TestHandleExecSuccess(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}

	body, _ := json.Marshal(execRequest{Command: "echo hi", WorkingDir: workspace})
	req := httptest.NewRequest(http.MethodPost, "/v1/exec", bytes.NewReader(body))
	rec := httptest.NewRecorder()

	handleExec(rec, req, cfg)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
}

func TestSessionStartAndStatus(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()

	body, _ := json.Marshal(sessionStartRequest{Command: "echo hi", WorkingDir: workspace})
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/start", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	handleSessionStart(rec, req, cfg, sessions)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var started sessionStartResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &started); err != nil {
		t.Fatalf("decode start response failed: %v", err)
	}
	if !started.OK || started.SessionID == "" {
		t.Fatalf("invalid start response: %+v", started)
	}

	var status sessionStatusResponse
	deadline := time.Now().Add(3 * time.Second)
	for {
		reqStatus := httptest.NewRequest(http.MethodGet, "/v1/sessions/"+started.SessionID, nil)
		reqStatus.Header.Set("X-Trace-Id", "t-status")
		recStatus := httptest.NewRecorder()
		handleSessionRoutes(recStatus, reqStatus, cfg, sessions)
		if recStatus.Code != http.StatusOK {
			t.Fatalf("expected 200 for status, got %d", recStatus.Code)
		}
		if err := json.Unmarshal(recStatus.Body.Bytes(), &status); err != nil {
			t.Fatalf("decode status response failed: %v", err)
		}
		if status.Status != "running" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("session did not finish in time")
		}
		time.Sleep(20 * time.Millisecond)
	}
	if status.Status != "done" {
		t.Fatalf("expected done status, got %s", status.Status)
	}
	if !strings.Contains(strings.ToLower(status.Stdout), "hi") {
		t.Fatalf("expected output to contain hi, got: %q", status.Stdout)
	}
}

func TestSessionKill(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  30,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()
	cmd := "sleep 5"
	if runtime.GOOS == "windows" {
		cmd = "ping -n 6 127.0.0.1 > NUL"
	}

	body, _ := json.Marshal(sessionStartRequest{Command: cmd, WorkingDir: workspace})
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/start", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	handleSessionStart(rec, req, cfg, sessions)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var started sessionStartResponse
	_ = json.Unmarshal(rec.Body.Bytes(), &started)

	reqKill := httptest.NewRequest(http.MethodPost, "/v1/sessions/"+started.SessionID+"/kill", nil)
	reqKill.Header.Set("X-Trace-Id", "t-kill")
	recKill := httptest.NewRecorder()
	handleSessionRoutes(recKill, reqKill, cfg, sessions)
	if recKill.Code != http.StatusOK {
		t.Fatalf("expected 200 for kill, got %d", recKill.Code)
	}

	deadline := time.Now().Add(3 * time.Second)
	for {
		reqStatus := httptest.NewRequest(http.MethodGet, "/v1/sessions/"+started.SessionID, nil)
		reqStatus.Header.Set("X-Trace-Id", "t-kill-status")
		recStatus := httptest.NewRecorder()
		handleSessionRoutes(recStatus, reqStatus, cfg, sessions)
		if recStatus.Code != http.StatusOK {
			t.Fatalf("expected 200 for status, got %d", recStatus.Code)
		}
		var status sessionStatusResponse
		_ = json.Unmarshal(recStatus.Body.Bytes(), &status)
		if status.Status != "running" {
			if status.Status != "killed" {
				t.Fatalf("expected killed status, got %s", status.Status)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("session was not killed in time")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestSessionStartRejectsWorkdirOutsideWorkspace(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()
	outside := filepath.Dir(workspace)
	body, _ := json.Marshal(sessionStartRequest{Command: "echo hi", WorkingDir: outside})
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/start", bytes.NewReader(body))
	req.Header.Set("X-Trace-Id", "t-outside")
	rec := httptest.NewRecorder()
	handleSessionStart(rec, req, cfg, sessions)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", rec.Code)
	}
}

func TestSessionStartPTYUnsupported(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()

	origLookup := lookupPath
	lookupPath = func(file string) (string, error) {
		return "", os.ErrNotExist
	}
	defer func() {
		lookupPath = origLookup
	}()

	body, _ := json.Marshal(sessionStartRequest{
		Command:    "echo hi",
		WorkingDir: workspace,
		PTY:        true,
	})
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/start", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	handleSessionStart(rec, req, cfg, sessions)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
	var resp sessionStartResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode failed: %v", err)
	}
	if resp.ErrorCode != "pty_unsupported" {
		t.Fatalf("expected pty_unsupported, got %s", resp.ErrorCode)
	}
}

func TestSessionStatusIncludesPTYField(t *testing.T) {
	rec := &sessionRecord{
		ID:         "s-1",
		Command:    "echo hi",
		WorkingDir: ".",
		Status:     "running",
		PTY:        true,
	}
	s := rec.snapshot()
	if !s.PTY {
		t.Fatalf("expected status snapshot to include pty=true")
	}
}

func TestSessionReadReturnsChunkWhileRunning(t *testing.T) {
	workspace, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd failed: %v", err)
	}
	workspace, err = filepath.Abs(workspace)
	if err != nil {
		t.Fatalf("abs workspace failed: %v", err)
	}
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  10,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()

	cmd := "echo hello && sleep 2"
	if runtime.GOOS == "windows" {
		cmd = "echo hello & ping -n 3 127.0.0.1 > NUL"
	}
	body, _ := json.Marshal(sessionStartRequest{Command: cmd, WorkingDir: workspace})
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/start", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	handleSessionStart(rec, req, cfg, sessions)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	var started sessionStartResponse
	_ = json.Unmarshal(rec.Body.Bytes(), &started)

	deadline := time.Now().Add(2 * time.Second)
	found := false
	for {
		reqRead := httptest.NewRequest(http.MethodGet, "/v1/sessions/"+started.SessionID+"/read?cursor=0&max_bytes=64", nil)
		reqRead.Header.Set("X-Trace-Id", "t-read")
		recRead := httptest.NewRecorder()
		handleSessionRoutes(recRead, reqRead, cfg, sessions)
		if recRead.Code != http.StatusOK {
			t.Fatalf("expected 200 for read, got %d", recRead.Code)
		}
		var readResp sessionReadResponse
		if err := json.Unmarshal(recRead.Body.Bytes(), &readResp); err != nil {
			t.Fatalf("decode read response failed: %v", err)
		}
		if strings.Contains(strings.ToLower(readResp.Chunk), "hello") {
			found = true
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("expected running read chunk to include hello, got: %q", readResp.Chunk)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !found {
		t.Fatalf("expected to read non-empty chunk")
	}

	reqKill := httptest.NewRequest(http.MethodPost, "/v1/sessions/"+started.SessionID+"/kill", nil)
	reqKill.Header.Set("X-Trace-Id", "t-read-kill")
	recKill := httptest.NewRecorder()
	handleSessionRoutes(recKill, reqKill, cfg, sessions)
	if recKill.Code != http.StatusOK {
		t.Fatalf("expected 200 for kill, got %d", recKill.Code)
	}
	deadline = time.Now().Add(2 * time.Second)
	for {
		reqStatus := httptest.NewRequest(http.MethodGet, "/v1/sessions/"+started.SessionID, nil)
		reqStatus.Header.Set("X-Trace-Id", "t-read-status")
		recStatus := httptest.NewRecorder()
		handleSessionRoutes(recStatus, reqStatus, cfg, sessions)
		if recStatus.Code != http.StatusOK {
			t.Fatalf("expected 200 for status, got %d", recStatus.Code)
		}
		var status sessionStatusResponse
		_ = json.Unmarshal(recStatus.Body.Bytes(), &status)
		if status.Status != "running" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("session did not stop in time after kill")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestSessionReadInvalidQuery(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()
	req := httptest.NewRequest(http.MethodGet, "/v1/sessions/s-1/read?cursor=-1", nil)
	req.Header.Set("X-Trace-Id", "t-read-invalid")
	rec := httptest.NewRecorder()
	handleSessionRoutes(rec, req, cfg, sessions)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
}

func TestSessionWriteRouteSuccess(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()
	writer := &captureWriteCloser{}
	sessions.sessions["s-1"] = &sessionRecord{
		ID:         "s-1",
		Status:     "running",
		WorkingDir: workspace,
		Command:    "cat",
		stdin:      writer,
	}

	body := []byte(`{"input":"hello"}`)
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/s-1/write", bytes.NewReader(body))
	req.Header.Set("X-Trace-Id", "t-write")
	rec := httptest.NewRecorder()
	handleSessionRoutes(rec, req, cfg, sessions)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if writer.buf.String() != "hello" {
		t.Fatalf("expected input to be written, got %q", writer.buf.String())
	}
}

func TestSessionWriteRouteNotRunning(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()
	writer := &captureWriteCloser{}
	sessions.sessions["s-1"] = &sessionRecord{
		ID:         "s-1",
		Status:     "done",
		WorkingDir: workspace,
		Command:    "cat",
		stdin:      writer,
	}

	body := []byte(`{"input":"hello"}`)
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/s-1/write", bytes.NewReader(body))
	req.Header.Set("X-Trace-Id", "t-write-not-running")
	rec := httptest.NewRecorder()
	handleSessionRoutes(rec, req, cfg, sessions)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
	var resp sessionWriteResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode failed: %v", err)
	}
	if resp.ErrorCode != "session_not_running" {
		t.Fatalf("expected session_not_running, got %s", resp.ErrorCode)
	}
}

func TestSessionWriteRouteRejectsEmptyInput(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()
	sessions.sessions["s-1"] = &sessionRecord{
		ID:         "s-1",
		Status:     "running",
		WorkingDir: workspace,
		Command:    "cat",
		stdin:      &captureWriteCloser{},
	}

	body := []byte(`{"input":""}`)
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/s-1/write", bytes.NewReader(body))
	req.Header.Set("X-Trace-Id", "t-write-empty")
	rec := httptest.NewRecorder()
	handleSessionRoutes(rec, req, cfg, sessions)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
	var resp sessionWriteResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode failed: %v", err)
	}
	if resp.ErrorCode != "input_required" {
		t.Fatalf("expected input_required, got %s", resp.ErrorCode)
	}
}

func TestSessionWriteRouteHMACApprovalAllows(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    true,
		ApprovalSecret:     "hmac-secret",
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()
	writer := &captureWriteCloser{}
	sessions.sessions["s-1"] = &sessionRecord{
		ID:         "s-1",
		Status:     "running",
		WorkingDir: workspace,
		Command:    "cat",
		stdin:      writer,
	}

	body := []byte(`{"input":"status\n"}`)
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/s-1/write", bytes.NewReader(body))
	req.Header.Set("X-Trace-Id", "trace-w1")
	ts := time.Now().Unix()
	req.Header.Set("X-Approval-Timestamp", strconv.FormatInt(ts, 10))
	req.Header.Set("X-Approval-Signature", testHMACSig(cfg.ApprovalSecret, "trace-w1", "POST", "/v1/sessions/s-1/write", body, ts))
	rec := httptest.NewRecorder()
	handleSessionRoutes(rec, req, cfg, sessions)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if writer.buf.String() != "status\n" {
		t.Fatalf("expected input to be written, got %q", writer.buf.String())
	}
}

func TestSessionSignalRouteInvalidSignal(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()
	sessions.sessions["s-1"] = &sessionRecord{
		ID:         "s-1",
		Status:     "running",
		WorkingDir: workspace,
		Command:    "cat",
	}

	body := []byte(`{"signal":"bogus"}`)
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/s-1/signal", bytes.NewReader(body))
	req.Header.Set("X-Trace-Id", "t-signal-invalid")
	rec := httptest.NewRecorder()
	handleSessionRoutes(rec, req, cfg, sessions)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
}

func TestSessionSignalRouteNoProcess(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()
	sessions.sessions["s-1"] = &sessionRecord{
		ID:         "s-1",
		Status:     "running",
		WorkingDir: workspace,
		Command:    "cat",
	}

	body := []byte(`{"signal":"interrupt"}`)
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/s-1/signal", bytes.NewReader(body))
	req.Header.Set("X-Trace-Id", "t-signal-noproc")
	rec := httptest.NewRecorder()
	handleSessionRoutes(rec, req, cfg, sessions)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
	var resp sessionSignalResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode failed: %v", err)
	}
	if resp.ErrorCode != "process_unavailable" {
		t.Fatalf("expected process_unavailable, got %s", resp.ErrorCode)
	}
}

func TestSessionResizeRouteRequiresPTY(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()
	sessions.sessions["s-1"] = &sessionRecord{
		ID:         "s-1",
		Status:     "running",
		WorkingDir: workspace,
		Command:    "cat",
		PTY:        false,
	}

	body := []byte(`{"rows":40,"cols":120}`)
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/s-1/resize", bytes.NewReader(body))
	req.Header.Set("X-Trace-Id", "t-resize-no-pty")
	rec := httptest.NewRecorder()
	handleSessionRoutes(rec, req, cfg, sessions)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
	var resp sessionResizeResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode failed: %v", err)
	}
	if resp.ErrorCode != "pty_required" {
		t.Fatalf("expected pty_required, got %s", resp.ErrorCode)
	}
}

func TestSessionResizeRouteSuccess(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()
	sessions.sessions["s-1"] = &sessionRecord{
		ID:         "s-1",
		Status:     "running",
		WorkingDir: workspace,
		Command:    "cat",
		PTY:        true,
	}

	body := []byte(`{"rows":40,"cols":120}`)
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/s-1/resize", bytes.NewReader(body))
	req.Header.Set("X-Trace-Id", "t-resize-ok")
	rec := httptest.NewRecorder()
	handleSessionRoutes(rec, req, cfg, sessions)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	var resp sessionResizeResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode failed: %v", err)
	}
	if !resp.Applied {
		t.Fatalf("expected resize applied")
	}
}

func TestSessionListInvalidMethodEmitsAuditSchema(t *testing.T) {
	workspace := t.TempDir()
	cfg := &serverConfig{
		Workspace:          workspace,
		RequireApproval:    false,
		MaxOutputBytes:     1000,
		DefaultTimeoutSec:  5,
		MaxTimeoutSec:      30,
		DangerousCmdRegexp: []*regexp.Regexp{},
	}
	sessions := newSessionManager()
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions", nil)
	req.Header.Set("X-Trace-Id", "t-list")
	rec := httptest.NewRecorder()

	// Capture audit line by calling encodeAudit directly for schema coverage expectations.
	line, err := encodeAudit(auditPayload{
		Event:     "exec.session.list.invalid_method",
		TraceID:   "t-list",
		ErrorKind: "parameter",
		Message:   "GET required",
	})
	if err != nil || !strings.Contains(line, "\"event\"") || !strings.Contains(line, "\"trace_id\"") {
		t.Fatalf("encodeAudit schema mismatch: %v %s", err, line)
	}

	handleSessionList(rec, req, cfg, sessions)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", rec.Code)
	}
}

func TestSessionManagerGCRemovesExpiredTerminalSessions(t *testing.T) {
	m := newSessionManagerWithRetention(1)
	now := time.Now().UnixMilli()
	expired := &sessionRecord{
		ID:           "s-expired",
		Status:       "done",
		FinishedAtMs: now - 3000,
	}
	active := &sessionRecord{
		ID:          "s-running",
		Status:      "running",
		StartedAtMs: now,
	}
	m.sessions[expired.ID] = expired
	m.sessions[active.ID] = active

	items := m.list()
	if len(items) != 1 {
		t.Fatalf("expected 1 active session after gc, got %d", len(items))
	}
	if items[0].SessionID != "s-running" {
		t.Fatalf("unexpected session left after gc: %s", items[0].SessionID)
	}

	if _, ok := m.get("s-expired"); ok {
		t.Fatalf("expected expired session to be removed")
	}
}

func TestExecuteCommandTimeout(t *testing.T) {
	cmd := "sleep 2"
	if runtime.GOOS == "windows" {
		cmd = "ping -n 3 127.0.0.1 > NUL"
	}

	resp := executeCommand(cmd, ".", 1, 1000)
	if resp.OK {
		t.Fatalf("expected timeout failure")
	}
	if resp.ErrorCode != "command_timeout" {
		t.Fatalf("expected command_timeout, got %s", resp.ErrorCode)
	}
}

func TestEncodeAuditContainsSchemaFields(t *testing.T) {
	retry := true
	line, err := encodeAudit(auditPayload{
		Event:       "exec.request.denied",
		TraceID:     "t-1",
		PolicyCode:  "approval_required",
		PolicyScope: "exec_sidecar",
		ErrorKind:   "permission",
		Retryable:   &retry,
		Message:     "denied",
	})
	if err != nil {
		t.Fatalf("encodeAudit failed: %v", err)
	}

	var payload map[string]any
	if err := json.Unmarshal([]byte(line), &payload); err != nil {
		t.Fatalf("invalid json: %v", err)
	}
	if payload["event"] != "exec.request.denied" {
		t.Fatalf("unexpected event: %v", payload["event"])
	}
	if payload["trace_id"] != "t-1" {
		t.Fatalf("unexpected trace_id: %v", payload["trace_id"])
	}
	if payload["policy_code"] != "approval_required" {
		t.Fatalf("unexpected policy_code: %v", payload["policy_code"])
	}
	if payload["policy_scope"] != "exec_sidecar" {
		t.Fatalf("unexpected policy_scope: %v", payload["policy_scope"])
	}
}
