# AIDLC Project Context

This project follows the AI-DLC (AI-Driven Development Life Cycle) methodology.

## Key Locations

- **AIDLC Rules**: `aidlc-rules/` — Core workflow and phase-specific rules
  - `aws-aidlc-rules/core-workflow.md` — Main workflow entry point
  - `aws-aidlc-rule-details/` — Detailed rules by phase (inception, construction, operations)
- **Project Documentation**: `aidlc-docs/` — This project's AIDLC artifacts (requirements, design, construction plans)
- **Reference Guide**: `aidlc-docs/WORKING-WITH-AIDLC.md` — How to work with AIDLC methodology

## Project Structure

This is the PersonalContextAI product. The AIDLC methodology guides its development through structured phases:
1. Inception (requirements, application design, user stories)
2. Construction (functional design, NFR design, build and test)
3. Operations (deployment, monitoring)

## Continuity Note (2026-09-01)

Between 2026-08-31 and 2026-09-01 this project was developed in VS Code with GitHub
Copilot instead of Kiro. That session used `.aidlc-rule-details/` (populated then,
confirmed byte-identical to `aidlc-rules/aws-aidlc-rule-details/`) and
`.github/copilot-instructions.md` in place of this file, which was absent for that
stretch and has now been recreated pointing at the same rule tree Kiro originally used.

Work continued for real during that period — Unit 5 (Orchestration Depth) was built
substantially (its sub-steps are labelled unit-5 through unit-9 in git history, but map
onto the single Unit 5 scope from `aidlc-docs/inception/application-design/unit-of-work.md`).
`aidlc-docs/aidlc-state.md` and `aidlc-docs/audit.md` were both kept current throughout
and are the authoritative record of what was decided and built — read them before
resuming any AIDLC stage rather than relying on this note's summary.

As of this note: full test suite at 380 passing. Unit 5 code generation Part 2 has one
known open item (`tests/unit/test_resiliency_bounds.py` coverage for the
`GeminiProviderAdapter` semaphore/timeout and `GraphitiMemoryAdapter._guard`, per the
RESILIENCY-10 finding recorded in audit.md) and one uncommitted working-tree edit to
`aidlc-docs/construction/plans/unit-5-orchestration-depth-code-generation-plan.md`
reconciling its checklist against what was actually built.
