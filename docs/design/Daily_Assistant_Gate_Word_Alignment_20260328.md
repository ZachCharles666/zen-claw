# Daily Assistant Gate Word Alignment 20260328

## Goal

Align the repository execution record with the original Word planning document
`zen-claw-intent-router-讨论纪要与阶段规划.docx`, and explicitly identify
where the executed phase scope was narrower than the original plan.

## Comparison Table

| Phase | Word scope | Repo execution reality before this alignment | Alignment status |
| --- | --- | --- | --- |
| Phase 1 | compatibility-safe type and telemetry scaffolding | consistent | aligned |
| Phase 2 | candidate-oriented rule output | consistent | aligned |
| Phase 3 | introduce Safety Valve | initially landed as a minimal declarative slice; later Phase 6 completed the broader multi-factor Safety Valve and control-plane pieces | aligned after Phase 6 |
| Phase 4 | introduce Gate 2 classifier | initially landed as classifier integration only; clarification vs unclassified boundary and Gate 3 default contract were hardened later in Phase 7 | aligned after Phase 7 |
| Phase 5 | close Gate 3, crystallized pipeline, and telemetry | initially landed only as explicit Gate 3 entry + telemetry consolidation; crystallized runtime normalization remained missing | narrowed before this alignment |
| Phase 6 | control plane + Safety Valve enhancement | implemented | aligned |
| Phase 7 | Gate 2 prompt engineering | implemented | aligned |
| Phase 8 | external skill intake / promote governance | implemented | aligned |

## Main Scope Drift

### Phase 3

Word described a broader Safety Valve introduction. The repo first delivered a
minimal declarative-only slice, then later filled the missing factors in
Phase 6:

- explicit `control_signals`
- residual-ratio evidence
- multi-hit / pronoun / long-input checks
- richer telemetry decomposition

### Phase 4

Word treated Gate 2 as a complete classification layer. The repo first landed
classifier integration, while the prompt boundary and `Gate 2 -> Gate 3`
handoff contract were finalized later in Phase 7.

### Phase 5

This was the largest drift:

- Word scope: Gate 3 closure + crystallized pipeline normalization + telemetry
- executed repo scope: explicit Gate 3 entry + telemetry only

The missing runtime item was:

- move crystallized routing fully inside Gate 1 candidate generation

## Alignment Decision

To match the Word plan without rewriting the whole router stack, the completed
alignment version of Phase 5 uses this runtime shape:

1. crystallized routes are treated as a first-class Gate 1 candidate source
2. crystallized candidates execute through the same Safety Valve and telemetry
   path as other Gate 1 candidates
3. crystallized routes can be retired after repeated failures
4. declarative and native routing remain the fallback layers behind the
   crystallized candidate pass

## Acceptance Notes

Phase 5 should now be considered fully aligned with the Word planning scope
only when all of the following are true:

- Gate 3 remains the only free-planning path
- telemetry still records routing-stage and Gate 3 entry semantics
- crystallized routes are emitted as Gate 1 candidates with `source=crystallized`
- crystallized routes no longer bypass the common candidate / Safety Valve path
- crystallized routes can be deactivated by runtime retirement logic
