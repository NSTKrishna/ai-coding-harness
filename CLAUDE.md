# Claude Code Project Instructions

Read these files before substantial work:

1. `arch.md` — technical source of truth
2. `REQUIREMENTS.md` — hackathon compliance matrix
3. `PROGRESS.md` — current implementation state and next milestone

## Rules

- Inspect existing code before changing it.
- Do not claim a requirement is complete without test/evidence.
- Work only on the milestone requested.
- Do not begin the next milestone automatically.
- Preserve offline setup and Python 3.10+ compatibility.
- Avoid new dependencies unless they provide clear value.
- Prefer deterministic computation before model calls.
- Run targeted tests while developing, then the full suite.
- Update `PROGRESS.md` and `REQUIREMENTS.md` only after verification.
- Never weaken tests to make implementation pass.
- Never expose `AI_API_KEY`.
- Do not automatically commit changes.
- Keep the core architecture aligned with `arch.md`.

## Current architecture principle

The system should remain:

model reasoning
+ deterministic tools
+ explicit state
+ focused context
+ verification
+ recovery

Do not introduce multi-agent complexity unless there is measured justification.
