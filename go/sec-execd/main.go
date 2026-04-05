package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type execRequest struct {
	Command          string                 `json:"command"`
	WorkingDir       string                 `json:"working_dir"`
	TimeoutSeconds   int                    `json:"timeout_seconds"`
	SecurityContext  securityContextPayload `json:"security_context,omitempty"`
	PolicySnapshot   map[string]any         `json:"policy_snapshot,omitempty"`
	GatewayMeta      gatewayMetaPayload     `json:"gateway_meta,omitempty"`
	GatewaySignature string                 `json:"gateway_signature,omitempty"`
}

type securityContextPayload struct {
	TraceID       string `json:"trace_id,omitempty"`
	Channel       string `json:"channel,omitempty"`
	SenderID      string `json:"sender_id,omitempty"`
	ChatID        string `json:"chat_id,omitempty"`
	TenantID      string `json:"tenant_id,omitempty"`
	WorkspaceID   string `json:"workspace_id,omitempty"`
	AgentProfile  string `json:"agent_profile,omitempty"`
	Role          string `json:"role,omitempty"`
	TrustLevel    string `json:"trust_level,omitempty"`
	OriginSurface string `json:"origin_surface,omitempty"`
}

type gatewayMetaPayload struct {
	GatewayInstance    string `json:"gateway_instance,omitempty"`
	PolicySnapshotHash string `json:"policy_snapshot_hash,omitempty"`
	RequestHash        string `json:"request_hash,omitempty"`
}

type execResponse struct {
	OK           bool   `json:"ok"`
	Stdout       string `json:"stdout,omitempty"`
	Stderr       string `json:"stderr,omitempty"`
	ExitCode     int    `json:"exit_code,omitempty"`
	DurationMs   int64  `json:"duration_ms"`
	Truncated    bool   `json:"truncated,omitempty"`
	ErrorCode    string `json:"error_code,omitempty"`
	ErrorMessage string `json:"error_message,omitempty"`
}

type sessionStartRequest struct {
	Command          string                 `json:"command"`
	WorkingDir       string                 `json:"working_dir"`
	TimeoutSeconds   int                    `json:"timeout_seconds"`
	PTY              bool                   `json:"pty,omitempty"`
	SecurityContext  securityContextPayload `json:"security_context,omitempty"`
	PolicySnapshot   map[string]any         `json:"policy_snapshot,omitempty"`
	GatewayMeta      gatewayMetaPayload     `json:"gateway_meta,omitempty"`
	GatewaySignature string                 `json:"gateway_signature,omitempty"`
}

type sessionStartResponse struct {
	OK           bool   `json:"ok"`
	SessionID    string `json:"session_id,omitempty"`
	Status       string `json:"status,omitempty"`
	ErrorCode    string `json:"error_code,omitempty"`
	ErrorMessage string `json:"error_message,omitempty"`
}

type sessionStatusResponse struct {
	OK           bool   `json:"ok"`
	SessionID    string `json:"session_id"`
	Command      string `json:"command"`
	WorkingDir   string `json:"working_dir"`
	Status       string `json:"status"`
	PTY          bool   `json:"pty,omitempty"`
	PTYRows      int    `json:"pty_rows,omitempty"`
	PTYCols      int    `json:"pty_cols,omitempty"`
	Stdout       string `json:"stdout,omitempty"`
	Cursor       int    `json:"cursor,omitempty"`
	ExitCode     int    `json:"exit_code,omitempty"`
	Truncated    bool   `json:"truncated,omitempty"`
	ErrorCode    string `json:"error_code,omitempty"`
	ErrorMessage string `json:"error_message,omitempty"`
	StartedAtMs  int64  `json:"started_at_ms"`
	FinishedAtMs int64  `json:"finished_at_ms,omitempty"`
	TenantID     string `json:"tenant_id,omitempty"`
	WorkspaceID  string `json:"workspace_id,omitempty"`
	SessionOwner string `json:"session_owner,omitempty"`
	ExpiresAtMs  int64  `json:"expires_at_ms,omitempty"`
}

type sessionListResponse struct {
	OK       bool                    `json:"ok"`
	Sessions []sessionStatusResponse `json:"sessions"`
}

type sessionListRequest struct {
	SecurityContext  securityContextPayload `json:"security_context,omitempty"`
	PolicySnapshot   map[string]any         `json:"policy_snapshot,omitempty"`
	GatewayMeta      gatewayMetaPayload     `json:"gateway_meta,omitempty"`
	GatewaySignature string                 `json:"gateway_signature,omitempty"`
}

type sessionKillResponse struct {
	OK           bool   `json:"ok"`
	SessionID    string `json:"session_id"`
	Killed       bool   `json:"killed"`
	ErrorCode    string `json:"error_code,omitempty"`
	ErrorMessage string `json:"error_message,omitempty"`
}

type sessionReadResponse struct {
	OK           bool   `json:"ok"`
	SessionID    string `json:"session_id"`
	Status       string `json:"status,omitempty"`
	Cursor       int    `json:"cursor,omitempty"`
	NextCursor   int    `json:"next_cursor,omitempty"`
	Chunk        string `json:"chunk,omitempty"`
	Truncated    bool   `json:"truncated,omitempty"`
	ErrorCode    string `json:"error_code,omitempty"`
	ErrorMessage string `json:"error_message,omitempty"`
}

type sessionWriteRequest struct {
	Input            string                 `json:"input"`
	SecurityContext  securityContextPayload `json:"security_context,omitempty"`
	PolicySnapshot   map[string]any         `json:"policy_snapshot,omitempty"`
	GatewayMeta      gatewayMetaPayload     `json:"gateway_meta,omitempty"`
	GatewaySignature string                 `json:"gateway_signature,omitempty"`
}

type sessionWriteResponse struct {
	OK           bool   `json:"ok"`
	SessionID    string `json:"session_id"`
	Status       string `json:"status,omitempty"`
	WrittenBytes int    `json:"written_bytes,omitempty"`
	ErrorCode    string `json:"error_code,omitempty"`
	ErrorMessage string `json:"error_message,omitempty"`
}

type sessionSignalRequest struct {
	Signal           string                 `json:"signal"`
	SecurityContext  securityContextPayload `json:"security_context,omitempty"`
	PolicySnapshot   map[string]any         `json:"policy_snapshot,omitempty"`
	GatewayMeta      gatewayMetaPayload     `json:"gateway_meta,omitempty"`
	GatewaySignature string                 `json:"gateway_signature,omitempty"`
}

type sessionSignalResponse struct {
	OK           bool   `json:"ok"`
	SessionID    string `json:"session_id"`
	Status       string `json:"status,omitempty"`
	Signal       string `json:"signal,omitempty"`
	Delivered    bool   `json:"delivered,omitempty"`
	ErrorCode    string `json:"error_code,omitempty"`
	ErrorMessage string `json:"error_message,omitempty"`
}

type sessionResizeRequest struct {
	Rows             int                    `json:"rows"`
	Cols             int                    `json:"cols"`
	SecurityContext  securityContextPayload `json:"security_context,omitempty"`
	PolicySnapshot   map[string]any         `json:"policy_snapshot,omitempty"`
	GatewayMeta      gatewayMetaPayload     `json:"gateway_meta,omitempty"`
	GatewaySignature string                 `json:"gateway_signature,omitempty"`
}

type sessionReadRequest struct {
	Cursor           int                    `json:"cursor,omitempty"`
	MaxBytes         int                    `json:"max_bytes,omitempty"`
	SecurityContext  securityContextPayload `json:"security_context,omitempty"`
	PolicySnapshot   map[string]any         `json:"policy_snapshot,omitempty"`
	GatewayMeta      gatewayMetaPayload     `json:"gateway_meta,omitempty"`
	GatewaySignature string                 `json:"gateway_signature,omitempty"`
}

type sessionResizeResponse struct {
	OK           bool   `json:"ok"`
	SessionID    string `json:"session_id"`
	Status       string `json:"status,omitempty"`
	Rows         int    `json:"rows,omitempty"`
	Cols         int    `json:"cols,omitempty"`
	Applied      bool   `json:"applied,omitempty"`
	ErrorCode    string `json:"error_code,omitempty"`
	ErrorMessage string `json:"error_message,omitempty"`
}

type serverConfig struct {
	BindAddress         string
	Workspace           string
	MaxOutputBytes      int
	DefaultTimeoutSec   int
	MaxTimeoutSec       int
	SessionRetentionSec int
	RequireApproval     bool
	ApprovalToken       string
	ApprovalSecret      string
	DangerousCmdRegexp  []*regexp.Regexp
}

type sessionRecord struct {
	ID                 string
	Command            string
	WorkingDir         string
	StartedAtMs        int64
	FinishedAtMs       int64
	ExpiresAtMs        int64
	PTY                bool
	PTYRows            int
	PTYCols            int
	OwnerTraceID       string
	Channel            string
	SenderID           string
	ChatID             string
	TenantID           string
	WorkspaceID        string
	AgentProfile       string
	Role               string
	TrustLevel         string
	OriginSurface      string
	PolicySnapshotHash string
	RequestHash        string

	mu           sync.RWMutex
	Status       string
	Stdout       string
	ExitCode     int
	Truncated    bool
	ErrorCode    string
	ErrorMessage string

	cancel      context.CancelFunc
	cmd         *exec.Cmd
	stdin       io.WriteCloser
	killRequest bool
}

type sessionManager struct {
	mu          sync.RWMutex
	sessions    map[string]*sessionRecord
	nextID      uint64
	retentionMs int64
}

type auditPayload struct {
	Event              string         `json:"event"`
	TraceID            string         `json:"trace_id,omitempty"`
	PolicyCode         string         `json:"policy_code,omitempty"`
	PolicyScope        string         `json:"policy_scope,omitempty"`
	ErrorKind          string         `json:"error_kind,omitempty"`
	Retryable          *bool          `json:"retryable,omitempty"`
	Message            string         `json:"message,omitempty"`
	Capability         string         `json:"capability,omitempty"`
	ResourceScope      map[string]any `json:"resource_scope,omitempty"`
	PolicySnapshotHash string         `json:"policy_snapshot_hash,omitempty"`
	RequestHash        string         `json:"request_hash,omitempty"`
	SessionID          string         `json:"session_id,omitempty"`
	SessionOwner       string         `json:"session_owner,omitempty"`
	TenantID           string         `json:"tenant_id,omitempty"`
	WorkspaceID        string         `json:"workspace_id,omitempty"`
}

func main() {
	cfg, err := loadConfig()
	if err != nil {
		log.Fatalf("invalid config: %v", err)
	}

	mux := http.NewServeMux()
	sessions := newSessionManagerWithRetention(cfg.SessionRetentionSec)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/v1/exec", func(w http.ResponseWriter, r *http.Request) {
		handleExec(w, r, cfg)
	})
	mux.HandleFunc("/v1/sessions/start", func(w http.ResponseWriter, r *http.Request) {
		handleSessionStart(w, r, cfg, sessions)
	})
	mux.HandleFunc("/v1/sessions", func(w http.ResponseWriter, r *http.Request) {
		handleSessionList(w, r, cfg, sessions)
	})
	mux.HandleFunc("/v1/sessions/", func(w http.ResponseWriter, r *http.Request) {
		handleSessionRoutes(w, r, cfg, sessions)
	})

	// WriteTimeout must exceed MaxTimeoutSec so long-running commands can complete
	// before the server closes the response writer.
	writeTimeout := time.Duration(cfg.MaxTimeoutSec+30) * time.Second
	srv := &http.Server{
		Addr:              cfg.BindAddress,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      writeTimeout,
		IdleTimeout:       120 * time.Second,
	}

	log.Printf("sec-execd listening on %s (workspace=%s, require_approval=%v)", cfg.BindAddress, cfg.Workspace, cfg.RequireApproval)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

func loadConfig() (*serverConfig, error) {
	workspace := os.Getenv("SEC_EXECD_WORKSPACE")
	if workspace == "" {
		var err error
		workspace, err = os.Getwd()
		if err != nil {
			return nil, err
		}
	}
	workspace, err := filepath.Abs(workspace)
	if err != nil {
		return nil, err
	}

	cfg := &serverConfig{
		BindAddress:         getenv("SEC_EXECD_BIND", "127.0.0.1:4488"),
		Workspace:           workspace,
		MaxOutputBytes:      getenvInt("SEC_EXECD_MAX_OUTPUT_BYTES", 10000),
		DefaultTimeoutSec:   getenvInt("SEC_EXECD_DEFAULT_TIMEOUT_SEC", 60),
		MaxTimeoutSec:       getenvInt("SEC_EXECD_MAX_TIMEOUT_SEC", 120),
		SessionRetentionSec: getenvInt("SEC_EXECD_SESSION_RETENTION_SEC", 1800),
		RequireApproval:     getenvBool("SEC_EXECD_REQUIRE_APPROVAL", true),
		ApprovalToken:       os.Getenv("SEC_EXECD_APPROVAL_TOKEN"),
		ApprovalSecret:      os.Getenv("SEC_EXECD_APPROVAL_SECRET"),
		DangerousCmdRegexp: []*regexp.Regexp{
			regexp.MustCompile(`(?i)\brm\s+-[rf]{1,2}\b`),
			regexp.MustCompile(`(?i)\bdel\s+/[fq]\b`),
			regexp.MustCompile(`(?i)\brmdir\s+/s\b`),
			regexp.MustCompile(`(?i)\b(format|mkfs|diskpart)\b`),
			regexp.MustCompile(`(?i)\bdd\s+if=`),
			regexp.MustCompile(`(?i)\b(shutdown|reboot|poweroff)\b`),
		},
	}

	if cfg.MaxOutputBytes <= 0 {
		return nil, fmt.Errorf("SEC_EXECD_MAX_OUTPUT_BYTES must be > 0")
	}
	if cfg.DefaultTimeoutSec <= 0 || cfg.MaxTimeoutSec <= 0 {
		return nil, fmt.Errorf("timeout settings must be > 0")
	}
	if cfg.SessionRetentionSec <= 0 {
		return nil, fmt.Errorf("SEC_EXECD_SESSION_RETENTION_SEC must be > 0")
	}

	return cfg, nil
}

func validateSecurityContext(sec securityContextPayload) []string {
	missing := make([]string, 0, 9)
	for key, value := range map[string]string{
		"channel":        strings.TrimSpace(sec.Channel),
		"sender_id":      strings.TrimSpace(sec.SenderID),
		"chat_id":        strings.TrimSpace(sec.ChatID),
		"tenant_id":      strings.TrimSpace(sec.TenantID),
		"workspace_id":   strings.TrimSpace(sec.WorkspaceID),
		"agent_profile":  strings.TrimSpace(sec.AgentProfile),
		"role":           strings.TrimSpace(sec.Role),
		"trust_level":    strings.TrimSpace(sec.TrustLevel),
		"origin_surface": strings.TrimSpace(sec.OriginSurface),
	} {
		if value == "" {
			missing = append(missing, key)
		}
	}
	return missing
}

func sessionOwnerLabel(sec securityContextPayload) string {
	return strings.TrimSpace(sec.Channel) + "/" + strings.TrimSpace(sec.SenderID) + "/" + strings.TrimSpace(sec.ChatID)
}

func validateSessionEnvelope(
	traceID string,
	sec securityContextPayload,
	policySnapshot map[string]any,
	gateway gatewayMetaPayload,
	gatewaySignature string,
	payload any,
) (string, string) {
	if strings.TrimSpace(traceID) == "" {
		return "trace_required", "trace_id is required"
	}
	if missing := validateSecurityContext(sec); len(missing) > 0 {
		return "security_context_missing", "security_context missing: " + strings.Join(missing, ", ")
	}
	if strings.TrimSpace(gateway.PolicySnapshotHash) == "" {
		return "policy_snapshot_missing", "policy snapshot hash is required"
	}
	if strings.TrimSpace(gateway.RequestHash) == "" {
		return "request_hash_missing", "gateway request hash is required"
	}
	if strings.TrimSpace(gatewaySignature) == "" {
		return "gateway_signature_required", "gateway signature is required"
	}
	if strings.TrimSpace(os.Getenv("ZEN_CLAW_HMAC_MASTER_KEY")) == "" {
		return "", ""
	}
	canonical, err := canonicalGatewayPayload(payload)
	if err != nil {
		return "gateway_payload_invalid", "failed to canonicalize gateway payload"
	}
	canonicalHash := sha256.Sum256(canonical)
	if !strings.EqualFold(strings.TrimSpace(gateway.RequestHash), fmt.Sprintf("%x", canonicalHash[:])) {
		return "request_hash_mismatch", "gateway request hash does not match payload"
	}
	if !verifyGatewayPayloadSignature(canonical, gatewaySignature) {
		return "gateway_signature_invalid", "gateway signature does not match payload"
	}
	if strings.TrimSpace(stableMapHash(policySnapshot)) != "" && !strings.EqualFold(strings.TrimSpace(gateway.PolicySnapshotHash), stableMapHash(policySnapshot)) {
		return "policy_snapshot_hash_mismatch", "policy snapshot hash does not match payload"
	}
	return "", ""
}

func stableMapHash(payload map[string]any) string {
	if payload == nil {
		payload = map[string]any{}
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(raw)
	return fmt.Sprintf("%x", sum[:])
}

func canonicalGatewayPayload(payload any) ([]byte, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	var clone map[string]any
	if err := json.Unmarshal(raw, &clone); err != nil {
		return nil, err
	}
	delete(clone, "gateway_signature")
	if gatewayMeta, ok := clone["gateway_meta"].(map[string]any); ok {
		metaClone := map[string]any{}
		for key, value := range gatewayMeta {
			if key == "request_hash" {
				continue
			}
			metaClone[key] = value
		}
		clone["gateway_meta"] = metaClone
	}
	return json.Marshal(clone)
}

func gatewayPayloadMasterKey() []byte {
	sum := sha256.Sum256([]byte(strings.TrimSpace(os.Getenv("ZEN_CLAW_HMAC_MASTER_KEY"))))
	return sum[:]
}

func verifyGatewayPayloadSignature(canonical []byte, signature string) bool {
	mac := hmac.New(sha256.New, gatewayPayloadMasterKey())
	_, _ = mac.Write(canonical)
	expected := fmt.Sprintf("%x", mac.Sum(nil))
	return subtle.ConstantTimeCompare([]byte(strings.ToLower(strings.TrimSpace(signature))), []byte(expected)) == 1
}

func enforceSessionOwner(rec *sessionRecord, sec securityContextPayload) (string, string) {
	if strings.TrimSpace(rec.TenantID) != "" && !strings.EqualFold(strings.TrimSpace(rec.TenantID), strings.TrimSpace(sec.TenantID)) {
		return "session_owner_tenant_mismatch", "session belongs to a different tenant"
	}
	if strings.TrimSpace(rec.WorkspaceID) != "" && strings.TrimSpace(rec.WorkspaceID) != strings.TrimSpace(sec.WorkspaceID) {
		return "session_owner_workspace_mismatch", "session belongs to a different workspace"
	}
	if strings.TrimSpace(rec.Channel) != "" && !strings.EqualFold(strings.TrimSpace(rec.Channel), strings.TrimSpace(sec.Channel)) {
		return "session_owner_channel_mismatch", "session belongs to a different channel"
	}
	if strings.TrimSpace(rec.SenderID) != "" && strings.TrimSpace(rec.SenderID) != strings.TrimSpace(sec.SenderID) {
		return "session_owner_sender_mismatch", "session belongs to a different sender"
	}
	if strings.TrimSpace(rec.ChatID) != "" && strings.TrimSpace(rec.ChatID) != strings.TrimSpace(sec.ChatID) {
		return "session_owner_chat_mismatch", "session belongs to a different chat"
	}
	if rec.ExpiresAtMs > 0 && time.Now().UnixMilli() > rec.ExpiresAtMs {
		return "session_expired", "session has expired"
	}
	return "", ""
}

func handleExec(w http.ResponseWriter, r *http.Request, cfg *serverConfig) {
	traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
	if r.Method != http.MethodPost {
		logAudit(auditPayload{
			Event:      "exec.request.invalid_method",
			TraceID:    traceID,
			PolicyCode: "method_not_allowed",
			ErrorKind:  "parameter",
			Message:    "POST required",
		})
		writeJSON(w, http.StatusMethodNotAllowed, execResponse{
			OK:           false,
			ErrorCode:    "method_not_allowed",
			ErrorMessage: "POST required",
		})
		return
	}

	bodyBytes, err := readBodyBytes(r, 1<<20)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, execResponse{
			OK:           false,
			ErrorCode:    "invalid_body",
			ErrorMessage: err.Error(),
		})
		return
	}

	if !checkApproval(r, cfg, traceID, "POST", "/v1/exec", bodyBytes) {
		logAudit(auditPayload{
			Event:       "exec.request.denied",
			TraceID:     traceID,
			PolicyCode:  "approval_required",
			PolicyScope: "exec_sidecar",
			ErrorKind:   "permission",
			Message:     "missing or invalid approval token",
		})
		writeJSON(w, http.StatusForbidden, execResponse{
			OK:           false,
			ErrorCode:    "approval_required",
			ErrorMessage: "missing or invalid approval token",
		})
		return
	}

	var req execRequest
	if err := json.Unmarshal(bodyBytes, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, execResponse{
			OK:           false,
			ErrorCode:    "invalid_json",
			ErrorMessage: err.Error(),
		})
		return
	}
	if code, msg := validateSessionEnvelope(traceID, req.SecurityContext, req.PolicySnapshot, req.GatewayMeta, req.GatewaySignature, req); code != "" {
		logAudit(auditPayload{
			Event:       "exec.request.denied",
			TraceID:     traceID,
			PolicyCode:  code,
			PolicyScope: "gateway_envelope",
			ErrorKind:   "permission",
			Message:     msg,
		})
		writeJSON(w, http.StatusForbidden, execResponse{
			OK:           false,
			ErrorCode:    code,
			ErrorMessage: msg,
		})
		return
	}
	if strings.TrimSpace(req.Command) == "" {
		logAudit(auditPayload{
			Event:      "exec.request.invalid",
			TraceID:    traceID,
			PolicyCode: "command_required",
			ErrorKind:  "parameter",
			Message:    "command cannot be empty",
		})
		writeJSON(w, http.StatusBadRequest, execResponse{
			OK:           false,
			ErrorCode:    "command_required",
			ErrorMessage: "command cannot be empty",
		})
		return
	}
	if isDangerous(req.Command, cfg.DangerousCmdRegexp) {
		logAudit(auditPayload{
			Event:       "exec.request.denied",
			TraceID:     traceID,
			PolicyCode:  "dangerous_command",
			PolicyScope: "exec_sidecar",
			ErrorKind:   "permission",
			Message:     "blocked by dangerous command policy",
		})
		writeJSON(w, http.StatusForbidden, execResponse{
			OK:           false,
			ErrorCode:    "dangerous_command",
			ErrorMessage: "blocked by dangerous command policy",
		})
		return
	}

	workingDir := strings.TrimSpace(req.WorkingDir)
	if workingDir == "" {
		workingDir = cfg.Workspace
	}
	absWorkdir, err := filepath.Abs(workingDir)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, execResponse{
			OK:           false,
			ErrorCode:    "working_dir_invalid",
			ErrorMessage: err.Error(),
		})
		return
	}
	if !isSubPath(cfg.Workspace, absWorkdir) {
		logAudit(auditPayload{
			Event:       "exec.request.denied",
			TraceID:     traceID,
			PolicyCode:  "working_dir_outside_workspace",
			PolicyScope: "exec_sidecar",
			ErrorKind:   "permission",
			Message:     "working_dir is outside configured workspace",
		})
		writeJSON(w, http.StatusForbidden, execResponse{
			OK:           false,
			ErrorCode:    "working_dir_outside_workspace",
			ErrorMessage: "working_dir is outside configured workspace",
		})
		return
	}

	timeoutSec := req.TimeoutSeconds
	if timeoutSec <= 0 {
		timeoutSec = cfg.DefaultTimeoutSec
	}
	if timeoutSec > cfg.MaxTimeoutSec {
		timeoutSec = cfg.MaxTimeoutSec
	}

	started := time.Now()
	resp := executeCommand(req.Command, absWorkdir, timeoutSec, cfg.MaxOutputBytes)
	resp.DurationMs = time.Since(started).Milliseconds()
	if resp.OK {
		logAudit(auditPayload{
			Event:   "exec.request.allowed",
			TraceID: traceID,
			Message: "command executed",
		})
	} else {
		kind := "runtime"
		if resp.ErrorCode == "command_timeout" {
			kind = "retryable"
		}
		logAudit(auditPayload{
			Event:      "exec.request.failed",
			TraceID:    traceID,
			PolicyCode: resp.ErrorCode,
			ErrorKind:  kind,
			Message:    resp.ErrorMessage,
		})
	}

	status := http.StatusOK
	if !resp.OK {
		if resp.ErrorCode == "command_timeout" {
			status = http.StatusGatewayTimeout
		} else if strings.Contains(resp.ErrorCode, "spawn") {
			status = http.StatusInternalServerError
		} else if resp.ErrorCode == "nonzero_exit" {
			status = http.StatusBadRequest
		}
	}
	writeJSON(w, status, resp)
}

func handleSessionStart(w http.ResponseWriter, r *http.Request, cfg *serverConfig, sessions *sessionManager) {
	traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
	if r.Method != http.MethodPost {
		logAudit(auditPayload{
			Event:      "exec.session.invalid_method",
			TraceID:    traceID,
			PolicyCode: "method_not_allowed",
			ErrorKind:  "parameter",
			Message:    "POST required",
		})
		writeJSONAny(w, http.StatusMethodNotAllowed, sessionStartResponse{
			OK:           false,
			ErrorCode:    "method_not_allowed",
			ErrorMessage: "POST required",
		})
		return
	}

	bodyBytes, err := readBodyBytes(r, 1<<20)
	if err != nil {
		writeJSONAny(w, http.StatusBadRequest, sessionStartResponse{
			OK:           false,
			ErrorCode:    "invalid_body",
			ErrorMessage: err.Error(),
		})
		return
	}

	if !checkApproval(r, cfg, traceID, "POST", "/v1/sessions/start", bodyBytes) {
		logAudit(auditPayload{
			Event:       "exec.session.denied",
			TraceID:     traceID,
			PolicyCode:  "approval_required",
			PolicyScope: "exec_sidecar",
			ErrorKind:   "permission",
			Message:     "missing or invalid approval token",
		})
		writeJSONAny(w, http.StatusForbidden, sessionStartResponse{
			OK:           false,
			ErrorCode:    "approval_required",
			ErrorMessage: "missing or invalid approval token",
		})
		return
	}

	var req sessionStartRequest
	if err := json.Unmarshal(bodyBytes, &req); err != nil {
		writeJSONAny(w, http.StatusBadRequest, sessionStartResponse{
			OK:           false,
			ErrorCode:    "invalid_json",
			ErrorMessage: err.Error(),
		})
		return
	}
	if code, msg := validateSessionEnvelope(traceID, req.SecurityContext, req.PolicySnapshot, req.GatewayMeta, req.GatewaySignature, req); code != "" {
		logAudit(auditPayload{
			Event:              "exec.session.denied",
			TraceID:            traceID,
			PolicyCode:         code,
			PolicyScope:        "session_security",
			ErrorKind:          "permission",
			Message:            msg,
			Capability:         "sessions.spawn",
			PolicySnapshotHash: strings.TrimSpace(req.GatewayMeta.PolicySnapshotHash),
			RequestHash:        strings.TrimSpace(req.GatewayMeta.RequestHash),
			TenantID:           strings.TrimSpace(req.SecurityContext.TenantID),
			WorkspaceID:        strings.TrimSpace(req.SecurityContext.WorkspaceID),
		})
		writeJSONAny(w, http.StatusForbidden, sessionStartResponse{
			OK:           false,
			ErrorCode:    code,
			ErrorMessage: msg,
		})
		return
	}
	if strings.TrimSpace(req.Command) == "" {
		writeJSONAny(w, http.StatusBadRequest, sessionStartResponse{
			OK:           false,
			ErrorCode:    "command_required",
			ErrorMessage: "command cannot be empty",
		})
		return
	}
	if isDangerous(req.Command, cfg.DangerousCmdRegexp) {
		writeJSONAny(w, http.StatusForbidden, sessionStartResponse{
			OK:           false,
			ErrorCode:    "dangerous_command",
			ErrorMessage: "blocked by dangerous command policy",
		})
		return
	}
	absWorkdir, ok := resolveWorkingDir(cfg.Workspace, req.WorkingDir)
	if !ok {
		writeJSONAny(w, http.StatusForbidden, sessionStartResponse{
			OK:           false,
			ErrorCode:    "working_dir_outside_workspace",
			ErrorMessage: "working_dir is outside configured workspace",
		})
		return
	}

	timeoutSec := req.TimeoutSeconds
	if timeoutSec <= 0 {
		timeoutSec = cfg.DefaultTimeoutSec
	}
	if timeoutSec > cfg.MaxTimeoutSec {
		timeoutSec = cfg.MaxTimeoutSec
	}

	if req.PTY {
		if !isPTYSupported() {
			writeJSONAny(w, http.StatusBadRequest, sessionStartResponse{
				OK:           false,
				ErrorCode:    "pty_unsupported",
				ErrorMessage: "pty requested but not supported on this host",
			})
			return
		}
	}

	rec := sessions.start(req.Command, absWorkdir, timeoutSec, cfg.MaxOutputBytes, req.PTY, req.SecurityContext, req.GatewayMeta)
	logAudit(auditPayload{
		Event:              "exec.session.started",
		TraceID:            traceID,
		Message:            "session started",
		Capability:         "sessions.spawn",
		ResourceScope:      map[string]any{"working_dir": absWorkdir, "command": req.Command},
		PolicySnapshotHash: strings.TrimSpace(req.GatewayMeta.PolicySnapshotHash),
		RequestHash:        strings.TrimSpace(req.GatewayMeta.RequestHash),
		SessionID:          rec.ID,
		SessionOwner:       sessionOwnerLabel(req.SecurityContext),
		TenantID:           strings.TrimSpace(req.SecurityContext.TenantID),
		WorkspaceID:        strings.TrimSpace(req.SecurityContext.WorkspaceID),
	})
	writeJSONAny(w, http.StatusOK, sessionStartResponse{
		OK:        true,
		SessionID: rec.ID,
		Status:    "running",
	})
}

func handleSessionList(w http.ResponseWriter, r *http.Request, cfg *serverConfig, sessions *sessionManager) {
	if r.Method != http.MethodGet {
		traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
		logAudit(auditPayload{
			Event:      "exec.session.list.invalid_method",
			TraceID:    traceID,
			PolicyCode: "method_not_allowed",
			ErrorKind:  "parameter",
			Message:    "GET required",
		})
		writeJSONAny(w, http.StatusMethodNotAllowed, sessionListResponse{
			OK:       false,
			Sessions: []sessionStatusResponse{},
		})
		return
	}
	traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
	bodyBytes, err := readBodyBytes(r, 1<<20)
	if err != nil {
		writeJSONAny(w, http.StatusBadRequest, sessionListResponse{OK: false, Sessions: []sessionStatusResponse{}})
		return
	}
	if !checkApproval(r, cfg, traceID, "GET", "/v1/sessions", bodyBytes) {
		logAudit(auditPayload{
			Event:       "exec.session.list.denied",
			TraceID:     traceID,
			PolicyCode:  "approval_required",
			PolicyScope: "exec_sidecar",
			ErrorKind:   "permission",
			Message:     "missing or invalid approval token",
		})
		writeJSONAny(w, http.StatusForbidden, sessionListResponse{
			OK:       false,
			Sessions: []sessionStatusResponse{},
		})
		return
	}
	var req sessionListRequest
	if len(bodyBytes) > 0 {
		if err := json.Unmarshal(bodyBytes, &req); err != nil {
			writeJSONAny(w, http.StatusBadRequest, sessionListResponse{OK: false, Sessions: []sessionStatusResponse{}})
			return
		}
	}
	if code, msg := validateSessionEnvelope(traceID, req.SecurityContext, req.PolicySnapshot, req.GatewayMeta, req.GatewaySignature, req); code != "" {
		logAudit(auditPayload{
			Event:              "exec.session.list.denied",
			TraceID:            traceID,
			PolicyCode:         code,
			PolicyScope:        "session_security",
			ErrorKind:          "permission",
			Message:            msg,
			Capability:         "sessions.list",
			PolicySnapshotHash: strings.TrimSpace(req.GatewayMeta.PolicySnapshotHash),
			RequestHash:        strings.TrimSpace(req.GatewayMeta.RequestHash),
			TenantID:           strings.TrimSpace(req.SecurityContext.TenantID),
			WorkspaceID:        strings.TrimSpace(req.SecurityContext.WorkspaceID),
		})
		writeJSONAny(w, http.StatusForbidden, sessionListResponse{OK: false, Sessions: []sessionStatusResponse{}})
		return
	}
	logAudit(auditPayload{
		Event:              "exec.session.list.allowed",
		TraceID:            traceID,
		Message:            "sessions listed",
		Capability:         "sessions.list",
		PolicySnapshotHash: strings.TrimSpace(req.GatewayMeta.PolicySnapshotHash),
		RequestHash:        strings.TrimSpace(req.GatewayMeta.RequestHash),
		SessionOwner:       sessionOwnerLabel(req.SecurityContext),
		TenantID:           strings.TrimSpace(req.SecurityContext.TenantID),
		WorkspaceID:        strings.TrimSpace(req.SecurityContext.WorkspaceID),
	})
	writeJSONAny(w, http.StatusOK, sessionListResponse{
		OK:       true,
		Sessions: sessions.listForOwner(req.SecurityContext),
	})
}

func handleSessionRoutes(w http.ResponseWriter, r *http.Request, cfg *serverConfig, sessions *sessionManager) {
	path := strings.TrimPrefix(r.URL.Path, "/v1/sessions/")
	parts := strings.Split(strings.Trim(path, "/"), "/")
	if len(parts) == 0 || parts[0] == "" {
		traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
		logAudit(auditPayload{
			Event:      "exec.session.status.not_found",
			TraceID:    traceID,
			PolicyCode: "session_id_required",
			ErrorKind:  "parameter",
			Message:    "session id required",
		})
		writeJSONAny(w, http.StatusNotFound, sessionStartResponse{
			OK:           false,
			ErrorCode:    "session_not_found",
			ErrorMessage: "session id required",
		})
		return
	}
	sessionID := parts[0]
	if len(parts) == 1 && r.Method == http.MethodGet {
		traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
		bodyBytes, err := readBodyBytes(r, 1<<20)
		if err != nil {
			writeJSONAny(w, http.StatusBadRequest, sessionStartResponse{
				OK:           false,
				ErrorCode:    "invalid_body",
				ErrorMessage: err.Error(),
			})
			return
		}
		if !checkApproval(r, cfg, traceID, "GET", "/v1/sessions/"+sessionID, bodyBytes) {
			logAudit(auditPayload{
				Event:       "exec.session.status.denied",
				TraceID:     traceID,
				PolicyCode:  "approval_required",
				PolicyScope: "exec_sidecar",
				ErrorKind:   "permission",
				Message:     "missing or invalid approval token",
			})
			writeJSONAny(w, http.StatusForbidden, sessionStartResponse{
				OK:           false,
				ErrorCode:    "approval_required",
				ErrorMessage: "missing or invalid approval token",
			})
			return
		}
		var req sessionListRequest
		if len(bodyBytes) > 0 {
			if err := json.Unmarshal(bodyBytes, &req); err != nil {
				writeJSONAny(w, http.StatusBadRequest, sessionStartResponse{
					OK:           false,
					ErrorCode:    "invalid_json",
					ErrorMessage: err.Error(),
				})
				return
			}
		}
		if code, msg := validateSessionEnvelope(traceID, req.SecurityContext, req.PolicySnapshot, req.GatewayMeta, req.GatewaySignature, req); code != "" {
			writeJSONAny(w, http.StatusForbidden, sessionStartResponse{
				OK:           false,
				ErrorCode:    code,
				ErrorMessage: msg,
			})
			return
		}
		status, ok := sessions.getForOwner(sessionID, req.SecurityContext)
		if !ok {
			logAudit(auditPayload{
				Event:      "exec.session.status.not_found",
				TraceID:    traceID,
				PolicyCode: "session_not_found",
				ErrorKind:  "parameter",
				Message:    "session not found",
			})
			writeJSONAny(w, http.StatusNotFound, sessionStartResponse{
				OK:           false,
				ErrorCode:    "session_not_found",
				ErrorMessage: "session not found",
			})
			return
		}
		logAudit(auditPayload{
			Event:              "exec.session.status.allowed",
			TraceID:            traceID,
			Message:            "session status returned",
			Capability:         "sessions.status",
			PolicySnapshotHash: strings.TrimSpace(req.GatewayMeta.PolicySnapshotHash),
			RequestHash:        strings.TrimSpace(req.GatewayMeta.RequestHash),
			SessionID:          sessionID,
			SessionOwner:       sessionOwnerLabel(req.SecurityContext),
			TenantID:           strings.TrimSpace(req.SecurityContext.TenantID),
			WorkspaceID:        strings.TrimSpace(req.SecurityContext.WorkspaceID),
		})
		writeJSONAny(w, http.StatusOK, status)
		return
	}
	if len(parts) == 2 && parts[1] == "kill" && r.Method == http.MethodPost {
		traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
		bodyBytes, _ := readBodyBytes(r, 1<<20)
		if !checkApproval(r, cfg, traceID, "POST", "/v1/sessions/"+sessionID+"/kill", bodyBytes) {
			logAudit(auditPayload{
				Event:       "exec.session.kill.denied",
				TraceID:     traceID,
				PolicyCode:  "approval_required",
				PolicyScope: "exec_sidecar",
				ErrorKind:   "permission",
				Message:     "missing or invalid approval token",
			})
			writeJSONAny(w, http.StatusForbidden, sessionKillResponse{
				OK:           false,
				SessionID:    sessionID,
				Killed:       false,
				ErrorCode:    "approval_required",
				ErrorMessage: "missing or invalid approval token",
			})
			return
		}
		var req sessionListRequest
		if len(bodyBytes) > 0 {
			if err := json.Unmarshal(bodyBytes, &req); err != nil {
				writeJSONAny(w, http.StatusBadRequest, sessionKillResponse{
					OK:           false,
					SessionID:    sessionID,
					Killed:       false,
					ErrorCode:    "invalid_json",
					ErrorMessage: err.Error(),
				})
				return
			}
		}
		if code, msg := validateSessionEnvelope(traceID, req.SecurityContext, req.PolicySnapshot, req.GatewayMeta, req.GatewaySignature, req); code != "" {
			writeJSONAny(w, http.StatusForbidden, sessionKillResponse{
				OK:           false,
				SessionID:    sessionID,
				Killed:       false,
				ErrorCode:    code,
				ErrorMessage: msg,
			})
			return
		}
		killed, found := sessions.killForOwner(sessionID, req.SecurityContext)
		if !found {
			logAudit(auditPayload{
				Event:      "exec.session.kill.not_found",
				TraceID:    traceID,
				PolicyCode: "session_not_found",
				ErrorKind:  "parameter",
				Message:    "session not found",
			})
			writeJSONAny(w, http.StatusNotFound, sessionKillResponse{
				OK:           false,
				SessionID:    sessionID,
				Killed:       false,
				ErrorCode:    "session_not_found",
				ErrorMessage: "session not found",
			})
			return
		}
		ev := "exec.session.kill.noop"
		msg := "session not running"
		if killed {
			ev = "exec.session.kill.allowed"
			msg = "session killed"
		}
		logAudit(auditPayload{
			Event:              ev,
			TraceID:            traceID,
			Message:            msg,
			Capability:         "sessions.kill",
			PolicySnapshotHash: strings.TrimSpace(req.GatewayMeta.PolicySnapshotHash),
			RequestHash:        strings.TrimSpace(req.GatewayMeta.RequestHash),
			SessionID:          sessionID,
			SessionOwner:       sessionOwnerLabel(req.SecurityContext),
			TenantID:           strings.TrimSpace(req.SecurityContext.TenantID),
			WorkspaceID:        strings.TrimSpace(req.SecurityContext.WorkspaceID),
		})
		writeJSONAny(w, http.StatusOK, sessionKillResponse{
			OK:        true,
			SessionID: sessionID,
			Killed:    killed,
		})
		return
	}
	if len(parts) == 2 && parts[1] == "write" && r.Method == http.MethodPost {
		traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
		bodyBytes, err := readBodyBytes(r, 1<<20)
		if err != nil {
			writeJSONAny(w, http.StatusBadRequest, sessionWriteResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "invalid_body",
				ErrorMessage: err.Error(),
			})
			return
		}
		if !checkApproval(r, cfg, traceID, "POST", "/v1/sessions/"+sessionID+"/write", bodyBytes) {
			logAudit(auditPayload{
				Event:       "exec.session.write.denied",
				TraceID:     traceID,
				PolicyCode:  "approval_required",
				PolicyScope: "exec_sidecar",
				ErrorKind:   "permission",
				Message:     "missing or invalid approval token",
			})
			writeJSONAny(w, http.StatusForbidden, sessionWriteResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "approval_required",
				ErrorMessage: "missing or invalid approval token",
			})
			return
		}
		var req sessionWriteRequest
		if err := json.Unmarshal(bodyBytes, &req); err != nil {
			writeJSONAny(w, http.StatusBadRequest, sessionWriteResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "invalid_json",
				ErrorMessage: err.Error(),
			})
			return
		}
		if code, msg := validateSessionEnvelope(traceID, req.SecurityContext, req.PolicySnapshot, req.GatewayMeta, req.GatewaySignature, req); code != "" {
			writeJSONAny(w, http.StatusForbidden, sessionWriteResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    code,
				ErrorMessage: msg,
			})
			return
		}
		if req.Input == "" {
			writeJSONAny(w, http.StatusBadRequest, sessionWriteResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "input_required",
				ErrorMessage: "input cannot be empty",
			})
			return
		}
		if len(req.Input) > 8192 {
			writeJSONAny(w, http.StatusBadRequest, sessionWriteResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "input_too_large",
				ErrorMessage: "input exceeds 8192 bytes",
			})
			return
		}

		resp, found := sessions.writeForOwner(sessionID, []byte(req.Input), req.SecurityContext)
		if !found {
			logAudit(auditPayload{
				Event:      "exec.session.write.not_found",
				TraceID:    traceID,
				PolicyCode: "session_not_found",
				ErrorKind:  "parameter",
				Message:    "session not found",
			})
			writeJSONAny(w, http.StatusNotFound, sessionWriteResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "session_not_found",
				ErrorMessage: "session not found",
			})
			return
		}
		if !resp.OK {
			code := http.StatusBadRequest
			if resp.ErrorCode == "stdin_write_failed" {
				code = http.StatusInternalServerError
			}
			if strings.HasPrefix(resp.ErrorCode, "session_owner_") || resp.ErrorCode == "session_expired" {
				code = http.StatusForbidden
			}
			writeJSONAny(w, code, resp)
			return
		}
		logAudit(auditPayload{
			Event:              "exec.session.write.allowed",
			TraceID:            traceID,
			Message:            "session input written",
			Capability:         "sessions.write",
			PolicySnapshotHash: strings.TrimSpace(req.GatewayMeta.PolicySnapshotHash),
			RequestHash:        strings.TrimSpace(req.GatewayMeta.RequestHash),
			SessionID:          sessionID,
			SessionOwner:       sessionOwnerLabel(req.SecurityContext),
			TenantID:           strings.TrimSpace(req.SecurityContext.TenantID),
			WorkspaceID:        strings.TrimSpace(req.SecurityContext.WorkspaceID),
		})
		writeJSONAny(w, http.StatusOK, resp)
		return
	}
	if len(parts) == 2 && parts[1] == "signal" && r.Method == http.MethodPost {
		traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
		bodyBytes, err := readBodyBytes(r, 1<<20)
		if err != nil {
			writeJSONAny(w, http.StatusBadRequest, sessionSignalResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "invalid_body",
				ErrorMessage: err.Error(),
			})
			return
		}
		if !checkApproval(r, cfg, traceID, "POST", "/v1/sessions/"+sessionID+"/signal", bodyBytes) {
			logAudit(auditPayload{
				Event:       "exec.session.signal.denied",
				TraceID:     traceID,
				PolicyCode:  "approval_required",
				PolicyScope: "exec_sidecar",
				ErrorKind:   "permission",
				Message:     "missing or invalid approval token",
			})
			writeJSONAny(w, http.StatusForbidden, sessionSignalResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "approval_required",
				ErrorMessage: "missing or invalid approval token",
			})
			return
		}
		var req sessionSignalRequest
		if err := json.Unmarshal(bodyBytes, &req); err != nil {
			writeJSONAny(w, http.StatusBadRequest, sessionSignalResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "invalid_json",
				ErrorMessage: err.Error(),
			})
			return
		}
		if code, msg := validateSessionEnvelope(traceID, req.SecurityContext, req.PolicySnapshot, req.GatewayMeta, req.GatewaySignature, req); code != "" {
			writeJSONAny(w, http.StatusForbidden, sessionSignalResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    code,
				ErrorMessage: msg,
			})
			return
		}
		sig := strings.TrimSpace(strings.ToLower(req.Signal))
		if sig == "" {
			sig = "interrupt"
		}
		if sig != "interrupt" && sig != "terminate" && sig != "kill" {
			logAudit(auditPayload{
				Event:      "exec.session.signal.invalid",
				TraceID:    traceID,
				PolicyCode: "invalid_signal",
				ErrorKind:  "parameter",
				Message:    "signal must be one of interrupt|terminate|kill",
			})
			writeJSONAny(w, http.StatusBadRequest, sessionSignalResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "invalid_signal",
				ErrorMessage: "signal must be one of interrupt|terminate|kill",
			})
			return
		}
		resp, found := sessions.signalForOwner(sessionID, sig, req.SecurityContext)
		if !found {
			logAudit(auditPayload{
				Event:      "exec.session.signal.not_found",
				TraceID:    traceID,
				PolicyCode: "session_not_found",
				ErrorKind:  "parameter",
				Message:    "session not found",
			})
			writeJSONAny(w, http.StatusNotFound, sessionSignalResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "session_not_found",
				ErrorMessage: "session not found",
			})
			return
		}
		if !resp.OK {
			code := http.StatusBadRequest
			if resp.ErrorCode == "signal_delivery_failed" {
				code = http.StatusInternalServerError
			}
			if strings.HasPrefix(resp.ErrorCode, "session_owner_") || resp.ErrorCode == "session_expired" {
				code = http.StatusForbidden
			}
			writeJSONAny(w, code, resp)
			return
		}
		ev := "exec.session.signal.noop"
		msg := "signal accepted without delivery"
		if resp.Delivered {
			ev = "exec.session.signal.allowed"
			msg = "signal delivered"
		}
		logAudit(auditPayload{
			Event:              ev,
			TraceID:            traceID,
			Message:            msg,
			Capability:         "sessions.signal",
			PolicySnapshotHash: strings.TrimSpace(req.GatewayMeta.PolicySnapshotHash),
			RequestHash:        strings.TrimSpace(req.GatewayMeta.RequestHash),
			SessionID:          sessionID,
			SessionOwner:       sessionOwnerLabel(req.SecurityContext),
			TenantID:           strings.TrimSpace(req.SecurityContext.TenantID),
			WorkspaceID:        strings.TrimSpace(req.SecurityContext.WorkspaceID),
		})
		writeJSONAny(w, http.StatusOK, resp)
		return
	}
	if len(parts) == 2 && parts[1] == "resize" && r.Method == http.MethodPost {
		traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
		bodyBytes, err := readBodyBytes(r, 1<<20)
		if err != nil {
			writeJSONAny(w, http.StatusBadRequest, sessionResizeResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "invalid_body",
				ErrorMessage: err.Error(),
			})
			return
		}
		if !checkApproval(r, cfg, traceID, "POST", "/v1/sessions/"+sessionID+"/resize", bodyBytes) {
			logAudit(auditPayload{
				Event:       "exec.session.resize.denied",
				TraceID:     traceID,
				PolicyCode:  "approval_required",
				PolicyScope: "exec_sidecar",
				ErrorKind:   "permission",
				Message:     "missing or invalid approval token",
			})
			writeJSONAny(w, http.StatusForbidden, sessionResizeResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "approval_required",
				ErrorMessage: "missing or invalid approval token",
			})
			return
		}
		var req sessionResizeRequest
		if err := json.Unmarshal(bodyBytes, &req); err != nil {
			writeJSONAny(w, http.StatusBadRequest, sessionResizeResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "invalid_json",
				ErrorMessage: err.Error(),
			})
			return
		}
		if code, msg := validateSessionEnvelope(traceID, req.SecurityContext, req.PolicySnapshot, req.GatewayMeta, req.GatewaySignature, req); code != "" {
			writeJSONAny(w, http.StatusForbidden, sessionResizeResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    code,
				ErrorMessage: msg,
			})
			return
		}
		if req.Rows <= 0 || req.Cols <= 0 || req.Rows > 1000 || req.Cols > 1000 {
			logAudit(auditPayload{
				Event:      "exec.session.resize.invalid",
				TraceID:    traceID,
				PolicyCode: "invalid_dimensions",
				ErrorKind:  "parameter",
				Message:    "rows/cols must be in range 1..1000",
			})
			writeJSONAny(w, http.StatusBadRequest, sessionResizeResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "invalid_dimensions",
				ErrorMessage: "rows/cols must be in range 1..1000",
			})
			return
		}
		resp, found := sessions.resizeForOwner(sessionID, req.Rows, req.Cols, req.SecurityContext)
		if !found {
			logAudit(auditPayload{
				Event:      "exec.session.resize.not_found",
				TraceID:    traceID,
				PolicyCode: "session_not_found",
				ErrorKind:  "parameter",
				Message:    "session not found",
			})
			writeJSONAny(w, http.StatusNotFound, sessionResizeResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "session_not_found",
				ErrorMessage: "session not found",
			})
			return
		}
		if !resp.OK {
			code := http.StatusBadRequest
			if strings.HasPrefix(resp.ErrorCode, "session_owner_") || resp.ErrorCode == "session_expired" {
				code = http.StatusForbidden
			}
			writeJSONAny(w, code, resp)
			return
		}
		ev := "exec.session.resize.noop"
		msg := "resize accepted without applying"
		if resp.Applied {
			ev = "exec.session.resize.allowed"
			msg = "resize applied"
		}
		logAudit(auditPayload{
			Event:              ev,
			TraceID:            traceID,
			Message:            msg,
			Capability:         "sessions.resize",
			PolicySnapshotHash: strings.TrimSpace(req.GatewayMeta.PolicySnapshotHash),
			RequestHash:        strings.TrimSpace(req.GatewayMeta.RequestHash),
			SessionID:          sessionID,
			SessionOwner:       sessionOwnerLabel(req.SecurityContext),
			TenantID:           strings.TrimSpace(req.SecurityContext.TenantID),
			WorkspaceID:        strings.TrimSpace(req.SecurityContext.WorkspaceID),
		})
		writeJSONAny(w, http.StatusOK, resp)
		return
	}
	if len(parts) == 2 && parts[1] == "read" && r.Method == http.MethodGet {
		traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
		bodyBytes, err := readBodyBytes(r, 1<<20)
		if err != nil {
			writeJSONAny(w, http.StatusBadRequest, sessionReadResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "invalid_body",
				ErrorMessage: err.Error(),
			})
			return
		}
		if !checkApproval(r, cfg, traceID, "GET", "/v1/sessions/"+sessionID+"/read", bodyBytes) {
			logAudit(auditPayload{
				Event:       "exec.session.read.denied",
				TraceID:     traceID,
				PolicyCode:  "approval_required",
				PolicyScope: "exec_sidecar",
				ErrorKind:   "permission",
				Message:     "missing or invalid approval token",
			})
			writeJSONAny(w, http.StatusForbidden, sessionReadResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "approval_required",
				ErrorMessage: "missing or invalid approval token",
			})
			return
		}
		var req sessionReadRequest
		if len(bodyBytes) > 0 {
			if err := json.Unmarshal(bodyBytes, &req); err != nil {
				writeJSONAny(w, http.StatusBadRequest, sessionReadResponse{
					OK:           false,
					SessionID:    sessionID,
					ErrorCode:    "invalid_json",
					ErrorMessage: err.Error(),
				})
				return
			}
		}
		if code, msg := validateSessionEnvelope(traceID, req.SecurityContext, req.PolicySnapshot, req.GatewayMeta, req.GatewaySignature, req); code != "" {
			writeJSONAny(w, http.StatusForbidden, sessionReadResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    code,
				ErrorMessage: msg,
			})
			return
		}

		cursor, ok := parseNonNegativeIntBounded(strconv.Itoa(req.Cursor), 0, cfg.MaxOutputBytes)
		if !ok {
			logAudit(auditPayload{
				Event:      "exec.session.read.invalid_query",
				TraceID:    traceID,
				PolicyCode: "invalid_cursor",
				ErrorKind:  "parameter",
				Message:    "cursor must be a non-negative integer",
			})
			writeJSONAny(w, http.StatusBadRequest, sessionReadResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "invalid_cursor",
				ErrorMessage: "cursor must be a non-negative integer",
			})
			return
		}
		maxBytes, ok := parseNonNegativeIntBounded(strconv.Itoa(req.MaxBytes), 2048, cfg.MaxOutputBytes)
		if !ok || maxBytes <= 0 {
			logAudit(auditPayload{
				Event:      "exec.session.read.invalid_query",
				TraceID:    traceID,
				PolicyCode: "invalid_max_bytes",
				ErrorKind:  "parameter",
				Message:    "max_bytes must be a positive integer",
			})
			writeJSONAny(w, http.StatusBadRequest, sessionReadResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "invalid_max_bytes",
				ErrorMessage: "max_bytes must be a positive integer",
			})
			return
		}

		resp, found := sessions.readForOwner(sessionID, cursor, maxBytes, req.SecurityContext)
		if !found {
			logAudit(auditPayload{
				Event:      "exec.session.read.not_found",
				TraceID:    traceID,
				PolicyCode: "session_not_found",
				ErrorKind:  "parameter",
				Message:    "session not found",
			})
			writeJSONAny(w, http.StatusNotFound, sessionReadResponse{
				OK:           false,
				SessionID:    sessionID,
				ErrorCode:    "session_not_found",
				ErrorMessage: "session not found",
			})
			return
		}
		if !resp.OK {
			code := http.StatusBadRequest
			if strings.HasPrefix(resp.ErrorCode, "session_owner_") || resp.ErrorCode == "session_expired" {
				code = http.StatusForbidden
			}
			writeJSONAny(w, code, resp)
			return
		}

		logAudit(auditPayload{
			Event:              "exec.session.read.allowed",
			TraceID:            traceID,
			Message:            "session output chunk returned",
			Capability:         "sessions.read",
			PolicySnapshotHash: strings.TrimSpace(req.GatewayMeta.PolicySnapshotHash),
			RequestHash:        strings.TrimSpace(req.GatewayMeta.RequestHash),
			SessionID:          sessionID,
			SessionOwner:       sessionOwnerLabel(req.SecurityContext),
			TenantID:           strings.TrimSpace(req.SecurityContext.TenantID),
			WorkspaceID:        strings.TrimSpace(req.SecurityContext.WorkspaceID),
		})
		writeJSONAny(w, http.StatusOK, resp)
		return
	}
	traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
	logAudit(auditPayload{
		Event:      "exec.session.route.not_found",
		TraceID:    traceID,
		PolicyCode: "route_not_found",
		ErrorKind:  "parameter",
		Message:    "unsupported session route",
	})
	writeJSONAny(w, http.StatusNotFound, sessionStartResponse{
		OK:           false,
		ErrorCode:    "route_not_found",
		ErrorMessage: "unsupported session route",
	})
}

func resolveWorkingDir(workspace, requestWorkingDir string) (string, bool) {
	workingDir := strings.TrimSpace(requestWorkingDir)
	if workingDir == "" {
		workingDir = workspace
	}
	absWorkdir, err := filepath.Abs(workingDir)
	if err != nil {
		return "", false
	}
	if !isSubPath(workspace, absWorkdir) {
		return "", false
	}
	return absWorkdir, true
}

func checkApproval(r *http.Request, cfg *serverConfig, traceID, method, path string, bodyBytes []byte) bool {
	if !cfg.RequireApproval {
		return true
	}
	if cfg.ApprovalSecret != "" {
		return checkHMACApproval(r, cfg.ApprovalSecret, traceID, method, path, bodyBytes)
	}
	headerToken := r.Header.Get("X-Approval-Token")
	return cfg.ApprovalToken != "" && headerToken != "" &&
		subtle.ConstantTimeCompare([]byte(headerToken), []byte(cfg.ApprovalToken)) == 1
}

func logAudit(p auditPayload) {
	line, err := encodeAudit(p)
	if err != nil {
		log.Printf("audit_encode_error: %v", err)
		return
	}
	// Write audit JSON lines to stdout without log prefix so downstream tooling can parse reliably.
	_, _ = fmt.Fprintln(os.Stdout, line)
}

func encodeAudit(p auditPayload) (string, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(p); err != nil {
		return "", err
	}
	return strings.TrimSpace(buf.String()), nil
}

func executeCommand(command, workingDir string, timeoutSec, maxOutput int) execResponse {
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeoutSec)*time.Second)
	defer cancel()

	cmd := buildExecCommand(ctx, command)
	cmd.Dir = workingDir

	output, err := cmd.CombinedOutput()
	stdout, truncated := truncateBytes(string(output), maxOutput)

	if ctx.Err() == context.DeadlineExceeded {
		return execResponse{
			OK:           false,
			Stdout:       stdout,
			Truncated:    truncated,
			ErrorCode:    "command_timeout",
			ErrorMessage: fmt.Sprintf("command exceeded timeout (%ds)", timeoutSec),
		}
	}

	if err != nil {
		exitCode := 1
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			exitCode = exitErr.ExitCode()
		}
		return execResponse{
			OK:           false,
			Stdout:       stdout,
			ExitCode:     exitCode,
			Truncated:    truncated,
			ErrorCode:    "nonzero_exit",
			ErrorMessage: err.Error(),
		}
	}

	return execResponse{
		OK:        true,
		Stdout:    stdout,
		ExitCode:  0,
		Truncated: truncated,
	}
}

func buildExecCommand(ctx context.Context, command string) *exec.Cmd {
	if runtime.GOOS == "windows" {
		return exec.CommandContext(ctx, "cmd", "/C", command)
	}
	return exec.CommandContext(ctx, "sh", "-c", command)
}

func isDangerous(cmd string, patterns []*regexp.Regexp) bool {
	for _, p := range patterns {
		if p.MatchString(cmd) {
			return true
		}
	}
	return false
}

func isSubPath(root, target string) bool {
	rootClean := filepath.Clean(root)
	targetClean := filepath.Clean(target)
	rel, err := filepath.Rel(rootClean, targetClean)
	if err != nil {
		return false
	}
	return rel == "." || (!strings.HasPrefix(rel, "..") && !strings.Contains(rel, string(filepath.Separator)+".."))
}

func truncateBytes(text string, maxBytes int) (string, bool) {
	if len(text) <= maxBytes {
		return text, false
	}
	return text[:maxBytes], true
}

func writeJSON(w http.ResponseWriter, status int, body execResponse) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeJSONAny(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
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

func parseNonNegativeIntBounded(raw string, defaultValue, maxValue int) (int, bool) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return defaultValue, true
	}
	parsed, err := strconv.Atoi(raw)
	if err != nil || parsed < 0 {
		return 0, false
	}
	if parsed > maxValue {
		return maxValue, true
	}
	return parsed, true
}

func checkHMACApproval(r *http.Request, secret, traceID, method, path string, bodyBytes []byte) bool {
	traceID = strings.TrimSpace(traceID)
	if traceID == "" {
		return false
	}
	tsStr := strings.TrimSpace(r.Header.Get("X-Gateway-Timestamp"))
	if tsStr == "" {
		tsStr = strings.TrimSpace(r.Header.Get("X-Approval-Timestamp"))
	}
	nonce := strings.TrimSpace(r.Header.Get("X-Gateway-Nonce"))
	requestHash := strings.TrimSpace(r.Header.Get("X-Gateway-Request-Hash"))
	gatewayInstance := strings.TrimSpace(r.Header.Get("X-Gateway-Instance"))
	policySnapshotHash := strings.TrimSpace(r.Header.Get("X-Policy-Snapshot-Hash"))
	sigHex := strings.TrimSpace(r.Header.Get("X-Gateway-Signature"))
	if sigHex == "" {
		sigHex = strings.TrimSpace(r.Header.Get("X-Approval-Signature"))
	}
	if tsStr == "" || sigHex == "" {
		return false
	}
	ts, err := strconv.ParseInt(tsStr, 10, 64)
	if err != nil {
		return false
	}
	now := time.Now().Unix()
	if ts < now-120 || ts > now+120 {
		return false
	}
	bodyHash := sha256.Sum256(bodyBytes)
	computedHash := hex.EncodeToString(bodyHash[:])
	if requestHash != "" && !strings.EqualFold(requestHash, computedHash) {
		return false
	}
	var canonical string
	if nonce != "" || gatewayInstance != "" || policySnapshotHash != "" {
		canonical = strings.Join(
			[]string{
				traceID,
				tsStr,
				strings.TrimSpace(nonce),
				strings.ToUpper(strings.TrimSpace(method)),
				strings.TrimSpace(path),
				computedHash,
				strings.TrimSpace(gatewayInstance),
				strings.TrimSpace(policySnapshotHash),
			},
			"\n",
		)
	} else {
		canonical = strings.Join(
			[]string{
				traceID,
				tsStr,
				strings.ToUpper(strings.TrimSpace(method)),
				strings.TrimSpace(path),
				computedHash,
			},
			"\n",
		)
	}
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(canonical))
	expected := hex.EncodeToString(mac.Sum(nil))
	return hmac.Equal([]byte(expected), []byte(sigHex))
}

func newSessionManager() *sessionManager {
	return newSessionManagerWithRetention(1800)
}

func newSessionManagerWithRetention(retentionSec int) *sessionManager {
	retentionMs := int64(retentionSec) * 1000
	if retentionMs <= 0 {
		retentionMs = 1800 * 1000
	}
	return &sessionManager{
		sessions:    make(map[string]*sessionRecord),
		retentionMs: retentionMs,
	}
}

func (m *sessionManager) start(
	command, workingDir string,
	timeoutSec, maxOutput int,
	pty bool,
	sec securityContextPayload,
	gateway gatewayMetaPayload,
) *sessionRecord {
	id := fmt.Sprintf("s-%d", atomic.AddUint64(&m.nextID, 1))
	now := time.Now().UnixMilli()
	rec := &sessionRecord{
		ID:                 id,
		Command:            command,
		WorkingDir:         workingDir,
		StartedAtMs:        now,
		ExpiresAtMs:        now + int64(timeoutSec)*1000,
		Status:             "running",
		PTY:                pty,
		OwnerTraceID:       strings.TrimSpace(sec.TraceID),
		Channel:            strings.TrimSpace(sec.Channel),
		SenderID:           strings.TrimSpace(sec.SenderID),
		ChatID:             strings.TrimSpace(sec.ChatID),
		TenantID:           strings.TrimSpace(sec.TenantID),
		WorkspaceID:        strings.TrimSpace(sec.WorkspaceID),
		AgentProfile:       strings.TrimSpace(sec.AgentProfile),
		Role:               strings.TrimSpace(sec.Role),
		TrustLevel:         strings.TrimSpace(sec.TrustLevel),
		OriginSurface:      strings.TrimSpace(sec.OriginSurface),
		PolicySnapshotHash: strings.TrimSpace(gateway.PolicySnapshotHash),
		RequestHash:        strings.TrimSpace(gateway.RequestHash),
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeoutSec)*time.Second)
	rec.cancel = cancel

	m.mu.Lock()
	m.gcExpiredLocked(now)
	m.sessions[id] = rec
	m.mu.Unlock()

	go func() {
		defer cancel()
		cmd, err := buildSessionCommand(ctx, command, pty)
		if err != nil {
			rec.mu.Lock()
			rec.Status = "failed"
			rec.ErrorCode = "pty_unsupported"
			rec.ErrorMessage = err.Error()
			rec.FinishedAtMs = time.Now().UnixMilli()
			rec.mu.Unlock()
			return
		}
		cmd.Dir = workingDir
		stdinPipe, err := cmd.StdinPipe()
		if err != nil {
			rec.mu.Lock()
			rec.Status = "failed"
			rec.ErrorCode = "command_spawn_failed"
			rec.ErrorMessage = err.Error()
			rec.FinishedAtMs = time.Now().UnixMilli()
			rec.mu.Unlock()
			return
		}
		stdoutPipe, err := cmd.StdoutPipe()
		if err != nil {
			rec.mu.Lock()
			rec.Status = "failed"
			rec.ErrorCode = "command_spawn_failed"
			rec.ErrorMessage = err.Error()
			rec.FinishedAtMs = time.Now().UnixMilli()
			rec.mu.Unlock()
			return
		}
		stderrPipe, err := cmd.StderrPipe()
		if err != nil {
			rec.mu.Lock()
			rec.Status = "failed"
			rec.ErrorCode = "command_spawn_failed"
			rec.ErrorMessage = err.Error()
			rec.FinishedAtMs = time.Now().UnixMilli()
			rec.mu.Unlock()
			return
		}
		if err := cmd.Start(); err != nil {
			rec.mu.Lock()
			if rec.killRequest || ctx.Err() == context.Canceled {
				rec.Status = "killed"
				rec.ErrorCode = "command_killed"
				rec.ErrorMessage = "session killed"
			} else {
				rec.Status = "failed"
				rec.ErrorCode = "command_spawn_failed"
				rec.ErrorMessage = err.Error()
			}
			rec.FinishedAtMs = time.Now().UnixMilli()
			rec.mu.Unlock()
			return
		}
		rec.mu.Lock()
		rec.cmd = cmd
		rec.stdin = stdinPipe
		rec.mu.Unlock()

		var wg sync.WaitGroup
		wg.Add(2)
		go func() {
			defer wg.Done()
			streamToSessionOutput(rec, stdoutPipe, maxOutput)
		}()
		go func() {
			defer wg.Done()
			streamToSessionOutput(rec, stderrPipe, maxOutput)
		}()

		err = cmd.Wait()
		wg.Wait()

		rec.mu.Lock()
		if rec.stdin != nil {
			_ = rec.stdin.Close()
			rec.stdin = nil
		}
		defer rec.mu.Unlock()
		rec.FinishedAtMs = time.Now().UnixMilli()

		if ctx.Err() == context.DeadlineExceeded {
			rec.Status = "timeout"
			rec.ErrorCode = "command_timeout"
			rec.ErrorMessage = fmt.Sprintf("command exceeded timeout (%ds)", timeoutSec)
			return
		}
		if rec.killRequest {
			rec.Status = "killed"
			rec.ErrorCode = "command_killed"
			rec.ErrorMessage = "session killed"
			return
		}
		if err != nil {
			rec.Status = "failed"
			rec.ErrorCode = "nonzero_exit"
			rec.ErrorMessage = err.Error()
			exitCode := 1
			var exitErr *exec.ExitError
			if errors.As(err, &exitErr) {
				exitCode = exitErr.ExitCode()
			}
			rec.ExitCode = exitCode
			return
		}
		rec.Status = "done"
		rec.ExitCode = 0
	}()
	return rec
}

func (m *sessionManager) get(id string) (sessionStatusResponse, bool) {
	m.mu.Lock()
	m.gcExpiredLocked(time.Now().UnixMilli())
	rec, ok := m.sessions[id]
	m.mu.Unlock()
	if !ok {
		return sessionStatusResponse{}, false
	}
	return rec.snapshot(), true
}

func (m *sessionManager) getForOwner(id string, sec securityContextPayload) (sessionStatusResponse, bool) {
	m.mu.Lock()
	m.gcExpiredLocked(time.Now().UnixMilli())
	rec, ok := m.sessions[id]
	m.mu.Unlock()
	if !ok {
		return sessionStatusResponse{}, false
	}
	if code, _ := enforceSessionOwner(rec, sec); code != "" {
		return sessionStatusResponse{}, false
	}
	return rec.snapshot(), true
}

func (m *sessionManager) list() []sessionStatusResponse {
	m.mu.Lock()
	m.gcExpiredLocked(time.Now().UnixMilli())
	defer m.mu.Unlock()
	out := make([]sessionStatusResponse, 0, len(m.sessions))
	for _, rec := range m.sessions {
		out = append(out, rec.snapshot())
	}
	return out
}

func (m *sessionManager) listForOwner(sec securityContextPayload) []sessionStatusResponse {
	m.mu.Lock()
	m.gcExpiredLocked(time.Now().UnixMilli())
	defer m.mu.Unlock()
	out := make([]sessionStatusResponse, 0, len(m.sessions))
	for _, rec := range m.sessions {
		if code, _ := enforceSessionOwner(rec, sec); code != "" {
			continue
		}
		out = append(out, rec.snapshot())
	}
	return out
}

func (m *sessionManager) kill(id string) (bool, bool) {
	m.mu.RLock()
	rec, ok := m.sessions[id]
	m.mu.RUnlock()
	if !ok {
		return false, false
	}
	rec.mu.Lock()
	defer rec.mu.Unlock()
	if rec.Status != "running" {
		return false, true
	}
	rec.killRequest = true
	if rec.stdin != nil {
		_ = rec.stdin.Close()
		rec.stdin = nil
	}
	rec.cancel()
	return true, true
}

func (m *sessionManager) killForOwner(id string, sec securityContextPayload) (bool, bool) {
	m.mu.RLock()
	rec, ok := m.sessions[id]
	m.mu.RUnlock()
	if !ok {
		return false, false
	}
	if code, _ := enforceSessionOwner(rec, sec); code != "" {
		return false, false
	}
	return m.kill(id)
}

func (m *sessionManager) write(id string, input []byte) (sessionWriteResponse, bool) {
	m.mu.Lock()
	m.gcExpiredLocked(time.Now().UnixMilli())
	rec, ok := m.sessions[id]
	m.mu.Unlock()
	if !ok {
		return sessionWriteResponse{}, false
	}
	status, written, code, msg := rec.writeInput(input)
	if code != "" {
		return sessionWriteResponse{
			OK:           false,
			SessionID:    id,
			Status:       status,
			WrittenBytes: written,
			ErrorCode:    code,
			ErrorMessage: msg,
		}, true
	}
	return sessionWriteResponse{
		OK:           true,
		SessionID:    id,
		Status:       status,
		WrittenBytes: written,
	}, true
}

func (m *sessionManager) writeForOwner(id string, input []byte, sec securityContextPayload) (sessionWriteResponse, bool) {
	m.mu.Lock()
	m.gcExpiredLocked(time.Now().UnixMilli())
	rec, ok := m.sessions[id]
	m.mu.Unlock()
	if !ok {
		return sessionWriteResponse{}, false
	}
	if code, msg := enforceSessionOwner(rec, sec); code != "" {
		return sessionWriteResponse{
			OK:           false,
			SessionID:    id,
			ErrorCode:    code,
			ErrorMessage: msg,
		}, true
	}
	return m.write(id, input)
}

func (m *sessionManager) read(id string, cursor, maxBytes int) (sessionReadResponse, bool) {
	m.mu.Lock()
	m.gcExpiredLocked(time.Now().UnixMilli())
	rec, ok := m.sessions[id]
	m.mu.Unlock()
	if !ok {
		return sessionReadResponse{}, false
	}
	chunk, start, next, status, truncated := rec.readChunk(cursor, maxBytes)
	return sessionReadResponse{
		OK:         true,
		SessionID:  id,
		Status:     status,
		Cursor:     start,
		NextCursor: next,
		Chunk:      chunk,
		Truncated:  truncated,
	}, true
}

func (m *sessionManager) readForOwner(id string, cursor, maxBytes int, sec securityContextPayload) (sessionReadResponse, bool) {
	m.mu.Lock()
	m.gcExpiredLocked(time.Now().UnixMilli())
	rec, ok := m.sessions[id]
	m.mu.Unlock()
	if !ok {
		return sessionReadResponse{}, false
	}
	if code, msg := enforceSessionOwner(rec, sec); code != "" {
		return sessionReadResponse{
			OK:           false,
			SessionID:    id,
			ErrorCode:    code,
			ErrorMessage: msg,
		}, true
	}
	return m.read(id, cursor, maxBytes)
}

func (m *sessionManager) signal(id, sig string) (sessionSignalResponse, bool) {
	m.mu.Lock()
	m.gcExpiredLocked(time.Now().UnixMilli())
	rec, ok := m.sessions[id]
	m.mu.Unlock()
	if !ok {
		return sessionSignalResponse{}, false
	}
	status, delivered, code, msg := rec.signal(sig)
	if code != "" {
		return sessionSignalResponse{
			OK:           false,
			SessionID:    id,
			Status:       status,
			Signal:       sig,
			Delivered:    delivered,
			ErrorCode:    code,
			ErrorMessage: msg,
		}, true
	}
	return sessionSignalResponse{
		OK:        true,
		SessionID: id,
		Status:    status,
		Signal:    sig,
		Delivered: delivered,
	}, true
}

func (m *sessionManager) signalForOwner(id, sig string, sec securityContextPayload) (sessionSignalResponse, bool) {
	m.mu.Lock()
	m.gcExpiredLocked(time.Now().UnixMilli())
	rec, ok := m.sessions[id]
	m.mu.Unlock()
	if !ok {
		return sessionSignalResponse{}, false
	}
	if code, msg := enforceSessionOwner(rec, sec); code != "" {
		return sessionSignalResponse{
			OK:           false,
			SessionID:    id,
			Signal:       sig,
			ErrorCode:    code,
			ErrorMessage: msg,
		}, true
	}
	return m.signal(id, sig)
}

func (m *sessionManager) resize(id string, rows, cols int) (sessionResizeResponse, bool) {
	m.mu.Lock()
	m.gcExpiredLocked(time.Now().UnixMilli())
	rec, ok := m.sessions[id]
	m.mu.Unlock()
	if !ok {
		return sessionResizeResponse{}, false
	}
	status, applied, code, msg := rec.resizePTY(rows, cols)
	if code != "" {
		return sessionResizeResponse{
			OK:           false,
			SessionID:    id,
			Status:       status,
			Rows:         rows,
			Cols:         cols,
			Applied:      applied,
			ErrorCode:    code,
			ErrorMessage: msg,
		}, true
	}
	return sessionResizeResponse{
		OK:        true,
		SessionID: id,
		Status:    status,
		Rows:      rows,
		Cols:      cols,
		Applied:   applied,
	}, true
}

func (m *sessionManager) resizeForOwner(id string, rows, cols int, sec securityContextPayload) (sessionResizeResponse, bool) {
	m.mu.Lock()
	m.gcExpiredLocked(time.Now().UnixMilli())
	rec, ok := m.sessions[id]
	m.mu.Unlock()
	if !ok {
		return sessionResizeResponse{}, false
	}
	if code, msg := enforceSessionOwner(rec, sec); code != "" {
		return sessionResizeResponse{
			OK:           false,
			SessionID:    id,
			Rows:         rows,
			Cols:         cols,
			ErrorCode:    code,
			ErrorMessage: msg,
		}, true
	}
	return m.resize(id, rows, cols)
}

func (m *sessionManager) gcExpiredLocked(nowMs int64) {
	if m.retentionMs <= 0 {
		return
	}
	for id, rec := range m.sessions {
		if !rec.isTerminal() {
			continue
		}
		finishedAt := rec.finishedAt()
		if finishedAt <= 0 {
			continue
		}
		if nowMs-finishedAt > m.retentionMs {
			delete(m.sessions, id)
		}
	}
}

func (r *sessionRecord) snapshot() sessionStatusResponse {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return sessionStatusResponse{
		OK:           true,
		SessionID:    r.ID,
		Command:      r.Command,
		WorkingDir:   r.WorkingDir,
		Status:       r.Status,
		PTY:          r.PTY,
		PTYRows:      r.PTYRows,
		PTYCols:      r.PTYCols,
		Stdout:       r.Stdout,
		Cursor:       len(r.Stdout),
		ExitCode:     r.ExitCode,
		Truncated:    r.Truncated,
		ErrorCode:    r.ErrorCode,
		ErrorMessage: r.ErrorMessage,
		StartedAtMs:  r.StartedAtMs,
		FinishedAtMs: r.FinishedAtMs,
		TenantID:     r.TenantID,
		WorkspaceID:  r.WorkspaceID,
		SessionOwner: sessionOwnerLabel(securityContextPayload{
			Channel:  r.Channel,
			SenderID: r.SenderID,
			ChatID:   r.ChatID,
		}),
		ExpiresAtMs: r.ExpiresAtMs,
	}
}

var lookupPath = exec.LookPath

func isPTYSupported() bool {
	if runtime.GOOS == "windows" {
		return false
	}
	_, err := lookupPath("script")
	return err == nil
}

func buildSessionCommand(ctx context.Context, command string, pty bool) (*exec.Cmd, error) {
	if !pty {
		return buildExecCommand(ctx, command), nil
	}
	if runtime.GOOS == "windows" {
		return nil, fmt.Errorf("pty requested but windows sidecar does not support pseudo terminals")
	}
	if _, err := lookupPath("script"); err != nil {
		return nil, fmt.Errorf("pty requested but 'script' command is unavailable")
	}
	// Use util-linux script to allocate a pseudo-terminal for interactive-ish commands.
	return exec.CommandContext(ctx, "script", "-q", "-c", command, "/dev/null"), nil
}

func streamToSessionOutput(rec *sessionRecord, reader io.Reader, maxOutput int) {
	buf := make([]byte, 2048)
	for {
		n, err := reader.Read(buf)
		if n > 0 {
			rec.appendOutput(buf[:n], maxOutput)
		}
		if err != nil {
			return
		}
	}
}

func (r *sessionRecord) appendOutput(chunk []byte, maxOutput int) {
	if len(chunk) == 0 {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.Truncated {
		return
	}
	remaining := maxOutput - len(r.Stdout)
	if remaining <= 0 {
		r.Truncated = true
		return
	}
	if len(chunk) > remaining {
		r.Stdout += string(chunk[:remaining])
		r.Truncated = true
		return
	}
	r.Stdout += string(chunk)
}

func (r *sessionRecord) readChunk(cursor, maxBytes int) (string, int, int, string, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	total := len(r.Stdout)
	if cursor < 0 {
		cursor = 0
	}
	if cursor > total {
		cursor = total
	}
	if maxBytes <= 0 {
		maxBytes = 1024
	}
	end := cursor + maxBytes
	if end > total {
		end = total
	}
	return r.Stdout[cursor:end], cursor, end, r.Status, r.Truncated
}

func (r *sessionRecord) writeInput(input []byte) (status string, written int, code, msg string) {
	r.mu.RLock()
	status = r.Status
	stdin := r.stdin
	r.mu.RUnlock()
	if status != "running" {
		return status, 0, "session_not_running", "session is not running"
	}
	if stdin == nil {
		return status, 0, "stdin_unavailable", "session stdin is unavailable"
	}
	n, err := stdin.Write(input)
	if err != nil {
		return status, n, "stdin_write_failed", err.Error()
	}
	return status, n, "", ""
}

func (r *sessionRecord) signal(sig string) (status string, delivered bool, code, msg string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	status = r.Status
	if status != "running" {
		return status, false, "session_not_running", "session is not running"
	}
	if r.cmd == nil || r.cmd.Process == nil {
		return status, false, "process_unavailable", "session process is unavailable"
	}
	switch sig {
	case "interrupt":
		if err := r.cmd.Process.Signal(os.Interrupt); err != nil {
			return status, false, "signal_delivery_failed", err.Error()
		}
	case "terminate", "kill":
		r.killRequest = true
		if err := r.cmd.Process.Kill(); err != nil {
			return status, false, "signal_delivery_failed", err.Error()
		}
	default:
		return status, false, "invalid_signal", "signal must be one of interrupt|terminate|kill"
	}
	return status, true, "", ""
}

func (r *sessionRecord) resizePTY(rows, cols int) (status string, applied bool, code, msg string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	status = r.Status
	if status != "running" {
		return status, false, "session_not_running", "session is not running"
	}
	if !r.PTY {
		return status, false, "pty_required", "session was not started with pty=true"
	}
	if rows <= 0 || cols <= 0 {
		return status, false, "invalid_dimensions", "rows/cols must be positive"
	}
	r.PTYRows = rows
	r.PTYCols = cols
	return status, true, "", ""
}

func (r *sessionRecord) isTerminal() bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	switch r.Status {
	case "done", "failed", "timeout", "killed":
		return true
	default:
		return false
	}
}

func (r *sessionRecord) finishedAt() int64 {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.FinishedAtMs
}

func getenv(name, defaultValue string) string {
	v := strings.TrimSpace(os.Getenv(name))
	if v == "" {
		return defaultValue
	}
	return v
}

func getenvInt(name string, defaultValue int) int {
	v := strings.TrimSpace(os.Getenv(name))
	if v == "" {
		return defaultValue
	}
	parsed, err := strconv.Atoi(v)
	if err != nil {
		return defaultValue
	}
	return parsed
}

func getenvBool(name string, defaultValue bool) bool {
	v := strings.TrimSpace(strings.ToLower(os.Getenv(name)))
	if v == "" {
		return defaultValue
	}
	return v == "1" || v == "true" || v == "yes" || v == "on"
}
