# compliance_check

Use this skill when the user asks for policy, risk, or marketing-language review.

## Review layers

1. Exact forbidden or sensitive wording.
2. Pattern-based claims such as guaranteed returns or absolute rankings.
3. Broader semantic risk that still needs human confirmation.

## Inputs to collect

- source_text
- industry
- channel
- policy_context

## Output shape

- overall risk level: low | medium | high
- short score rationale
- issues list with the exact phrase, risk type, and safer rewrite
- explicit note when legal or compliance sign-off is still required
