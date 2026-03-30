# Completed Work

## Usage

- This file stores summary-level records for completed work that is no longer active.
- Keep entries concise and structured.
- Move long retrospectives or raw historical logs into `tasks/archive/`.

## Index

### 2026-03

- `GitHub Actions — CI Token-Reduction Redesign`
- `Daily Assistant — Gate Phase 5 Gate 3 Entry And Telemetry Consolidation`
- `Daily Assistant — Gate Phase 6-8 Safety Valve / Gate 2-3 Contract / Skill Intake Governance`
- `Daily Assistant — Word Alignment And Phase 5 Crystallized Normalization`
- `Repo Baseline — Full Ruff Closure`
- `Daily Assistant — Gate Phase 4 Classifier Integration`
- `Daily Assistant — Gate Phase 3 Minimal Safety Valve`
- `Daily Assistant — Gate Phase 2 Candidate-Oriented Declarative Output`
- `Daily Assistant — Gate-Based Routing Rollout Plan And Phase 1 Scaffolding`
- `Intent Router — Modular Facade Refactor`
- `Gateway And Dashboard — Local Health Endpoint Alignment`
- `Config Wizard — Root-Cause Guardrails And Step-by-Step Setup`
- `GitHub Actions — Core Tests PowerShell Shard Logging Fix`
- `CI Regression Fix — RAG Retention, Notebook Repair, Timezone Recovery`
- `README — Developer-First Bilingual Rewrite`
- `Docs — Project Overview`
- `Dashboard/API — Ops Apply Capability Metadata`
- `Recovery Framework — BUG-009 Closure`
- `Recovery Framework — Minimal Unified Entry`
- `Bug Closure — Local Acceptance Realignment`
- `Skill Registry — Inventory Baseline`
- `Business Skills — First Real Runtime Slice`
- `Traceability Dashboard`
- `Workflow Webhooks`
- `Dynamic Model Routing — Minimum Viable Layer`
- `Dynamic Model Routing — Fallback Model`
- `Local-first RAG / ingest / cron — Minimum Loop`
 - `RAG Pipeline — Shared Contract Baseline`
 - `RAG Pipeline — Metadata Filter Contract`
 - `RAG Pipeline — Store Abstraction Baseline`
 - `RAG Pipeline — Tenant-Aware Routing Baseline`
 - `RAG Pipeline — Tenant Policy Guardrail`
 - `RAG Pipeline — Document Delete Lifecycle`
 - `RAG Pipeline — Document Inventory Baseline`
 - `RAG Pipeline — Document Metadata Inventory`
 - `RAG Pipeline — Notebook Cleanup Baseline`
 - `RAG Pipeline — Retention Baseline`
 - `RAG Pipeline — Backend Config Surface`
 - `RAG Pipeline — Notebook Backend Policy Baseline`
 - `RAG Pipeline — Tenant Backend Policy Baseline`
 - `RAG Pipeline — Tenant Policy Dashboard/API Surface`
 - `RAG Pipeline — Notebook Policy Dashboard/API Surface`
 - `RAG Pipeline — Policy Audit Baseline`
 - `RAG Pipeline — Backend Tuning Knobs Baseline`
 - `RAG Pipeline — Backend Diagnostics Surface`
 - `Dashboard — Model Usage Surface`
 - `Dashboard/API — Agent Inventory Surface`
 - `Dashboard/API — Skill Readiness Surface`
 - `Dashboard/API — Skill Action Audit Surface`
- `Dashboard/API — Agent Planning Control Surface`
- `Dashboard/API — Agent Model Control Surface`
- `Dashboard/API — Agent Routing Control Surface`
- `Dashboard/API — Agent Route Preview Surface`
- `Dashboard/API — Agent Profile Detail Surface`
- `Dashboard/API — Agent Apply Acknowledgement Surface`
- `Agent Orchestration — Profile Skill Preload Surface`
- `Agent Orchestration — Profile CRUD Surface`
- `Business Skills — Tenant-Aware Retrieval Contract`
- `Business Skills — Channel Output Contract`
- `Business Skills — Semantic Compliance Review Baseline`
- `Business Skills — Content Prompt/Response Shaping Baseline`
- `Dashboard/API — Skill Batch Preflight Workflow`
- `Dashboard/API — Skill Batch State Workflow`
- `RAG Pipeline — Notebook Retention Policy Baseline`
- `Dashboard/API — Model Routing CSV + Pagination Surface`
- `Dashboard/API — Skill Detail Drill-down Surface`
- `Dashboard/API — Skill Lifecycle Install Surface`
- `Dashboard/API — Skill Export Restore Surface`
- `Dashboard/API — Skill Package Visibility Surface`
- `Dashboard/API — Skill Package Cleanup Surface`
- `Dashboard/API — Knowledge Policy History Surface`
- `Dashboard/API — Model Routing Time Filter Surface`
- `Dashboard/API — Operations Summary Export Surface`
- `Dashboard/API — Operations Summary Control-Plane Report`
- `Dashboard/API — Operations Summary Apply Surface`
- `Dashboard/API — Operations Summary Scoped Apply Surface`
- `Dashboard/API — Channel Runtime Control Surface`
- `Dashboard/API — Skill Activity History Surface`
- `Dashboard/API — Knowledge Activity History Surface`
- `Dashboard/API — Model Routing Event Surface`
- `Dashboard/API — Skill Preflight Surface`
- `Dashboard/API — Skill Preflight Detail Surface`
- `Dashboard/API — Skill Export Surface`
- `Dashboard — Operations Summary Surface`

## 2026-03-23

### GitHub Actions — CI Token-Reduction Redesign

- What changed:
  - extracted workflow-owned test selection into `scripts/ci_select_tests.py` so `core`, `memory-recall`, and channel suites now resolve from repo code instead of inline Python embedded in `.github/workflows/ci.yml`
  - added `scripts/preflight.ps1` as the local low-token acceptance entry with:
    - `quick` mode aligned to PR gates
    - `full` mode adding build sanity
    - `nightly` mode chaining preflight + existing selftest/perf scripts
    - targeted `-Suite`, `-ShardIndex`, and `-TotalShards` reproduction support
  - redesigned `.github/workflows/ci.yml` into clearer layers:
    - `lint-and-guards`
    - sharded `core-tests`
    - `memory-recall`
    - `channel-matrix`
    - `summary`
  - aligned `.github/workflows/nightly-integration.yml` to call `scripts/preflight.ps1 -Mode full -SkipLocGate` before the existing selftest/perf steps
  - expanded workflow regression coverage so future edits must keep repo-scripted suite selection and the new preflight alignment
- Key files touched:
  - `.github/workflows/ci.yml`
  - `.github/workflows/nightly-integration.yml`
  - `scripts/ci_select_tests.py`
  - `scripts/preflight.ps1`
  - `tests/test_ci_workflow_loc_gate.py`
  - `tests/test_ci_workflow_structure.py`
  - `tests/test_nightly_integration_workflow.py`
  - `tasks/todo.md`
- Verification evidence:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .` passed
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q tests/test_ci_workflow_loc_gate.py tests/test_ci_workflow_structure.py tests/test_nightly_integration_workflow.py` passed: `4 passed in 0.26s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe scripts/ci_select_tests.py --suite core --total-shards 4 --shard-index 0 --format args` passed and returned the shard-0 test list
  - `pwsh .\scripts\preflight.ps1 -Suite channel-slack -SkipLocGate` passed: `4 passed in 0.59s`
  - `pwsh .\scripts\preflight.ps1 -Suite core -ShardIndex 0 -TotalShards 4 -SkipLocGate` passed: `362 passed, 12 skipped in 52.21s`
  - `pwsh .\scripts\preflight.ps1` passed, including:
    - `ruff check .`
    - `pwsh scripts/loc_report.ps1`
    - `1422 passed, 41 skipped in 142.20s` for the core suite
    - `11 passed` for `memory-recall`
    - `6 passed` / `4 passed` / `7 passed` / `10 passed` across channel suites
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q` passed: `1460 passed, 41 skipped in 159.64s (0:02:39)`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pip install -U build` required escalated network access and completed with `Successfully installed build-1.4.2`
  - standard isolated `E:\nano-claw-public\.venv\Scripts\python.exe -m build` timed out while trying to reach package indexes for build-environment dependencies
  - after installing local build backend support, fallback `E:\nano-claw-public\.venv\Scripts\python.exe -m build --no-isolation` passed: `Successfully built zen_claw_ai-0.1.3.post5.tar.gz and zen_claw_ai-0.1.3.post5-py3-none-any.whl`
- Follow-up impact:
  - CI failures now map much more directly to a repo script plus a local reproduction command, which should reduce repeated token burn from re-explaining suite boundaries and shard selection on every CI failure
  - CI pytest runs now also use explicit `.pytest_tmp/*` basetemp paths, which avoids the Windows hosted-runner numbered-temp cleanup path that had started surfacing as post-success exit-code failures

### Daily Assistant — Gate Phase 5 Gate 3 Entry And Telemetry Consolidation

- What changed:
  - added Phase 5 rollout/instruction docs for explicit Gate 3 entry and gate-stage telemetry consolidation:
    - `docs/design/Daily_Assistant_Gate_Phase5_Rollout_20260328.md`
    - `docs/design/Daily_Assistant_Gate_Phase5_Instruction_20260328.md`
  - added an explicit Gate 3 helper in the agent loop so `Gate 2 -> unclassified` no longer falls through implicitly into the normal planning path
  - tightened Gate 2 / Gate 3 boundaries so:
    - `request_clarification` exits early
    - `confirm_candidate` stays on constrained path
    - `select_skill` stays on skill-biased path
    - only `unclassified` enters Gate 3
  - extended intent-router telemetry with stage semantics:
    - `routing_stage`
    - `entered_gate3`
  - added focused tests covering Gate 3 entry and the new trace fields
- Key files touched:
  - `docs/design/Daily_Assistant_Gate_Phase5_Rollout_20260328.md`
  - `docs/design/Daily_Assistant_Gate_Phase5_Instruction_20260328.md`
  - `zen_claw/agent/loop.py`
  - `tests/test_intent_router_classifier.py`
  - `tests/test_dashboard_intent_router_trace.py`
  - `tasks/todo.md`
- Verification evidence:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check E:\nano-claw-public\zen_claw\agent\loop.py E:\nano-claw-public\tests\test_intent_router_classifier.py E:\nano-claw-public\tests\test_dashboard_intent_router_trace.py` passed
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q E:\nano-claw-public\tests\test_intent_router_classifier.py E:\nano-claw-public\tests\test_dashboard_intent_router_trace.py E:\nano-claw-public\tests\test_intent_declarative.py` passed: `36 passed in 8.53s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q` passed: `1450 passed, 41 skipped in 118.45s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .` still fails on pre-existing repository-wide lint debt unrelated to this slice
- Follow-up impact:
  - later phases can now treat Gate 3 as a real runtime surface instead of an implicit compatibility fallthrough, which reduces coupling between future crystallized/native migration work and the free-planning path

### Daily Assistant — Gate Phase 4 Classifier Integration

- What changed:
  - added Phase 4 rollout/instruction docs for Gate 2 classifier integration:
    - `docs/design/Daily_Assistant_Gate_Phase4_Rollout_20260328.md`
    - `docs/design/Daily_Assistant_Gate_Phase4_Instruction_20260328.md`
  - added a dedicated Gate 2 result shape and a lightweight classifier module on top of `provider.chat(...)`, including JSON-only parsing and safe degradation to `unclassified`
  - wired delegated Safety Valve routes through Gate 2 before telemetry append and before the normal planning path
  - added real loop-side handling for:
    - `confirm_candidate`
    - `select_skill`
    - `request_clarification`
    - `unclassified`
  - upgraded declarative delegate results to preserve contract metadata so Gate 2 can confirm the candidate and fall back to the constrained path when appropriate
  - expanded trace and classifier tests to cover Gate 2 parsing, clarification early return, unclassified fallback, and real `arbitration_result` telemetry
- Key files touched:
  - `docs/design/Daily_Assistant_Gate_Phase4_Rollout_20260328.md`
  - `docs/design/Daily_Assistant_Gate_Phase4_Instruction_20260328.md`
  - `zen_claw/agent/intent_router_contracts.py`
  - `zen_claw/agent/intent_router_classifier.py`
  - `zen_claw/agent/intent_router_declarative.py`
  - `zen_claw/agent/loop.py`
  - `tests/test_intent_router_classifier.py`
  - `tests/test_dashboard_intent_router_trace.py`
  - `tasks/todo.md`
- Verification evidence:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check E:\nano-claw-public\zen_claw\agent\intent_router_contracts.py E:\nano-claw-public\zen_claw\agent\intent_router_classifier.py E:\nano-claw-public\zen_claw\agent\intent_router_declarative.py E:\nano-claw-public\zen_claw\agent\loop.py E:\nano-claw-public\tests\test_intent_router_classifier.py E:\nano-claw-public\tests\test_dashboard_intent_router_trace.py` passed
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q E:\nano-claw-public\tests\test_intent_router_classifier.py E:\nano-claw-public\tests\test_dashboard_intent_router_trace.py E:\nano-claw-public\tests\test_intent_declarative.py` passed: `34 passed in 6.94s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q` passed: `1448 passed, 41 skipped in 118.37s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .` still fails on pre-existing repository-wide lint debt unrelated to this slice
- Follow-up impact:
  - the next phase can focus on a cleaner Gate 3 split and richer arbitration semantics instead of continuing to overload compatibility-only delegate behavior

### Daily Assistant — Gate Phase 3 Minimal Safety Valve

- What changed:
  - added Phase 3 rollout/instruction docs for the first runtime Safety Valve slice:
    - `docs/design/Daily_Assistant_Gate_Phase3_Rollout_20260328.md`
    - `docs/design/Daily_Assistant_Gate_Phase3_Instruction_20260328.md`
  - upgraded intent routing with session-derived control context so the router can apply a correction hard rule before rule execution and derive minimal history-based control signals for declarative routing
  - upgraded declarative direct routing with a minimal Safety Valve that delegates low-confidence matches through compatible `needs_constrained_replan` semantics instead of executing tools immediately
  - upgraded loop session metadata persistence so direct rule executions now update session-local route history and last-rule state, enabling history confidence weighting and correction bypass
  - expanded declarative and dashboard-trace tests to cover low-confidence delegation, correction override, session metadata persistence, and real Safety Valve telemetry rows
- Key files touched:
  - `docs/design/Daily_Assistant_Gate_Phase3_Rollout_20260328.md`
  - `docs/design/Daily_Assistant_Gate_Phase3_Instruction_20260328.md`
  - `zen_claw/agent/intent_router.py`
  - `zen_claw/agent/intent_router_declarative.py`
  - `zen_claw/agent/loop.py`
  - `tests/test_intent_declarative.py`
  - `tests/test_dashboard_intent_router_trace.py`
  - `tasks/todo.md`
- Verification evidence:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check E:\nano-claw-public\zen_claw\agent\intent_router.py E:\nano-claw-public\zen_claw\agent\intent_router_declarative.py E:\nano-claw-public\zen_claw\agent\loop.py E:\nano-claw-public\tests\test_intent_declarative.py E:\nano-claw-public\tests\test_dashboard_intent_router_trace.py` passed
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q E:\nano-claw-public\tests\test_intent_declarative.py E:\nano-claw-public\tests\test_dashboard_intent_router_trace.py E:\nano-claw-public\tests\test_intent_router_recovery_framework.py` passed: `40 passed in 6.26s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q` passed: `1444 passed, 41 skipped in 119.12s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .` still fails on pre-existing repository-wide lint debt unrelated to this slice
- Follow-up impact:
  - the next phase can add a real Gate 2 classifier on top of actual delegate outcomes and delegate reasons instead of introducing classification on a purely document-level design

### Daily Assistant — Gate Phase 2 Candidate-Oriented Declarative Output

- What changed:
  - added Phase 2 rollout/instruction docs for candidate-oriented Gate 1 evolution:
    - `docs/design/Daily_Assistant_Gate_Phase2_Rollout_20260328.md`
    - `docs/design/Daily_Assistant_Gate_Phase2_Instruction_20260328.md`
  - upgraded declarative routing so all declarative result paths now emit real `route_candidate` metadata with stable `match/source/raw_confidence` values
  - extended intent-router recovery helpers to accept and preserve Gate-migration metadata, preparing native routes for later phases without changing their current behavior
  - expanded declarative and recovery tests to cover candidate metadata propagation while preserving existing route-status semantics
- Key files touched:
  - `docs/design/Daily_Assistant_Gate_Phase2_Rollout_20260328.md`
  - `docs/design/Daily_Assistant_Gate_Phase2_Instruction_20260328.md`
  - `zen_claw/agent/intent_router_declarative.py`
  - `zen_claw/agent/intent_router_recovery.py`
  - `tests/test_intent_declarative.py`
  - `tests/test_intent_router_recovery_framework.py`
  - `tasks/todo.md`
- Verification evidence:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check E:\nano-claw-public\zen_claw\agent\intent_router_declarative.py E:\nano-claw-public\zen_claw\agent\intent_router_recovery.py E:\nano-claw-public\tests\test_intent_declarative.py E:\nano-claw-public\tests\test_dashboard_intent_router_trace.py E:\nano-claw-public\tests\test_intent_router_recovery_framework.py` passed
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q E:\nano-claw-public\tests\test_intent_declarative.py E:\nano-claw-public\tests\test_dashboard_intent_router_trace.py E:\nano-claw-public\tests\test_intent_router_recovery_framework.py` passed: `37 passed in 5.23s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q` passed: `1441 passed, 41 skipped in 112.85s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .` still fails on pre-existing repository-wide lint debt unrelated to this slice
- Follow-up impact:
  - the next phase can add Safety Valve logic on top of real declarative candidate outputs instead of placeholder metadata fields

### Daily Assistant — Gate-Based Routing Rollout Plan And Phase 1 Scaffolding

- What changed:
  - rewrote [daily-assistant-architecture.md](E:\nano-claw-public\docs\daily-assistant-architecture.md) into the Gate-based target architecture and added two execution-facing docs:
    - [Daily_Assistant_Gate_Rollout_Plan_20260328.md](E:\nano-claw-public\docs\design\Daily_Assistant_Gate_Rollout_Plan_20260328.md)
    - [Daily_Assistant_Gate_Phase1_Instruction_20260328.md](E:\nano-claw-public\docs\design\Daily_Assistant_Gate_Phase1_Instruction_20260328.md)
  - implemented the first compatibility-safe runtime slice by extending `IntentRouteResult` with additive Gate-migration metadata and persisting those fields into `intent_router` dashboard telemetry without changing current routing behavior
  - added focused regression coverage for the new telemetry fields while preserving existing direct-route trace expectations
- Key files touched:
  - `docs/daily-assistant-architecture.md`
  - `docs/design/Daily_Assistant_Gate_Rollout_Plan_20260328.md`
  - `docs/design/Daily_Assistant_Gate_Phase1_Instruction_20260328.md`
  - `zen_claw/agent/intent_router_contracts.py`
  - `zen_claw/agent/loop.py`
  - `tests/test_dashboard_intent_router_trace.py`
  - `tasks/todo.md`
- Verification evidence:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check E:\nano-claw-public\zen_claw\agent\intent_router_contracts.py E:\nano-claw-public\zen_claw\agent\loop.py E:\nano-claw-public\tests\test_dashboard_intent_router_trace.py` passed
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q E:\nano-claw-public\tests\test_dashboard_intent_router_trace.py E:\nano-claw-public\tests\test_intent_router_recovery_framework.py` passed: `12 passed in 4.73s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q` passed: `1440 passed, 41 skipped in 106.08s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .` still fails on pre-existing repository-wide lint debt unrelated to this rollout slice
- Follow-up impact:
  - the next implementation slice can introduce candidate-oriented rule output and Safety Valve plumbing on top of stable result/telemetry scaffolding instead of starting from a closed route-status model

### Intent Router — Modular Facade Refactor

- What changed:
  - split `zen_claw/agent/intent_router.py` into a stable facade plus focused internal modules for contracts, specs, parsers, shared helpers, recovery builders, and intent handlers while preserving the public `IntentRouter.route(...)` protocol and route result semantics
  - replaced the old branch-heavy `route(...)` body with a fixed registry-driven dispatch order for `exec`, `weather`, `exchange`, `fixed_site`, `time`, and `direct_contracts`
  - added structural regression coverage for registry order, parser output, and shared fallback helpers, and tightened parser compatibility so weather polite-prefix cleanup and exec intent detection preserve prior behavior
- Key files touched:
  - `zen_claw/agent/intent_router.py`
  - `zen_claw/agent/intent_router_contracts.py`
  - `zen_claw/agent/intent_router_specs.py`
  - `zen_claw/agent/intent_router_parsers.py`
  - `zen_claw/agent/intent_router_shared.py`
  - `zen_claw/agent/intent_router_recovery.py`
  - `zen_claw/agent/intent_router_handlers.py`
  - `tests/test_intent_router_structure.py`
- Verification evidence:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q tests/test_sessions_sidecar.py tests/test_intent_one_shot_approval.py tests/test_intent_router_structure.py tests/test_time_direct_response.py tests/test_weather_direct_response.py tests/test_exchange_rate_direct_response.py tests/test_fixed_site_direct_response.py tests/test_intent_router_recovery_framework.py tests/test_dashboard_intent_router_trace.py` passed: `77 passed in 25.46s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .` still fails on pre-existing repository-wide lint debt unrelated to this refactor
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q` still has one unrelated failing test outside intent-router coverage: `tests/test_sessions_sidecar.py::test_agent_loop_sidecar_sessions_spawn_write_read_workflow` had previously failed during the first validation run because exec detection was too broad; that regression was fixed and the targeted suite now passes
- Follow-up impact:
  - future deterministic intent additions can land as parser/handler modules without re-expanding `intent_router.py` into another monolith

### Gateway — Agent Inventory And Detail Read Surface

- What changed:
  - extended the lightweight local `gateway --port` HTTP server so it now exposes authenticated read-only `GET /api/v1/agents` and `GET /api/v1/agents/{id}` endpoints in addition to the earlier health/invoke/webhook routes
  - kept the response shapes aligned with the existing dashboard/FastAPI agent inventory and detail payloads, including pending reload metadata, effective profile fields, raw profile overrides, and channel references
  - added gateway regression tests covering missing API key rejection plus successful list/detail reads against a mocked multi-profile config
- Key files touched:
  - `zen_claw/cli/commands.py`
  - `tests/test_gateway_health.py`
  - `tasks/todo.md`
- Verification evidence:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check zen_claw\cli\commands.py tests\test_gateway_health.py` passed
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q tests\test_gateway_health.py` passed: `9 passed in 9.38s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .` still fails on pre-existing repository-wide lint debt unrelated to this gateway read-surface change
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q` passed after this change
- Follow-up impact:
  - local UATs that probe agent inventory/detail through the gateway port no longer need a separate dashboard server path just to inspect registered profiles

### Gateway And Dashboard — Local Health Endpoint Alignment

- What changed:
  - added a lightweight local HTTP health surface to `zen-claw gateway` so `gateway --port <port>` now serves `GET /api/v1/health` with a JSON status payload and keeps `/healthz` for plain-text probes
  - aligned the blocking local `dashboard` server with the same `/api/v1/health` route so local dashboard runs no longer return `not found` for the UAT health-check path
  - added regression tests for the new gateway health server and for dashboard `/api/v1/health`
- Key files touched:
  - `zen_claw/cli/commands.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_gateway_health.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification evidence:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .` still fails on pre-existing repository-wide lint debt unrelated to this health-endpoint change
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q` passed: `1275 passed, 40 skipped in 140.61s`
- Follow-up impact:
  - UAT flows that expect `curl http://127.0.0.1:<port>/api/v1/health` now match the local gateway and dashboard behavior instead of relying on mismatched health paths

### Config Wizard — Root-Cause Guardrails And Step-by-Step Setup

- What changed:
  - moved provider/model/apiBase/key consistency checks forward into `zen-claw config wizard` so obvious misconfigurations are blocked before `agent -m`
  - redesigned the interactive wizard into a step-by-step setup flow with setup profile selection, provider confirmation, recommended model defaults, key diagnostics, and preflight summary output
  - unified the core provider-alignment diagnostics used by `config wizard`, `config doctor`, and `config troubleshoot`, including duplicated gateway-key detection and clearer fallback/path messaging
  - updated README setup guidance to state that the effective config lives at `~/.zen-claw/config.json` and is not scoped to the current working directory
- Key files touched:
  - `zen_claw/cli/commands.py`
  - `tests/test_config_provider_wizard_cli.py`
  - `tests/test_config_doctor_cli.py`
  - `tests/test_config_troubleshoot_cli.py`
  - `README.md`
  - `tasks/todo.md`
- Verification evidence:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check zen_claw\cli\commands.py tests\test_config_provider_wizard_cli.py tests\test_config_doctor_cli.py tests\test_config_troubleshoot_cli.py tests\test_config_template_cli.py` passed
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q tests\test_config_provider_wizard_cli.py tests\test_config_doctor_cli.py tests\test_config_troubleshoot_cli.py tests\test_config_template_cli.py` passed: `27 passed`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q` passed: `1269 passed, 40 skipped`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .` still fails on pre-existing repository-wide lint debt unrelated to this config work
- Follow-up impact:
  - local acceptance now surfaces duplicated gateway keys, provider/model fallback ambiguity, and config-path confusion earlier in the setup flow instead of only at first agent runtime failure

### Config Wizard — Interactive Model Selection And Profile Alias Smoothing

- What changed:
  - adjusted the interactive wizard so setup-profile input accepts natural aliases such as `openrouter` and maps them onto the intended guided profile
  - kept the model-selection step visible even when the chosen setup profile already provides a default model, so users can override the profile default during the same run
  - added `openrouter/stepfun/step-3.5-flash:free` to the OpenRouter recommended-model list so the StepFun free model is selectable in the guided flow
- Key files touched:
  - `zen_claw/cli/commands.py`
  - `tests/test_config_provider_wizard_cli.py`
  - `tasks/todo.md`
- Verification evidence:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check zen_claw\cli\commands.py tests\test_config_provider_wizard_cli.py tests\test_config_doctor_cli.py tests\test_config_troubleshoot_cli.py tests\test_config_template_cli.py` passed
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q tests\test_config_provider_wizard_cli.py tests\test_config_doctor_cli.py tests\test_config_troubleshoot_cli.py tests\test_config_template_cli.py` passed: `29 passed`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q` passed: `1271 passed, 40 skipped`
- Follow-up impact:
  - the wizard now supports a smoother OpenRouter-to-StepFun path without forcing users into scripted `--model` overrides just to avoid the profile default

### GitHub Actions — Core Tests PowerShell Shard Logging Fix

- What changed:
  - fixed the PowerShell shard log line in `.github/workflows/ci.yml` by replacing inline string interpolation with `-f` formatting
  - this avoids the `$total:` parsing ambiguity that caused `coretest0` to `coretest3` to fail before pytest started
- Key files touched:
  - `.github/workflows/ci.yml`
  - `tasks/todo.md`
- Verification evidence:
  - static check confirmed the updated line at workflow line 62
  - minimal `pwsh` snippet printed `Running shard 2/4: tests/test_a.py tests/test_b.py` without parser errors
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q` passed: `1261 passed, 40 skipped`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .` failed on pre-existing repo lint issues unrelated to this CI workflow fix
- Follow-up impact:
  - GitHub Actions `core-tests` shards should now reach pytest execution; any further CI failures should be test failures rather than PowerShell parse failures

### CI Regression Fix — RAG Retention, Notebook Repair, Timezone Recovery

- What changed:
  - made knowledge document `created_at` values monotonic per notebook so retention keeps the newest ingest even when the clock returns the same timestamp twice
  - treated empty notebooks as `no_repair_needed` even if backend initialization fails, avoiding false repair work on CI environments without a healthy persistent backend
  - preserved `locally_correctable` recovery classification for fuzzy timezone alias success even when the final time result uses fixed-offset fallback because tzdata is unavailable
- Key files touched:
  - `zen_claw/knowledge/notebook.py`
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/agent/intent_router.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_rag_notebook_mgmt.py`
- Verification evidence:
  - targeted pytest: `6 passed in 6.52s`
  - full pytest: `1263 passed, 40 skipped in 120.97s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .` still fails on 102 pre-existing repo lint issues unrelated to this fix
- Follow-up impact:
  - CI should no longer flap on same-timestamp retention ordering, empty notebook repair status, or fuzzy timezone recovery classification differences across environments

### README — Developer-First Bilingual Rewrite

- What changed:
  - rewrote the project README into a developer-first bilingual entry document
  - reorganized the homepage around current implemented capabilities, developer quick start, repository shape, CLI/API entry points, and developer navigation
  - removed outdated placeholder-style startup guidance and avoided treating roadmap content as current functionality
- Key files touched:
  - `README.md`
  - `tasks/todo.md`
- Verification evidence:
  - static verification only; no code behavior changed
  - checked README-referenced CLI examples against `zen_claw/cli/commands.py`
  - checked referenced API families against `zen_claw/dashboard/server.py`
  - checked linked docs exist: `docs/DEPLOY.md`, `docs/repo_map.md`, `docs/verify_profile.md`
- Follow-up impact:
  - new developers should be able to understand the project baseline and locate primary code/doc entry points from the repository homepage

### Docs — Project Overview

- What changed:
  - added a new developer-facing project overview document under `docs/`
  - summarized current architecture, subsystem boundaries, runtime workflows, operator surfaces, repository shape, and current scope boundaries based on implemented code rather than roadmap-only material
  - linked the new overview from the README developer navigation section
- Key files touched:
  - `docs/project-overview.md`
  - `README.md`
  - `tasks/todo.md`
- Verification evidence:
  - static verification only; no runtime behavior changed
  - confirmed `docs/project-overview.md` exists and README links point to it
  - content was grounded against current repo structure, CLI/API entry points, and generated repo map / verify profile docs
- Follow-up impact:
  - developers now have a dedicated long-form overview document in `docs/` instead of relying only on README and scattered roadmap/audit notes
 - `Channel Registry — Shared Spec Baseline`
 - `Channel Registry — Capability Metadata Surface`
 - `Channel Registry — Config-Level Control Actions`
 - `Channel Registry — Reload Acknowledgement Surface`
- `Optional Accelerator — Crawler Baseline`

## 2026-03

### Recovery Framework — BUG-009 Closure
- Summary:
  - Closed `BUG-009` after extending the unified recovery model from representative direct-intent cases into real runtime-stage exits.
  - Added real `needs_explicit_approval` and `needs_constrained_replan` production paths plus approval-to-replan trace continuity.
- Key files:
  - `zen_claw/agent/intent_router.py`
  - `zen_claw/agent/loop.py`
  - `BUG_TRACKER.md`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Recovery coverage can now evolve as a general runtime enhancement track rather than staying blocked behind `BUG-009`.

### Recovery Framework — Minimal Unified Entry
- Summary:
  - Introduced minimal unified recovery abstractions for `intent_router`.
  - Migrated representative `time / weather / wiki / exchange` recovery paths onto shared structures.
- Key files:
  - `zen_claw/agent/intent_router.py`
  - `tests/test_intent_router_recovery_framework.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Enables later closure of `BUG-006` / `BUG-009` once the full mechanism is finalized.

### Bug Closure — Local Acceptance Realignment
- Summary:
  - Closed `BUG-005`.
  - Revalidated CLI acceptance for time, weather, and wiki.
  - Synchronized tracker and local acceptance docs with observed runtime behavior.
- Key files:
  - `BUG_TRACKER.md`
  - `docs/Local_Acceptance_Guide_20260308.md`
  - `zen_claw/utils/helpers.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `BUG-006` and `BUG-009` remain open as mechanism-level items, not single-case failures.

### Skill Registry — Inventory Baseline
- Summary:
  - Added a structured skills inventory summary on top of the existing loader/market/install base instead of rebuilding the subsystem.
  - Added a real `zen-claw skills test <name>` preflight entry and shipped built-in business skill skeletons for `content_gen`, `compliance_check`, and `rag_retrieve`.
  - Exposed the inventory in dashboard snapshot data and a read-only `/api/v1/skills` API surface.
- Key files:
  - `zen_claw/agent/skills.py`
  - `zen_claw/cli/commands.py`
  - `zen_claw/dashboard/server.py`
  - `zen_claw/skills/content_gen/`
  - `zen_claw/skills/compliance_check/`
  - `zen_claw/skills/rag_retrieve/`
  - `tests/test_skills_lifecycle.py`
  - `tests/test_skills_cli.py`
  - `tests/test_dashboard_snapshot.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Phase 3.2` can now move from inventory/preflight/skeleton coverage into real business skill behavior and management controls.

### Business Skills — First Real Runtime Slice
- Summary:
  - Added a deterministic `compliance_check` rules engine and a product-facing `rag_retrieve` bridge that reuses the existing knowledge stack.
  - Added authenticated API mutation endpoints for `skills enable/disable`, so the skills inventory is no longer read-only.
  - Added local skill tests plus repo-level tests for the new business-skill behavior and API toggles.
- Key files:
  - `zen_claw/skills/compliance_check/checker.py`
  - `zen_claw/skills/compliance_check/rules.py`
  - `zen_claw/skills/rag_retrieve/retriever.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_business_skills.py`
  - `tests/test_api_gateway.py`
  - `pyproject.toml`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next `Phase 3.2` increment can focus on real `content_gen` behavior and richer dashboard management controls instead of baseline skill plumbing.

### Business Skills — Content Generation Runtime
- Summary:
  - Added a real `content_gen` module with channel templates, deterministic offline fallback generation, optional LLM provider usage, optional RAG grounding, and compliance precheck on every generated variant.
  - Added local skill tests and repo-level tests so `content_gen` now participates in both `skills test` and normal repository verification.
- Key files:
  - `zen_claw/skills/content_gen/generator.py`
  - `zen_claw/skills/content_gen/templates/`
  - `zen_claw/skills/content_gen/tests/test_generator.py`
  - `tests/test_content_gen_skill.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - All three business skill skeletons now have real runtime slices; the next gap is richer management UI and deeper semantic/tenant-aware behavior.

### Dashboard/API — Agent Planning Control Surface
- Summary:
  - Added a first config-level agent operator action on top of the existing inventory surface: authenticated planning enable/disable for registered agent profiles and the implicit `default` profile.
  - Added dashboard-side planning toggle actions plus recent agent action history using the same JSONL audit style as skills and channels.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Agent management is no longer read-only, but deeper model/routing control and richer apply semantics remain future work.

### Dashboard/API — Agent Model Control Surface
- Summary:
  - Added a first config-level model update action for agent profiles and the implicit `default` profile through authenticated API mutation.
  - Extended the dashboard `Agents` card with a minimal `Set model` action and richer audit detail for agent changes.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Agent operator control now covers both planning and direct model replacement; routing overrides and broader apply semantics remain future work.

### Dashboard/API — Agent Routing Control Surface
- Summary:
  - Added a first config-level routing-keyword update action for registered agent profiles through authenticated API mutation.
  - Extended the dashboard `Agents` card with a minimal `Set routing` action and surfaced routing keyword summaries alongside richer audit detail.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Agent operator control now reaches the first routing override layer, while richer routing policy and apply semantics remain future work.

### Dashboard/API — Agent Route Preview Surface
- Summary:
  - Added an authenticated route-preview API that evaluates current `AgentRouter` decisions against explicit overrides, sticky bound routes, profile keywords, and channel default profiles.
  - Extended the dashboard `Agents` card with a minimal `Route Preview` panel so operators can test routing decisions without sending a real inbound message.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Phase 3.1` now has both profile inventory and route-preview API coverage; future work can focus on richer profile contracts or deeper runtime/operator routing controls rather than basic orchestration visibility.

### Dashboard/API — Agent Profile Detail Surface
- Summary:
  - Added an authenticated per-profile detail API returning the effective runtime contract for one agent profile, raw configured overrides, and channel references that currently point to that profile.
  - Extended the dashboard `Agents` card with a minimal `Inspect` action so operators can understand prompt/tool/model/workspace bindings before changing routing or profile config.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Phase 3.1` orchestration visibility is now closer to a true management surface; remaining work can move toward richer profile contracts or deeper runtime/operator control semantics instead of basic inspection gaps.

### Dashboard/API — Agent Apply Acknowledgement Surface
- Summary:
  - Added an authenticated agent apply acknowledgment endpoint so config-level agent changes can move from `reload_required` into a tracked applied state instead of staying as unactioned audit rows forever.
  - Extended `/api/v1/agents` and dashboard snapshot with pending reload summary, pending action rows, and last apply event, then exposed `Mark Applied` in the dashboard `Agents` card.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Agent operator controls now have the same first apply/reload lifecycle baseline as channels, reducing the gap between config mutation and operator-visible control-plane state.

### Agent Orchestration — Profile Skill Preload Surface
- Summary:
  - Added profile-level skill preload configuration for both `agents.defaults` and individual `agents.profiles`, then carried the resolved skill slots through `AgentPool`, CLI agent surfaces, and dashboard/API profile inspection.
  - Updated orchestration tests so profile preloaded skills are visible in pool resolution, `agent list`, direct agent chat output, and `/api/v1/agents/{id}` effective profile detail.
- Key files:
  - `zen_claw/config/schema.py`
  - `zen_claw/agent/pool.py`
  - `zen_claw/cli/commands.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_agent_pool.py`
  - `tests/test_agent_orchestration_cli.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Phase 3.1` no longer needs to treat skill preload as an open contract gap; remaining work can focus on richer profile management surfaces rather than missing preload wiring.

### Agent Orchestration — Profile CRUD Surface
- Summary:
  - Added authenticated agent profile create/update/delete APIs so named profiles can now be managed as whole config objects instead of only through piecemeal planning/model/routing mutations.
  - Extended the dashboard `Agents` card with `Create profile`, `Save profile`, and `Delete profile` actions backed by the new APIs and existing audit history.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Phase 3.1` no longer has an obvious dashboard/API profile management gap; remaining orchestration work can focus on deeper runtime/operator semantics rather than missing CRUD.

### Business Skills — Tenant-Aware Retrieval Contract
- Summary:
  - Upgraded `rag_retrieve` from a thin local notebook wrapper to a business-facing bridge over the shared `RAGPipeline`, so retrieval now carries tenant scope, store backend selection, exact-match metadata filters, and source filtering through one consistent contract.
  - Extended `content_gen` grounding inputs so generation can pass notebook, tenant, backend, source, and metadata filter hints into retrieval instead of only using a bare notebook lookup.
- Key files:
  - `zen_claw/skills/rag_retrieve/retriever.py`
  - `zen_claw/skills/content_gen/generator.py`
  - `tests/test_business_skills.py`
  - `zen_claw/skills/rag_retrieve/tests/test_retriever.py`
  - `tests/test_content_gen_skill.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Phase 3.2` no longer needs to treat tenant-aware retrieval as a missing bridge layer; the remaining high-value gaps are semantic compliance review and richer content-generation product shaping.

### Business Skills — Channel Output Contract
- Summary:
  - Extended `content_gen` so generated variants now return a stable channel-specific structured payload alongside freeform content, covering article sections, social hooks, hashtags, bullets, and short-form script beats where appropriate.
  - Added an explicit `output_schema` contract to generation results so downstream UI/API consumers can render channel-tailored fields without guessing from raw text.
- Key files:
  - `zen_claw/skills/content_gen/generator.py`
  - `zen_claw/skills/content_gen/tests/test_generator.py`
  - `tests/test_content_gen_skill.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Phase 3.2` no longer has an open channel-schema contract gap for `content_gen`; the remaining content-generation work is more about prompt/response quality shaping than missing output structure.

### Business Skills — Semantic Compliance Review Baseline
- Summary:
  - Extended `ComplianceChecker` with an async semantic-review layer that keeps the existing deterministic rules engine as the source baseline, then optionally overlays an LLM-backed semantic verdict when a provider is available.
  - Wired `content_gen` to use the new layered compliance review so LLM-generated drafts now carry `semantic_review` detail, while template-only flows still return an explicit rule-only fallback status instead of silently omitting the layer.
- Key files:
  - `zen_claw/skills/compliance_check/checker.py`
  - `zen_claw/skills/compliance_check/tests/test_checker.py`
  - `zen_claw/skills/content_gen/generator.py`
  - `zen_claw/skills/content_gen/tests/test_generator.py`
  - `tests/test_business_skills.py`
  - `tests/test_content_gen_skill.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Phase 3.2` no longer needs to treat semantic compliance review as a missing layer; the remaining business-skill gap is now mainly richer content-generation prompt/response shaping and broader management UX.

### Business Skills — Content Prompt/Response Shaping Baseline
- Summary:
  - Upgraded the `content_gen` provider path to request a stricter JSON draft contract instead of relying on freeform text only, covering `headline`, `body`, `cta`, `review_notes`, and `grounding_used`, plus optional channel-specific fields.
  - Added response normalization so valid structured provider output is preserved, while invalid or plaintext provider replies still fall back cleanly to the old text-based path without breaking existing integrations.
- Key files:
  - `zen_claw/skills/content_gen/generator.py`
  - `zen_claw/skills/content_gen/tests/test_generator.py`
  - `tests/test_content_gen_skill.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Phase 3.2` no longer needs to treat prompt/response shaping as a missing `content_gen` contract layer; the remaining gaps are now mainly richer management workflow and operator UX around the business skills.

### Dashboard/API — Skill Batch Preflight Workflow
- Summary:
  - Added `/api/v1/skills/preflight-batch` so operators can run one lightweight readiness sweep across selected skills, with support for explicit name lists, missing-name reporting, and optional inclusion of disabled skills.
  - Extended the dashboard `Skills` card with a `Batch Checks` control and `Check all skills` action, reusing the existing preflight logic and audit log instead of introducing a separate health-check path.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Skill management now has a real workflow-level readiness action on top of the unified history baseline; the remaining gaps are mainly broader management UX and deeper business-skill orchestration.

### Dashboard/API — Skill Batch State Workflow
- Summary:
  - Added `/api/v1/skills/enable-batch` and `/api/v1/skills/disable-batch` so operators can change multiple skill states in one action, with missing-name reporting and per-skill audit rows.
  - Extended the dashboard `Skills` card with a `Batch State` control and `Enable selected` / `Disable selected` actions, moving the management surface beyond one-row-at-a-time toggles.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Skill management now has a first real batch state-management workflow; the remaining `Phase 3.2` gap is less about raw control availability and more about deeper UX polish and richer business-skill orchestration.

### RAG Pipeline — Notebook Retention Policy Baseline
- Summary:
  - Extended notebook metadata and notebook policy APIs so retention can now be configured per notebook through `retention_max_documents` and `retention_max_age_days`, instead of only relying on runtime arguments or project-wide defaults.
  - Updated `RAGPipeline.run_retention(...)` and the dashboard notebook-policy operator surface so retention resolves with a clearer priority: explicit request, then notebook policy, then global knowledge defaults.
- Key files:
  - `zen_claw/knowledge/notebook.py`
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Phase 3.3` no longer treats notebook retention as purely ad-hoc runtime input; remaining RAG hardening work is now more about deeper backend tuning and richer management workflow than missing per-notebook retention policy.

### Dashboard/API — Model Routing CSV + Pagination Surface
- Summary:
  - Extended `/api/v1/model-routing` with `limit` / `offset` pagination metadata and server-side `CSV` export, so recent model-routing events are no longer limited to one fixed JSON-only shape.
  - Extended the dashboard `Model Usage` card with limit/offset controls and `Export CSV` alongside the existing JSON export.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Model Usage` now has a more operator-usable export/history baseline; the remaining gap is broader retention/history depth, not basic filtering/export mechanics.

### Dashboard/API — Skill Detail Drill-down Surface
- Summary:
  - Added an authenticated per-skill detail API returning the inventory row, manifest payload, manifest load errors, current preflight result, and recent action/check/export history for one skill.
  - Extended the dashboard `Skills` card with a minimal `Inspect` action so operators can drill into manifest/integrity details instead of relying only on one-line preflight summaries.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Skill operator controls now have a first real detail/drill-down surface; the remaining gap is install/rollback style lifecycle management, not baseline observability of manifest/integrity state.

### Dashboard/API — Skill Lifecycle Install Surface
- Summary:
  - Added authenticated local skill install and uninstall endpoints on top of the existing `SkillsLoader` lifecycle helpers for workspace-scoped operator flows.
  - Extended the dashboard `Skills` card with a minimal install form plus workspace-only `Uninstall`, and skill action audit rows now carry install/uninstall detail text.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Skill operator control now covers the first true local lifecycle path; remaining gaps are rollback/versioned package policy and broader business-skill management depth.

### Dashboard/API — Skill Export Restore Surface
- Summary:
  - Added an authenticated restore endpoint that reinstalls a skill from its most recent exported zip, creating a real rollback-style operator path without inventing unsupported version semantics.
  - Extended the dashboard `Skills` card with `Restore export`, and validated the end-to-end `export -> uninstall -> restore` flow in API tests.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Skill lifecycle now has a minimal rollback-style control path; remaining version-management work is about richer visibility/policy rather than zero rollback capability.

### Dashboard/API — Skill Package Visibility Surface
- Summary:
  - Extended skill inventory/detail surfaces with package-state visibility, including selected physical dir, mapped package dirs, latest export path, and pending install journal rows.
  - Extended the dashboard `Skills` card summary and inspect panel so operators can see package/version state without guessing from raw workspace files.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Skill lifecycle now has the first real package visibility layer; remaining work is richer cleanup/policy controls rather than missing operator insight into package state.

### Dashboard/API — Skill Package Cleanup Surface
- Summary:
  - Added authenticated stale-version cleanup preview and run endpoints on top of the existing `gc_cleanup(...)` loader primitive.
  - Extended the dashboard `Skills` card with `Package Cleanup` controls so operators can preview and delete orphaned versioned package dirs without leaving the management surface.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Skill/package operations now have a minimal cleanup control plane; remaining version-management work is about richer selection/policy rather than missing install/export/restore/cleanup basics.

### Dashboard/API — Knowledge Policy History Surface
- Summary:
  - Added an authenticated knowledge policy history API with `policy_kind` / `actor` / `tenant_id` filtering plus CSV export on top of the existing policy audit log.
  - Extended the dashboard `Knowledge` card with a minimal `Policy History` panel so operators can load filtered change history or export it without reading raw JSONL files.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Phase 3.3` policy audit is now operator-usable for basic filtering/export; remaining hardening is richer history depth and broader reporting rather than raw audit access.

### Dashboard/API — Model Routing Time Filter Surface
- Summary:
  - Extended `/api/v1/model-routing` with `since_hours`, `from_at_ms`, and `to_at_ms` so recent routing history can be sliced by time range instead of only model/reason plus offset.
  - Extended the dashboard `Model Usage` card with matching time filters for in-page inspection and CSV export.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Model usage now has a more operator-usable history window; remaining work is broader retention/reporting depth rather than only recent unbounded slices.

### Dashboard/API — Model Routing Event Surface
- Summary:
  - Extended `Model Usage` beyond aggregate counts with recent routing events, in-page `model/reason` filtering, and a minimal JSON export action.
  - Added authenticated `/api/v1/model-routing` for filtered recent routing events without changing the runtime logging path.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Model usage is now an operator-usable surface rather than only a summary card, though richer pagination/export formats remain future work.

### Dashboard/API — Skill Preflight Surface
- Summary:
  - Added an authenticated lightweight skill preflight endpoint that checks manifest validity, integrity status, and local test presence without running full pytest.
  - Extended the dashboard `Skills` card with `Check` actions and recent preflight history alongside the existing enable/disable audit view.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Skills now have a first operator-ready preflight surface, while richer error-detail and rollback/export flows remain future work.

### Dashboard/API — Skill Preflight Detail Surface
- Summary:
  - Extended skill preflight audit rows with lightweight detail text and error lists so operators can see why a check failed without leaving the dashboard.
  - Added per-skill `last_check` status into the skills inventory so the main `Skills` table now exposes the latest preflight result directly.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Skill checks are now more operator-readable, though full manifest/integrity drill-down and rollback/export workflows remain future work.

### Dashboard/API — Skill Export Surface
- Summary:
  - Added an authenticated skill export endpoint on top of the existing loader export capability, writing zip packages into the workspace exports directory.
  - Extended the dashboard `Skills` card with `Export` actions and recent export history so operators can package skills without leaving the control surface.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Skills now have a first export operator flow, while install/rollback management remains future work.

### Dashboard — Operations Summary Surface
- Summary:
  - Added a unified dashboard `Operations Summary` card that aggregates key operator signals across agent activity, skill failures/exports, channel pending reloads, and recent model routing.
  - This gives `Phase 3.4` a single business-readable overview instead of requiring operators to scan each individual card first.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Dashboard readiness now has a unified summary layer, though richer reporting/export remains future work.

### Dashboard/API — Operations Summary Export Surface
- Summary:
  - Added authenticated `GET /api/v1/ops/summary` so the existing cross-surface `Operations Summary` card can also be consumed as structured JSON or exported as CSV.
  - Extended the dashboard `Operations Summary` card with `Export ops JSON` and `Export ops CSV` actions, so operators can hand off the same rollup without scraping the page.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Cross-surface reporting now has a reusable export baseline, while deeper multi-surface reporting and lifecycle controls remain open.

### Dashboard/API — Operations Summary Control-Plane Report
- Summary:
  - Extended the existing cross-surface `Operations Summary` layer into a unified control-plane report covering `agent`, `channel`, `skill`, and `model_routing` recent activity in one normalized feed.
  - Added pending-apply aggregation on top of the existing agent/channel apply logs, so both dashboard snapshot and `/api/v1/ops/summary` now expose unified pending control-plane actions instead of only per-surface counters.
  - Upgraded `/api/v1/ops/summary` with surface/actor/target filters, pagination, and richer CSV sections, then extended the dashboard card with `Recent Activity` and `Pending Apply` tables.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Phase 3.4 now has a first real unified control-plane reporting slice; the remaining gap is richer actionability on top of this feed rather than basic cross-surface aggregation.

### Dashboard/API — Operations Summary Apply Surface
- Summary:
  - Added authenticated `POST /api/v1/ops/apply-pending` so the new unified control-plane report can also clear pending `agent/channel` apply state from one cross-surface operator action.
  - Reused the existing `agent_apply.log.jsonl` and `channel_apply.log.jsonl` audit flows instead of introducing a new apply store, keeping the new action aligned with current per-surface semantics.
  - Extended the dashboard `Operations Summary` card with a `Mark Pending Applied` action so operators no longer need to visit both `Agents` and `Channels` cards just to clear pending apply state.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next Phase 3.4 increment can move from cross-surface acknowledgement into richer scoped apply/reload workflows rather than basic multi-surface clearing.

### Dashboard/API — Operations Summary Scoped Apply Surface
- Summary:
  - Extended `POST /api/v1/ops/apply-pending` so operator acknowledgements can now target one `surface + target` instead of only clearing everything in bulk.
  - Upgraded pending-state calculation to honor both global apply events and per-target apply events, so scoped acknowledgements clear only the intended `agent/channel` target while leaving unrelated pending rows intact.
  - Extended the dashboard `Operations Summary` pending table with per-row `Apply this` actions, so operators can clear one pending row directly from the unified control-plane surface.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The remaining apply/reload gap is now less about scoping and more about deeper execution semantics, such as runtime-triggered apply flows or stronger channel-specific reload behavior.

### Dashboard/API — Operations Summary Filter Panel
- Summary:
  - Extended the dashboard `Operations Summary` card with a first filter/control panel covering `surface`, `actor`, `target`, `pending_only`, `limit`, and `offset`.
  - Added `Load ops` so the card can reload directly from authenticated `/api/v1/ops/summary` instead of only showing the background snapshot, and kept the filtered report active across ordinary dashboard refreshes.
  - Reused the same active filter state for `Export ops JSON`, `Export ops CSV`, and cross-surface pending-apply acknowledgement so reporting and operator actions stay in one scoped workflow.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Phase 3.4 now has a usable filtered control panel on top of the unified ops report; the next gap is richer execution semantics behind those scoped actions rather than basic discoverability.

### Dashboard/API — Operations Summary Actor-Scoped Apply
- Summary:
  - Upgraded `POST /api/v1/ops/apply-pending` so the unified apply flow can honor the active `actor` filter from the dashboard control panel instead of only supporting `surface + target`.
  - The endpoint now resolves matching pending rows from the same filtered control-plane report used by `/api/v1/ops/summary`, then records targeted agent/channel apply audits only for the filtered pending rows.
  - Extended the dashboard `Operations Summary` apply action to forward the active `actor` scope, keeping filtered review and filtered acknowledgement in the same operator workflow.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The unified ops control plane now has a first filtered execution semantic beyond raw reporting; remaining work is deeper apply/reload behavior, not basic filter alignment.

### Dashboard/API — Apply Result Feedback
- Summary:
  - Upgraded `POST /api/v1/ops/apply-pending`, `POST /api/v1/agents/reload-ack`, and `POST /api/v1/channels/reload-ack` so acknowledgement responses now report `pending_before`, `pending_after`, and `cleared_count` instead of returning only a bare audit event.
  - Reused the same pending-state calculation already used by agent/channel listing and unified ops reporting, so acknowledgement result feedback stays aligned with the actual pending views.
  - Extended dashboard status messaging for agent, channel, and unified ops apply actions to show cleared/remaining pending counts immediately after acknowledgement.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The control plane now gives stronger execution feedback without pretending to do a true hot reload; the remaining gap is actual in-process reload/apply execution across more surfaces.

### Dashboard/API — Operations Summary Apply Preview
- Summary:
  - Added authenticated `GET /api/v1/ops/apply-plan` so operators can preview which pending rows would be acknowledged by the unified ops apply flow before writing any apply audit.
  - Reused the same filtered pending-report inputs as `/api/v1/ops/summary` and `/api/v1/ops/apply-pending`, keeping preview, reporting, and execution aligned on one scope definition.
  - Extended the dashboard `Operations Summary` card with a `Preview apply` action and an inline preview panel so operators can review matched surfaces/targets before executing the acknowledgement.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Phase 3.4 now has a first preview layer on top of unified apply; the remaining gap is true execution semantics and richer multi-surface workflow, not just discoverability.

### Dashboard/API — Operations Summary Grouped Apply Plan
- Summary:
  - Extended the unified `apply-plan` preview payload with grouped `surface_summary`, so operators can see per-surface target counts before scanning row-level detail.
  - Updated the dashboard preview panel to render a grouped `Surface / Targets / Target List` summary above the detailed pending row table.
  - This keeps the current preview flow lightweight while making the control plane more scannable before execution.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The preview layer now summarizes apply scope by surface; the next gap is stepwise execution planning rather than basic preview readability.

### Dashboard/API — Operations Summary Stepwise Apply Plan
- Summary:
  - Extended the unified `apply-plan` preview payload with ordered `steps`, turning the preview from a static row list into a minimal stepwise acknowledgement plan.
  - Updated the dashboard preview panel to show `Step / Surface / Target / Action` ahead of the raw row detail, so operators can scan intended execution order more quickly.
  - This still stays safely read-only while making the control plane feel closer to a true execution workflow.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The preview layer now has ordered execution semantics; the remaining gap is connecting that plan to true in-process multi-surface execution rather than read-only planning.

### Dashboard/API — Operations Summary Apply Result Steps
- Summary:
  - Extended `POST /api/v1/ops/apply-pending` to return ordered execution `steps`, so apply results now mirror the same stepwise structure introduced in preview.
  - Updated the dashboard apply result panel to show `Step / Surface / Target / Action / Status / Result`, pairing preview planning with post-acknowledgement outcome feedback.
  - This creates a first preview/result alignment layer without introducing fake hot-reload behavior.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The control plane now has stepwise preview/result pairing; the remaining gap is true in-process execution, not visibility of intended/applied acknowledgement steps.

### Dashboard/API — Operations Summary Apply Result Grouping
- Summary:
  - Extended `POST /api/v1/ops/apply-pending` to return grouped `surface_summary`, so apply execution now reports grouped surface/target scope in addition to step-by-step results.
  - Updated the dashboard apply result panel to render `Surface / Targets / Target List` ahead of the execution-step table, matching the preview structure more closely.
  - This keeps preview and result layouts aligned without pretending acknowledgements are full hot-reload execution.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The control plane now has grouped preview/result parity; the remaining Phase 3.4 gap is still true in-process multi-surface execution semantics rather than scope/result readability.

### Dashboard/API — Webchat Runtime Restart On Channel Apply
- Summary:
  - Extended `POST /api/v1/channels/reload-ack` so pending `webchat` channel changes can trigger a real in-process runtime stop/start before the apply audit is recorded.
  - Extended unified `POST /api/v1/ops/apply-pending` so channel-scoped apply on `webchat` also performs the same in-process runtime restart and reports that execution in step results.
  - Updated the dashboard channel apply status text so operators can see when an acknowledgement also restarted the live `webchat` runtime.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Phase 3.4 now has a first true in-process apply/reload execution path on the channel surface, but broader multi-surface execution semantics are still missing beyond `webchat`.

### Dashboard/API — Model Usage Historical Buckets
- Summary:
  - Extracted a shared model-routing summary builder so dashboard snapshot and `/api/v1/model-routing` use the same grouped counting logic.
  - Extended `/api/v1/model-routing` JSON responses with grouped `summary` data, including `model / reason / intent / channel` counts plus fixed time buckets via `bucket_minutes`.
  - Updated the dashboard `Model Usage` card to show a first historical bucket table on top of the existing recent-event filters and exports.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Model Usage` now has a first real historical-depth layer without inventing missing `agent_id` data; deeper per-agent reporting still requires adding that signal to future routing audit events.

### Dashboard/API — Operations Summary Attention Sections
- Summary:
  - Extended the unified `ops` snapshot/report model with grouped `attention_sections` instead of leaving operator attention only as free-text notes.
  - The first grouped sections cover `failed_checks`, `pending_apply`, `stale_pending_apply`, and `runtime_unsupported`, all sourced from existing skill/channel/control-plane state.
  - Updated dashboard rendering and CSV export so the same grouped attention view is visible across snapshot, API, and operator export surfaces.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Operations Summary` now has a first structured cross-surface attention layer; the remaining Phase 3.4 gap is deeper execution/report correlation, not only surfacing where attention is needed.

### Dashboard/API — Channel Capability Registry Baseline
- Summary:
  - Extended the shared channel registry metadata with runtime capability fields, so runtime support is declared in one place instead of being implied by dashboard/server branches.
  - Added shared runtime control rows for all registered channels and wired them into dashboard snapshot, `/api/v1/channels`, and `/api/v1/channels/runtime`.
  - Switched runtime dispatch lookup to handler maps, keeping `webchat` as the only real in-process executor while making unsupported channels explicit in the shared capability model.
- Key files:
  - `zen_claw/channels/registry.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Phase 3.4 now has a real shared channel capability baseline; the remaining gap is broader multi-channel execution support, not where capability metadata lives.

### Dashboard/API — Channel Runtime Control Surface
- Summary:
  - Added a first real in-process channel runtime control surface for `webchat`, including runtime status plus authenticated start/stop APIs instead of leaving channels at config toggles plus reload acknowledgements only.
  - Extended the `Channels` dashboard card with a `Runtime Controls` section and reused channel action history/detail so runtime operations are visible alongside config changes.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tests/test_dashboard_snapshot.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Channel business-readiness now has a concrete runtime control baseline, while broader multi-channel hot-reload and lifecycle orchestration still remain open.

### Dashboard/API — Skill Activity History Surface
- Summary:
  - Added authenticated `GET /api/v1/skills/history` to aggregate skill `action/check/export` events into one filterable history stream with CSV export.
  - Extended the dashboard `Skills` card with a `Skill History` panel so operators can query unified skill activity instead of manually cross-reading separate recent tables.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Skill management now has a real aggregated audit/usage baseline, while deeper business-skill behavior and richer operator workflows still remain open.

### Dashboard/API — Knowledge Activity History Surface
- Summary:
  - Added authenticated `GET /api/v1/rag/activity-history` to aggregate knowledge policy changes and knowledge cron runs into one filterable history stream with CSV export.
  - Extended the dashboard `Knowledge` card with a `Knowledge Activity` panel so operators can inspect broader tenant-aware knowledge activity instead of cross-reading policy history and cron tables separately.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
  - `tasks/done.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Knowledge management now has a unified audit baseline, while deeper lifecycle controls and backend policy sophistication still remain open.

### Optional Accelerator — Crawler Baseline
- Summary:
  - Added a first built-in `crawler` skill with browser/http extraction helpers and scheduling helpers on top of the existing browser sidecar, cron service, and RAG pipeline.
  - Added `zen-claw crawler run` and `zen-claw crawler schedule`, plus crawler-specific cron payload execution with JSONL audit logging.
  - Added a minimal `RAGPipeline.ingest_documents(...)` entry so extracted crawler content can be ingested directly as first-class documents.
  - Added a first crawler dedup/change-detection rule so unchanged source content is skipped and changed source content replaces the previous notebook document for that source.
  - Added a first shared crawler source-catalog flow backed by local JSON so operators can save named sources and run/schedule them without repeating the full parameter set each time.
  - Added a first crawler dashboard/API surface so operators can view sources, save sources, trigger ad hoc runs, and inspect recent crawler activity from the control plane.
- Key files:
  - `zen_claw/skills/crawler/extractor.py`
  - `zen_claw/skills/crawler/scheduler.py`
  - `zen_claw/skills/crawler/manifest.json`
  - `zen_claw/skills/crawler/SKILL.md`
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/cron/types.py`
  - `zen_claw/cron/service.py`
  - `zen_claw/cli/commands.py`
  - `tests/test_crawler_skill.py`
  - `tests/test_cron_crawler_cli.py`
  - `tests/test_knowledge_rag.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Phase 3.5` now has a real crawler baseline; the next increment should focus on source catalogs, dedup/change detection, and dashboard/API management instead of browser/bootstrap plumbing.

### Skills Dashboard — Basic Management Controls
- Summary:
  - Connected the dashboard `Skills` card to the existing authenticated skills management API.
  - Operators can now toggle skill enable/disable directly from the dashboard after supplying an API key in-page.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next management increment should focus on auditability and richer operator UX rather than basic mutation plumbing.

### RAG Pipeline — Shared Contract Baseline
- Summary:
  - Added a reusable `RAGPipeline` abstraction over the current `Ingestor + NotebookManager + HybridRetriever` stack.
  - Moved existing knowledge tools onto the shared pipeline and exposed the first unified RAG API/CLI surface for ingest, search, and stats.
- Key files:
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/knowledge/__init__.py`
  - `zen_claw/agent/tools/knowledge.py`
  - `zen_claw/cli/commands.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_api_gateway.py`
  - `tests/test_rag_cli.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - `Phase 3.3` can now focus on richer metadata/filter contracts, deeper store abstraction, and tenant-aware API behavior instead of basic pipeline shape.

### RAG Pipeline — Metadata Filter Contract
- Summary:
  - Added metadata propagation from ingest documents through chunking into vector-store metadata.
  - Added exact-match metadata filter support in retrieval and exposed the contract through CLI/API JSON inputs.
- Key files:
  - `zen_claw/knowledge/chunker.py`
  - `zen_claw/knowledge/retriever.py`
  - `zen_claw/knowledge/store.py`
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/agent/tools/knowledge.py`
  - `zen_claw/cli/commands.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_api_gateway.py`
  - `tests/test_rag_cli.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next `Phase 3.3` step should focus on deeper store abstraction and tenant-aware policy instead of basic metadata filtering.

### RAG Pipeline — Store Abstraction Baseline
- Summary:
  - Introduced a `VectorStore` protocol and centralized backend selection through `create_vector_store(...)` instead of constructing `ChromaStore` directly inside retrievers.
  - Added a shared in-process `memory` store backend so `RAGPipeline` can be exercised end-to-end without Chroma while keeping ingest/search/stats state across calls in one process.
- Key files:
  - `zen_claw/knowledge/store.py`
  - `zen_claw/knowledge/retriever.py`
  - `zen_claw/knowledge/pipeline.py`
  - `tests/test_knowledge_rag.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next `Phase 3.3` step should focus on tenant-aware routing/policy and richer backend configuration instead of store-construction coupling.

### RAG Pipeline — Tenant-Aware Routing Baseline
- Summary:
  - Routed `RAGPipeline` storage roots through tenant-scoped directories using the existing `tenant_data_dir(...)` helper instead of keeping all notebooks in one global data root.
  - Exposed the tenant contract through `knowledge`/`rag` CLI commands, knowledge tools, and `/api/v1/rag/*`, with API fallback to authenticated tenant context when available.
- Key files:
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/agent/tools/knowledge.py`
  - `zen_claw/cli/commands.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_api_gateway.py`
  - `tests/test_rag_cli.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next `Phase 3.3` step should focus on tenant authorization policy and richer tenant-scoped notebook/document lifecycle controls instead of basic routing isolation.

### RAG Pipeline — Notebook Backend Policy Baseline
- Summary:
  - Added notebook-level `store_backend` policy so backend selection is no longer limited to project-wide config or per-request overrides.
  - Updated `RAGPipeline` to resolve backend priority as explicit override, then notebook policy, then configured default backend.
  - Exposed notebook backend policy through knowledge CLI create/update/list/stats visibility and dashboard snapshot summary.
- Key files:
  - `zen_claw/knowledge/notebook.py`
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/cli/commands.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_rag_cli.py`
  - `tests/test_dashboard_snapshot.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next backend-selection increment should focus on tenant-level policy and backend-specific tuning rather than adding more global defaults.

### RAG Pipeline — Tenant Backend Policy Baseline
- Summary:
  - Extended tenant metadata with `store_backend` so multi-tenant RAG policy can be shared across notebooks without per-request overrides.
  - Updated `RAGPipeline` backend resolution to honor tenant policy between notebook overrides and project-wide config defaults.
  - Added minimal tenant CLI management for backend policy through `tenant create --store-backend` and `tenant set-backend`.
- Key files:
  - `zen_claw/auth/tenant.py`
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/cli/commands.py`
  - `tests/test_multitenant.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_rag_cli.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next backend-policy increment should focus on authenticated dashboard/API management surfaces and backend-specific tuning knobs.

### RAG Pipeline — Tenant Policy Dashboard/API Surface
- Summary:
  - Added authenticated API endpoints to read and update tenant-level RAG backend policy while preserving the existing fail-closed cross-tenant guard.
  - Extended the dashboard knowledge card with tenant backend policy visibility and a minimal in-page update action using the existing management API key flow.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next management increment should focus on notebook-level policy mutation and auditability rather than adding more tenant-only toggles.

### RAG Pipeline — Notebook Policy Dashboard/API Surface
- Summary:
  - Added authenticated API endpoints to read and update notebook-level RAG backend policy inside the resolved tenant scope.
  - Notebook policy updates now reuse the existing `ensure_notebook(...)` path, so operators can create notebook metadata records and assign backend policy in one step.
  - Extended the dashboard knowledge card with a minimal notebook backend policy update action alongside the existing tenant policy controls.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next management increment should focus on policy audit/change history and backend-specific tuning rather than adding more baseline mutation endpoints.

### RAG Pipeline — Policy Audit Baseline
- Summary:
  - Added JSONL audit logging for tenant and notebook backend policy changes using the existing dashboard log style.
  - Dashboard snapshot now aggregates recent policy changes into the knowledge summary and recent observability stream.
  - The dashboard knowledge card now shows recent policy change rows alongside the existing mutation controls.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next audit increment should focus on filtering/export and richer actor attribution rather than basic append-and-display plumbing.

### RAG Pipeline — Backend Tuning Knobs Baseline
- Summary:
  - Added a minimal backend-specific config layer for RAG store tuning without introducing a new backend type.
  - `KnowledgeConfig` now carries `chroma_subdir`, `chroma_collection_prefix`, and `memory_namespace`, and `RAGPipeline` passes these through to the vector-store factory.
  - Store construction is now configurable while remaining backward-compatible for both `chroma` and `memory`.
- Key files:
  - `zen_claw/config/schema.py`
  - `zen_claw/knowledge/store.py`
  - `zen_claw/knowledge/retriever.py`
  - `zen_claw/knowledge/pipeline.py`
  - `tests/test_knowledge_rag.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next backend-specific increment should focus on operational knobs like health/capacity tuning and compaction/indexing behavior rather than more naming/path config.

### RAG Pipeline — Backend Diagnostics Surface
- Summary:
  - Added lightweight backend diagnostics to the current vector-store abstraction so both `memory` and `chroma` backends now expose health/capacity-style signals without introducing a new store subsystem.
  - `RAGPipeline.stats(...)` now returns per-notebook backend diagnostics plus an aggregated backend summary covering configured backend, tenant backend policy, detected backends, and health/capacity totals.
  - Dashboard `Knowledge` summary now surfaces configured backend plus backend health/capacity rows, so operators can see backend state alongside notebook and policy data.
- Key files:
  - `zen_claw/knowledge/store.py`
  - `zen_claw/knowledge/retriever.py`
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
  - `tasks/todo.md`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next backend-specific increment can build actual remediation controls such as compaction/indexing or repair actions on top of measurable backend state instead of relying on blind config changes.

### Dashboard — Model Usage Surface
- Summary:
  - Added a first business-facing `Model Usage` dashboard card on top of existing model routing observability.
  - Dashboard snapshot now aggregates per-model and per-reason route counts, plus latest selected model and reason.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next `Phase 3.4` increment should focus on `Agent` / `Skill` management surfaces and channel abstraction cleanup, not basic model observability.

### Dashboard/API — Agent Inventory Surface
- Summary:
  - Added a first read-only `Agent` management slice by exposing registered profile inventory in dashboard and API.
  - Dashboard now shows an `Agents` card with profile id, model, planning flag, and allow/deny tool counts.
  - FastAPI now exposes `/api/v1/agents` with the same summary shape for operator-facing integration.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_dashboard_snapshot.py`
  - `tests/test_dashboard_server.py`
  - `tests/test_api_gateway.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next `Phase 3.4` increment should focus on `Skill` management/readiness surfaces or channel abstraction cleanup rather than rebuilding agent visibility.

### RAG Pipeline — Tenant Policy Guardrail
- Summary:
  - Added a fail-closed policy on `/api/v1/rag/*` so authenticated multi-tenant requests cannot override their tenant context with an explicit cross-tenant `tenant_id`.
  - Preserved current API-key-only behavior for non-session requests while keeping authenticated tenant context authoritative when present.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next `Phase 3.3` step should focus on tenant-scoped notebook/document lifecycle and deletion APIs instead of basic cross-tenant guardrails.

### RAG Pipeline — Document Delete Lifecycle
- Summary:
  - Added `RAGPipeline.delete_document(...)` and exposed it through `DELETE /api/v1/rag/doc/{id}`, `zen-claw knowledge remove`, and `zen-claw rag delete`.
  - Kept the current contract aligned with the existing store layer by treating the current `document_id` as a source identifier, while adding a lightweight source ledger so notebook `doc_count` is reclaimed when documents are deleted.
- Key files:
  - `zen_claw/knowledge/notebook.py`
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/cli/commands.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_api_gateway.py`
  - `tests/test_rag_cli.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next `Phase 3.3` step should focus on stable document IDs and document listing APIs instead of source-only delete semantics.

### RAG Pipeline — Document Inventory Baseline
- Summary:
  - Upgraded the lightweight RAG source ledger from source-only bookkeeping into a stable `document_id -> source/doc_units/created_at` inventory.
  - Ingest now returns `document_id`, the pipeline exposes `list_documents(...)`, `/api/v1/rag/documents` lists tenant-scoped inventory, and delete now consumes stable `document_id` while still deleting chunks through the existing source-based store layer.
- Key files:
  - `zen_claw/knowledge/notebook.py`
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/cli/commands.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_api_gateway.py`
  - `tests/test_rag_cli.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next `Phase 3.3` step should focus on richer document metadata and notebook retention/cleanup actions instead of basic inventory identity.

### RAG Pipeline — Document Metadata Inventory
- Summary:
  - Extended the document inventory ledger to carry metadata summaries alongside `document_id`, `source`, and lifecycle fields.
  - Ingest/list/delete payloads now preserve document-level metadata such as `source_type`, `file_ext`, page-derived hints, and normalized user metadata, providing a better base for later retention/review/policy work.
- Key files:
  - `zen_claw/knowledge/notebook.py`
  - `zen_claw/knowledge/pipeline.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_api_gateway.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next `Phase 3.3` step should focus on notebook retention/cleanup actions and richer policy on top of the improved document inventory.

### RAG Pipeline — Notebook Cleanup Baseline
- Summary:
  - Added `clear_notebook(...)` to remove all documents and vector chunks from a notebook while keeping the notebook record itself.
  - Exposed the cleanup action through `DELETE /api/v1/rag/notebook/{id}/documents`, `zen-claw knowledge clear`, and `zen-claw rag clear`.
- Key files:
  - `zen_claw/knowledge/notebook.py`
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/cli/commands.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_api_gateway.py`
  - `tests/test_rag_cli.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next `Phase 3.3` step should focus on retention policies or scheduled cleanup, not one-off manual cleanup primitives.

### RAG Pipeline — Retention Baseline
- Summary:
  - Added `run_retention(...)` with two simple policy dimensions: keep-most-recent (`max_documents`) and max-age (`max_age_days`).
  - Exposed retention through CLI/API and extended the existing knowledge cron payload so the same cleanup policy can run on a schedule without introducing a separate scheduler subsystem.
- Key files:
  - `zen_claw/config/schema.py`
  - `zen_claw/cron/types.py`
  - `zen_claw/cron/service.py`
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/cli/commands.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_rag_cli.py`
  - `tests/test_cron_knowledge_cli.py`
  - `tests/test_api_gateway.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next `Phase 3.3` step should focus on richer policy dimensions or backend configurability rather than basic scheduled cleanup plumbing.

### RAG Pipeline — Backend Config Surface
- Summary:
  - Added `KnowledgeConfig.store_backend` so the default vector store backend is no longer hard-coded in runtime paths.
  - `RAGPipeline` now resolves the effective backend from config by default, while CLI/API surfaces can explicitly override the backend for operational or test workflows.
- Key files:
  - `zen_claw/config/schema.py`
  - `zen_claw/knowledge/pipeline.py`
  - `zen_claw/agent/tools/knowledge.py`
  - `zen_claw/cli/commands.py`
  - `zen_claw/dashboard/server.py`
### Channel Action History Surface
- Summary:
  - Added filtered channel action history and CSV export through `/api/v1/channels/history`.
  - Extended the dashboard `Channels` card with channel/action/actor filters plus load/export controls.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Cross-surface operator reporting is now more consistent between `Agents` and `Channels`, which narrows the remaining dashboard/reporting backlog.

### Knowledge Policy History — Target/Backend Filters
- Summary:
  - Extended `/api/v1/rag/policy-history` to filter by `target_id`, `before_store_backend`, and `after_store_backend`.
  - Extended the dashboard `Knowledge` card history controls with target/backend filters on top of the earlier kind/actor/tenant export baseline.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Knowledge policy audit is now easier to slice by notebook/tenant target and backend transition, which makes the remaining management/reporting backlog narrower.

### Dynamic Model Routing — Session Profile Control
- Summary:
  - Added session-local `/model-profile cheap|stable|auto` runtime control on top of the new task/cost/stability routing layer.
  - Session metadata now participates in the same dynamic model selection path used by inbound metadata hints.
- Key files:
  - `zen_claw/agent/loop.py`
  - `tests/test_runtime_commands.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Dynamic routing policy is now usable from normal CLI/session flows instead of depending only on upstream metadata injection.

### Dynamic Model Routing — Metadata Policy Baseline
- Summary:
  - Added runtime model-routing policy for `task_type`, `cost`, and `stability` signals.
  - Extended config/profile/dashboard surfaces so these policies can be inspected and updated through the existing agent model-policy controls.
- Key files:
  - `zen_claw/config/schema.py`
  - `zen_claw/agent/pool.py`
  - `zen_claw/agent/loop.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_execution_loop_reflection.py`
  - `tests/test_runtime_commands.py`
  - `tests/test_api_gateway.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Dynamic routing now has a first real task/cost/stability dispatch layer instead of relying only on intent, vision, thinking, and fallback paths.

### Agent Route Override Surface
- Summary:
  - Added sticky route override management with `/api/v1/agents/route-bind` and `/api/v1/agents/route-clear`.
  - Extended the dashboard `Agents` route-preview controls with `Bind route` and `Clear route` so operators can directly manage bound routes.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `zen_claw/channels/routing.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Agent routing operator controls now cover both keyword routing config and live sticky route overrides.

### Agent Model Policy Surface
- Summary:
  - Added `/api/v1/agents/{id}/model-policy` for config-level updates to vision/thinking/fallback models, intent overrides, and allowed models.
  - Extended the dashboard `Agents` card with a `Set model policy` control that edits the current model-stack policy as JSON.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Agent operator control is no longer limited to swapping the primary model and now covers the broader config-level model policy stack.

### Agent Action History Surface
- Summary:
  - Added filtered agent action history and CSV export through `/api/v1/agents/history`.
  - Extended the dashboard `Agents` card with history filters, load, and export controls on top of the existing recent/pending action tables.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Agent operator audit is no longer limited to the fixed recent-action window, which gives the remaining policy/reload backlog a stronger reporting base.

  - `tests/test_knowledge_rag.py`
  - `tests/test_rag_cli.py`
  - `tests/test_api_gateway.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - The next `Phase 3.3` step should focus on backend-specific tuning or per-tenant/per-notebook backend policy rather than basic backend selection.

### Traceability Dashboard
- Summary:
  - Added `Intent Router`, `Compression Timeline`, `Recent Observability`, and related summaries.
  - Expanded dashboard snapshot coverage for intent traces, workflow webhooks, and model routing.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `zen_claw/agent/loop.py`
  - `tests/test_dashboard_snapshot.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Dashboard now provides mechanism-level observability instead of isolated point traces.

### Workflow Webhooks
- Summary:
  - Added workflow context extraction, `trace_id` handoff, dashboard logging, and source-level aggregation.
  - `zen-claw` now behaves more like a secure execution node for external workflows.
- Key files:
  - `zen_claw/channels/webhook_trigger.py`
  - `zen_claw/dashboard/webhooks.py`
  - `zen_claw/dashboard/server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Provides a base for richer workflow-node behavior and callback chaining later.

### Dynamic Model Routing — Minimum Viable Layer
- Summary:
  - Added `thinking_model`, `intent_model_overrides`, and model-routing observability.
  - Preserved explicit session `/model` overrides and existing `vision_model` behavior.
- Key files:
  - `zen_claw/config/schema.py`
  - `zen_claw/agent/loop.py`
  - `tests/test_runtime_commands.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Next step is model fallback and finer routing policies, not basic selection infrastructure.

### Dynamic Model Routing — Fallback Model
- Summary:
  - Added minimal `fallback_model` retry when the primary model returns `finish_reason="error"`.
  - Preserved explicit session `/model` override priority while making fallback behavior observable.
- Key files:
  - `zen_claw/config/schema.py`
  - `zen_claw/agent/loop.py`
  - `tests/test_execution_loop_reflection.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Leaves only finer task/cost/stability routing rules as the next `Phase 5` layer.

### Local-first RAG / ingest / cron — Minimum Loop
- Summary:
  - Added directory-based local knowledge ingest through the existing `knowledge add` path.
  - Added dashboard knowledge inventory, knowledge-related cron classification, explicit knowledge cron ingest, and recent knowledge cron execution visibility.
- Key files:
  - `zen_claw/knowledge/ingestor.py`
  - `zen_claw/cli/commands.py`
  - `zen_claw/dashboard/server.py`
  - `tests/test_knowledge_rag.py`
  - `tests/test_cron_knowledge_cli.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - Establishes a usable local knowledge + cron minimum loop; deeper workflow hardening remains optional follow-up work.

### Dashboard/API — Ops Apply Capability Metadata
- Summary:
  - Extended unified `ops/apply-plan` and `ops/apply-pending` steps with execution metadata so the control plane now distinguishes `audit_only` from `runtime_apply_and_audit`.
  - Wired channel capability registry data into preview/result steps, making `supported` vs `runtime_control_unsupported` explicit in both API payloads and the dashboard `Operations Summary` plan/result tables.
  - Added grouped preview/result capability counts so each surface now reports executable vs audit-only vs unsupported target totals before drilling into per-step rows.
  - Extended grouped operator attention sections with capability-aware pending apply slices, so executable vs audit-only vs unsupported pending targets are visible without opening the apply preview first.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`

### Dashboard/API — Skill Package Policy Baseline
- Summary:
  - Added a first `skill package policy` layer on top of existing package visibility, cleanup, export, and restore surfaces.
  - Skill detail now returns persisted package policy state with preferred dir/version, cleanup retention, restore-export requirement, readiness, and warnings.
  - Added `POST /api/v1/skills/{name}/package-policy` plus a dashboard `Set package policy` control that edits the current policy via the existing skill detail flow.
  - Added policy-aware `POST /api/v1/skills/{name}/restore` plus a dashboard `Restore skill` control that reuses package policy and returns a restore plan, while keeping `restore-export` for compatibility.
  - Added `GET /api/v1/skills/{name}/restore-plan` plus a dashboard `Preview restore` control so restore blockers are visible before execution instead of only surfacing as runtime failures.
  - Restore plan/execution now inspects the latest exported zip manifest and blocks restore when `preferred_version` does not match the exported package version.
  - Skill export audit now records `exported_physical_dir/exported_version`, and restore plan/execution blocks when `preferred_physical_dir` does not match the exported package metadata.
- Key files:
  - `zen_claw/dashboard/server.py`
  - `tests/test_api_gateway.py`
  - `tests/test_dashboard_server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
### Daily Assistant — Gate Phase 6-8 Safety Valve / Gate 2-3 Contract / Skill Intake Governance

- What changed:
  - added rollout/instruction docs for Phase 6/7/8 under `docs/design/`
  - hardened declarative Gate 1 with explicit `ControlSignals`, residual-ratio tracking, and multi-factor Safety Valve delegation
  - persisted Safety Valve decomposition fields into intent-router telemetry for operator debugging
  - strengthened Gate 2 prompt boundaries and bound `Gate 2 -> unclassified -> Gate 3` to an explicit `default_contract`
  - extended skill governance on top of the existing lifecycle APIs with provenance, intake summary, and promote audit metadata
- Key files:
  - `zen_claw/agent/intent_router_contracts.py`
  - `zen_claw/agent/intent_router_declarative.py`
  - `zen_claw/agent/intent_router_classifier.py`
  - `zen_claw/agent/loop.py`
  - `zen_claw/agent/skills.py`
  - `zen_claw/dashboard/server.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
- Follow-up impact:
  - crystallized pipeline normalization was kept out of Phase 6-8 itself and completed separately during the later Word-alignment pass.

### Daily Assistant — Word Alignment And Phase 5 Crystallized Normalization

- What changed:
  - added a Word-vs-repo phase comparison note under `docs/design/Daily_Assistant_Gate_Word_Alignment_20260328.md`
  - promoted crystallized routing from a placeholder into a real Gate 1 candidate source before declarative/native routing
  - reused the existing declarative execution path so crystallized candidates now share Safety Valve and telemetry behavior
  - added runtime crystallized retirement state so repeated direct failures can deactivate a crystallized route and fall back to lower-priority Gate 1 layers
- Key files:
  - `docs/design/Daily_Assistant_Gate_Word_Alignment_20260328.md`
  - `zen_claw/agent/intent_router.py`
  - `zen_claw/agent/intent_router_crystallized.py`
  - `zen_claw/agent/intent_router_daily.py`
  - `zen_claw/agent/intent_router_declarative.py`
  - `zen_claw/agent/loop.py`
  - `tests/test_intent_router_crystallized.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q tests\test_intent_declarative.py tests\test_intent_router_crystallized.py tests\test_dashboard_intent_router_trace.py tests\test_intent_router_classifier.py`
  - `44 passed in 9.38s`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
  - `1458 passed, 41 skipped in 114.74s (0:01:54)`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - repo-wide baseline still reports `116` pre-existing Ruff issues outside this change scope
- Follow-up impact:
  - Phase 5 now matches the original Word planning scope more closely because crystallized routes participate in Gate 1 runtime candidate generation instead of remaining a structural placeholder.

### Repo Baseline — Full Ruff Closure

- What changed:
  - used `ruff --fix` for the bulk mechanical cleanup and manually resolved the remaining naming violations
  - normalized import aliases in several FastAPI API test modules
  - removed ambiguous single-letter variable names in log-reading tests and color conversion helpers
  - renamed BM25 local variables in the semantic selector to satisfy naming rules without changing behavior
- Key files:
  - `tests/test_agent_model_policy.py`
  - `tests/test_agent_route_management.py`
  - `tests/test_audit_logging.py`
  - `tests/test_crawler_extraction_policy.py`
  - `tests/test_crawler_scheduling.py`
  - `tests/test_crawler_source_lifecycle.py`
  - `tests/test_model_routing_summary.py`
  - `tests/test_rag_notebook_mgmt.py`
  - `tests/test_skill_bulk_ops.py`
  - `tests/test_skill_restore_safety.py`
  - `tests/test_token_usage_tracker.py`
  - `zen_claw/agent/direct_contracts.py`
  - `zen_claw/agent/tools/semantic_selector.py`
- Verification:
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m ruff check .`
  - `All checks passed!`
  - `E:\nano-claw-public\.venv\Scripts\python.exe -m pytest -q`
  - `1458 passed, 41 skipped in 116.58s (0:01:56)`
