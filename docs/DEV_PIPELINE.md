# Dev Pipeline

Sprint-based development workflow. All state is local (markdown files). Entry point: `/go`.

## Pipeline Flow

```
/brainstorm (свободный формат, вне спринта)
     ↓ (user routes action items manually)
     ├── → VISION.md (product direction changes)
     ├── → backlog.md (deferred pool)
     ├── → /new-sprint (scope for next sprint)
     └── → hotfix (< 3 files, [hotfix] prefix)

/go (dispatcher — reads STATUS.md, first match wins)
 │
 ├─ No sprint ──────────────── /new-sprint
 │   reads: VISION.md, ROADMAP.md, backlog.md
 │   creates: docs/sprints/NNN-slug/, STATUS.md
 │
 ├─ Phase has no tasks ─────── /plan-phase
 │   reads: sprint.md, code
 │   architectural gate: STOP if foundation is bad
 │   creates: task files in sprints/NNN/tasks/
 │
 ├─ Tasks pending/in_progress ─ /implement (per task)
 │   TDD: Red → Green → Refactor
 │   push + PR + CI gate + smoke test
 │   merge + update task status
 │
 ├─ All tasks done ──────────── /close-phase
 │   run integration tests
 │   write missing tests, fix stale tests
 │   advance to next phase
 │
 ├─ All phases done ─────────── Sprint Endgame:
 │   ├── /audit (code scan + VISION.md check)
 │   ├── /e2e-run (pipeline test)
 │   ├── Fix phase (triage: quick-fix / sprint-relevant / backlog)
 │   ├── /update-docs
 │   └── /close-sprint (push, CHANGELOG, ROADMAP, STATUS history)
 │
 └─ Blockers ────────────────── Report, wait for human
```

## Key Files

| File | Role | Updated by |
|------|------|------------|
| `docs/VISION.md` | Product direction + architectural invariants | User manually, `/brainstorm` action items |
| `docs/STATUS.md` | Current sprint state (phase, progress) | Sprint skills (`/go` reads this) |
| `docs/sprints/NNN-slug/` | Sprint directory (sprint.md + task files) | Sprint skills |
| `docs/backlog.md` | Deferred pool (tech debt, ideas) | User manually, `/close-sprint` deferred items |
| `docs/ROADMAP.md` | Story-level milestones | `/close-sprint` marks completed |
| `docs/CHANGELOG.md` | Release history | `/close-sprint` |
| `docs/audit.md` | Latest audit results | `/audit` |

## Skills

### Sprint lifecycle
| Skill | What it does |
|-------|-------------|
| `/go` | Dispatcher: reads STATUS.md, invokes the right skill |
| `/new-sprint` | Create sprint from VISION + ROADMAP + backlog |
| `/plan-phase` | Generate task files for current phase (with arch gate) |
| `/implement` | TDD cycle for one task, PR + CI + merge |
| `/close-phase` | Integration tests + advance to next phase |
| `/close-sprint` | Final gate: push, CHANGELOG, ROADMAP, STATUS history |

### Quality & testing
| Skill | What it does |
|-------|-------------|
| `/audit` | Code scan + VISION.md invariant check |
| `/e2e-run` | Full pipeline test (requires `make up`) |
| `/test-maintenance` | Run/fix integration tests locally |

### Thinking & docs
| Skill | What it does |
|-------|-------------|
| `/brainstorm` | Structured adversarial discussion on a topic |
| `/update-docs` | Sync living docs with codebase (incremental or full) |
| `/optimize` | Process skill feedback entries |

### Pipeline testing (require running services)
| Skill | What it does |
|-------|-------------|
| `/escort` | Accompany real user through full pipeline |
| `/architect` | Decompose story into tasks (API-based) |

## Sprint Structure

```
docs/sprints/NNN-slug/
├── sprint.md              — goal, phases, decisions, deferred, endgame status, results
├── tasks/
│   ├── phase0-task1-*.md  — description, tests, acceptance criteria, status
│   ├── phase0-task2-*.md
│   ├── phase1-task1-*.md
│   └── fix-task1-*.md     — fix phase tasks (from audit + e2e findings)
└── e2e/
    └── (e2e reports if applicable)
```

## Task File Format

```markdown
# Phase N Task M: <Title>

## Description
<what needs to change and why>

## Tests First
- <test 1 — what to assert, which test file>

## Acceptance Criteria
- [ ] <criterion>

## Status: pending | in_progress | done

## Developer Notes
_Filled during implementation._
```

## Sprint Endgame

After all feature phases complete:

1. **Audit + E2E** run in parallel (find different problem classes)
2. **Triage findings** into three buckets:
   - Quick-fix (<5 min) → fix phase task
   - Sprint-relevant (in changed code) → fix phase task
   - Backlog (unrelated tech debt) → `backlog.md`
3. **Fix phase** — implement quick-fixes and sprint-relevant issues
4. **Update docs** — `/update-docs` syncs living documentation
5. **Close sprint** — push all commits, update CHANGELOG/ROADMAP/STATUS

## Tech Sprints

Every 5th sprint (or earlier if backlog >30 items):
- `/new-sprint` checks cadence and proposes tech sprint
- Scope formed from backlog, prioritized: security → code smells → infra → nice-to-have
- Same lifecycle: phases, tasks, endgame

## Outside Sprint Flow

Small fixes (< 3 files) bypass the sprint:
- `[hotfix]` commit prefix + CHANGELOG entry
- No PR needed for doc-only changes

`/brainstorm` runs anytime, outside sprint rhythm. Results routed manually by user.

## Testing Strategy

**Per task** (in `/implement`):
- TDD: Red → Green → Refactor
- Test level matches code: DB/Redis → service test, cross-service → integration, pure logic → unit

**Per phase** (in `/close-phase`):
- Run existing integration tests
- Write missing integration tests for new code paths
- Fix stale tests (API contract changes, DTO changes)

**Per sprint** (endgame):
- `/audit` — static analysis, invariant check
- `/e2e-run` — runtime pipeline test
