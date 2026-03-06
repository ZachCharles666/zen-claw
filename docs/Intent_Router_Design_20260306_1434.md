# Intent Router Design (2026-03-06 14:34 UTC+08)

## 1. Background

This document defines a framework-first design for an `intent_router` layer in `zen-claw`.

The immediate trigger is the weather-query failure mode observed in local user-style UAT:

- the user asked a high-certainty question (`成都最近一周的天气`)
- the system should have deterministically preferred `web_fetch`
- instead, the runtime still depended on the LLM to choose the correct tool path
- when the model drifted toward `exec`, the request fell into approval handling instead of returning a useful result

This is not a weather-only problem. It is a class of failures caused by treating prompt guidance and skill instructions as a substitute for execution-layer determinism.

## 2. Design Goal

The goal is to add a pre-LLM routing layer for high-certainty intents so that:

- low-risk, low-ambiguity requests can bypass open-ended planning
- prompt drift does not immediately degrade into approval errors or poor UX
- the runtime remains governed by the same policy, permission, and audit controls
- the design scales beyond weather instead of accumulating ad hoc branches inside `AgentLoop`

## 3. Non-Goals

This design does not aim to:

- replace the existing skill system
- replace the planning / execution / reflection loop for open-ended tasks
- weaken the approval model for high-risk tools
- automatically grant dangerous tools when deterministic routing fails

## 4. Position in Architecture

### 4.1 Proposed Module

Add a dedicated module:

- `zen_claw/agent/intent_router.py`

Optional follow-on split if it grows:

- `zen_claw/agent/intents/base.py`
- `zen_claw/agent/intents/weather.py`
- `zen_claw/agent/intents/time.py`
- `zen_claw/agent/intents/exchange_rate.py`

### 4.2 Runtime Placement

The router should execute inside `AgentLoop._process_message()` after:

- identity fail-closed validation
- channel/session policy setup
- tool context preparation

But before:

- history construction
- system prompt assembly
- planning phase
- LLM tool selection

This keeps the router inside the governed runtime while avoiding unnecessary LLM turns.

## 5. Core Principle

The system must not rely on "the model should behave correctly" as a safety or UX guarantee.

The runtime should instead aim for:

`even if the model drifts, the system still follows the intended low-risk path whenever the intent is sufficiently clear`

## 6. Intent Classification Scope

The router should only cover intents that satisfy all of the following:

- strong user intent clarity
- narrow parameter extraction surface
- stable preferred tool path
- low-risk execution profile
- user expectation of direct, immediate results

Initial candidate intents:

- weather forecast
- time / current date
- timezone conversion
- exchange rate
- simple public-page fetch
- fixed-format factual utility responses

These should be introduced incrementally, not all at once.

## 7. Contract Model

The key missing layer in the current runtime is a formal `intent -> tool contract`.

### 7.1 Contract Purpose

The contract defines:

- which tools are valid for an intent
- which tool is preferred
- which tools must never be used for that intent
- whether constrained replanning is allowed
- whether approval escalation is ever valid for that intent

### 7.2 Proposed Shape

```python
@dataclass
class IntentToolContract:
    intent_name: str
    preferred_tools: list[str]
    allowed_tools: set[str]
    denied_tools: set[str]
    allow_constrained_replan: bool = True
    allow_high_risk_escalation: bool = False
    response_mode: Literal["direct", "llm_assisted"] = "direct"
```

Example for weather:

```python
IntentToolContract(
    intent_name="weather",
    preferred_tools=["web_fetch"],
    allowed_tools={"web_fetch"},
    denied_tools={"exec", "spawn", "write_file", "edit_file"},
    allow_constrained_replan=True,
    allow_high_risk_escalation=False,
    response_mode="direct",
)
```

This is intentionally stricter than SKILL.md guidance. Prompt text is advisory; the contract is runtime-enforced.

## 8. Router Result Model

The router should return a structured result instead of embedding behavior in scattered branches.

```python
@dataclass
class IntentRouteResult:
    handled: bool
    intent_name: str | None = None
    content: str | None = None
    contract: IntentToolContract | None = None
    route_status: Literal[
        "miss",
        "direct_success",
        "direct_failed",
        "needs_constrained_replan",
        "needs_explicit_approval",
    ] = "miss"
    diagnostic: str | None = None
```

## 9. Runtime State Machine

The intended state machine is:

`direct route -> low-risk auto-correction -> constrained replanning -> one-shot explicit approval -> fail`

### 9.1 Direct Route

If the router can confidently recognize the intent and execute the preferred low-risk tool path:

- run the preferred tool directly
- format the result deterministically
- return to the user without entering planning

### 9.2 Low-Risk Auto-Correction

If direct execution fails, the router may attempt limited internal correction only when:

- the contract still stays within low-risk tools
- correction is equivalent or narrower than the original route
- no dangerous tool is introduced

Examples:

- weather payload parsing fallback
- alternative JSON field interpretation
- alternate but still public weather endpoint through the same low-risk fetch tool

This stage is internal. The user should not see an approval prompt or be asked to reason about tool choice.

### 9.3 Constrained Replanning

If direct handling still fails and the contract allows it:

- enter one LLM replanning turn
- tool availability is constrained to the intent contract
- denied tools for that intent remain unavailable even if globally available

For weather, this means:

- the LLM may try again
- but only with `web_fetch`
- not with `exec`
- not with approval-triggering shell detours

This is the critical bridge between deterministic routing and the existing agentic runtime.

### 9.4 One-Shot Explicit Approval

If the constrained path still cannot solve the task and the contract explicitly allows high-risk escalation, the system may request a one-shot approval.

Important:

- this is not automatic escalation
- the runtime must never self-authorize dangerous tools because a low-risk route failed

The one-shot approval should be:

- explicit
- narrow
- session-scoped
- turn-scoped
- ideally parameter-hash bound
- auditable as a special approval type

Proposed semantics:

- tool name fixed
- tool args hash fixed
- valid for current session only
- consumed once
- expires quickly

### 9.5 Fail

If all prior stages fail:

- return a plain failure to the user
- avoid misleading "permission restriction" wording when the true failure is parsing or route mismatch
- preserve diagnostics for logs and audit, not user-facing clutter

## 10. Interaction with Existing Components

### 10.1 AgentLoop

`AgentLoop` remains the orchestrator, but it should stop embedding domain-specific logic directly.

Target shape:

- `AgentLoop` prepares runtime context and policies
- `intent_router` attempts deterministic handling
- on miss, the normal planning loop continues
- on constrained replanning, `AgentLoop` invokes the LLM with a narrowed tool surface

### 10.2 ToolRegistry

The router must not bypass `ToolRegistry`.

All tool execution should still flow through:

- `ToolRegistry.execute(...)`

This preserves:

- policy scopes
- quota enforcement
- logging
- trace propagation
- tool error normalization

### 10.3 Policy Engine

The router should add a temporary scope for constrained replanning when needed.

Example idea:

- scope name: `intent_contract`
- allow: contract `allowed_tools`
- deny: contract `denied_tools`

This keeps intent routing aligned with the existing layered governance approach already used in the project.

### 10.4 Skill Permission Gate

The intent contract should cooperate with skill permissions, not replace them.

Effective allowed tools during constrained replanning should be:

- global policy allowlist
- intersected with channel/session policy
- intersected with skill permission gate
- intersected with intent contract allowlist

The narrowest result wins.

### 10.5 ApprovalGate

Approval remains the last line for genuine high-risk actions.

Rules:

- direct-route low-risk intents should not fall into approval because the model drifted
- denied-by-contract tools should not become approval candidates
- approval should only occur when the contract explicitly permits escalation and the user explicitly authorizes it

This is the most important safety rule in the design.

## 11. User-Facing Behavior

The user should observe:

- fewer pointless approval interruptions
- more direct answers for obvious utility questions
- clearer error language when low-risk routing fails

The user should not observe:

- internal "router correction" chatter by default
- hidden auto-escalation to dangerous tools
- fake permission explanations for non-permission failures

Optional future diagnostic mode:

- when `/verbose on`, include a short line like:
  - `[intent-router] matched=weather, route=direct_success`

## 12. Weather as the First Migration Example

Weather is a good first intent because it demonstrates the whole pattern:

- intent extraction is simple
- preferred tool path is stable
- the user expects a direct answer
- the current failure mode is already known and reproducible

Weather should therefore be reworked not as a permanent special case in `AgentLoop`, but as the first intent implementation inside `intent_router`.

Recommended final path:

1. detect weather intent
2. build weather contract
3. execute deterministic fetch path
4. if parse fails, perform low-risk auto-correction
5. if still failing, allow one constrained replan with `web_fetch` only
6. do not escalate to `exec`

## 13. Rollout Plan

### Phase A

- create `intent_router` module
- move weather handling into it
- support only `direct_success` and `miss`

### Phase B

- add `IntentToolContract`
- add constrained replanning with contract-scoped tool narrowing

### Phase C

- add low-risk auto-correction framework
- add better diagnostic logging

### Phase D

- add one-shot explicit approval model for selected future intents
- only where high-risk escalation is truly legitimate

## 14. Risks

### 14.1 Scope Creep

If every convenience case becomes an intent, the router becomes a giant rule engine. The acceptance bar for new intents must stay high.

### 14.2 Duplicate Logic

If routing rules and skill prompts diverge, maintenance cost rises. Contracts and skill guidance should be generated or reviewed together when possible.

### 14.3 Hidden Architecture Drift

If direct-route code continues to live inside `AgentLoop`, the orchestration layer will accumulate business logic again. The dedicated module boundary should be enforced early.

## 15. Decision Summary

The project should proceed with:

- framework first
- then weather rework on top of the framework

It should not continue with:

- more ad hoc intent branches directly in `AgentLoop`
- reliance on skill prose as the only execution guarantee
- automatic dangerous-tool escalation after low-risk routing failure

## 16. One-Sentence Principle

`Do not design for "the model should behave"; design for "the runtime still behaves correctly when the model does not."`
