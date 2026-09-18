# CI Failure Analyzer — Gemini Agentic Tool Use

A real GitHub Actions pipeline where a **Gemini AI agent** autonomously diagnoses CI failures, proposes a code fix, waits for human approval via Slack, then creates a PR with the patch applied.

---

## End-to-End Flow

```
push to main
    │
    ▼
[Job 1: build]
    npm install → FAILS (react@17 + react-dom@18 peer conflict)
    │
    ▼ if: failure()
[Job 2: analyze-failure]
    ├─ gh api → download ci_failure.log from GitHub
    ├─ agent.py AGENT_MODE=analyze
    │    Turn 1: read_ci_logs()           → 206→26 lines, 89% token savings
    │    Turn 2: search_knowledge_base()  → npm-dependency match, similarity 0.8
    │    Turn 3: write_analysis()         → saves analysis.json
    ├─ notify_slack.py (approval)         → Slack message with GitHub approval link
    └─ upload analysis.json artifact
    │
    ▼ waits for human
[Job 3: approve-and-fix]  ←  environment: fix-approval (human clicks Approve / Reject)
    │
    ├── YES (Approved)
    │    ├─ agent.py AGENT_MODE=fix
    │    │    Turn 1: read_analysis()         → loads diagnosis
    │    │    Turn 2: apply_patch()            → react@17 → 18 in package.json
    │    │    Turn 3: create_pull_request()    → branch + commit + push + PR
    │    └─ notify_slack.py (success)          → Slack confirms PR created
    │
    └── NO (Rejected / timed out)
         └─ [Job 4: notify-rejection]
              └─ notify_slack.py (rejected)    → Slack asks for manual fix
```

---

## The Intentional Bug

`package.json` pins `react@17.0.2` and `react-dom@18.0.0`. These are incompatible — `react-dom@18` requires `react@18` as a peer dependency. `npm install` fails with `ERESOLVE`. This gives the agent a real, specific error to diagnose and a concrete file patch to propose.

---

## How Every Segment Works

### 1. `package.json` — The Trigger

```json
"react": "17.0.2",
"react-dom": "18.0.0"
```

Intentional version mismatch. `npm install` fails, which activates the entire AI pipeline downstream.

---

### 2. `ci.yml` — Workflow Orchestration

**Job 1: `build`** — runs `npm install`. Fails on the version conflict.

**Job 2: `analyze-failure`** — `if: failure()` means it only runs when build fails.
- Downloads the failed build logs via GitHub REST API (`gh api .../jobs/{id}/logs`)
- Runs `agent.py` in `AGENT_MODE=analyze`
- Sends Slack notification with the diagnosis and an approval link
- Uploads `analysis.json` as a GitHub Actions artifact (passes data to Job 3)

**Job 3: `approve-and-fix`** — `environment: fix-approval` pauses the job until a human approves or rejects in the GitHub UI.
- `if: always() && needs.analyze-failure.result == 'success'` — `always()` is required because `analyze-failure` used `if: failure()`; without it GitHub's implicit success check would skip this job.
- If approved: runs `agent.py` in `AGENT_MODE=fix`, then sends Slack success notification
- If rejected: skipped, triggers Job 4

**Job 4: `notify-rejection`** — runs only when `approve-and-fix` result is `failure` or `cancelled`. Sends a Slack message asking for manual intervention.

---

### 3. `agent.py` — The AI Agent (both modes)

A single script, two modes (`AGENT_MODE=analyze` / `AGENT_MODE=fix`), driven by a Gemini function-calling loop. Gemini autonomously decides which tools to call, in what order, and when to stop.

---

#### § Configuration

At startup the script builds two key things:

**`MODEL_PRIORITY`** — deduplicated fallback list:
```python
[GEMINI_MODEL, "gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-flash-8b"]
```
If the `GEMINI_MODEL` secret is set it goes first. When a model hits its daily quota, the agent moves to the next automatically.

**`ERROR_PATTERNS`** — regex that flags log lines worth keeping:
```
error | err  | failed | failure | exception | traceback |
exit code [^0] | cannot | fatal | eresolve | enoent | permission denied
```

---

#### § `with_retry`

Wraps every Gemini API call. On `429 / 503 / RESOURCE_EXHAUSTED` it waits and retries up to 5 times. Uses the suggested `retry in Xs` delay from the error if present, otherwise exponential backoff. Without this, a single noisy minute on the free tier would crash the pipeline.

---

#### § Analyze mode — tool sequence

**Tool 1: `read_ci_logs()`**

Reads `ci_failure.log`, finds every line matching `ERROR_PATTERNS`, keeps those lines plus 3 lines of context above and below each match, caps at 120 lines total.

```
Input : ci_failure.log (206 lines)
Output: 26 error lines — 89% token reduction
```

**Tool 2: `search_knowledge_base(query)`**

Takes the key error terms Gemini extracted from the log. Embeds the query with `gemini-embedding-001` (768-dimensional vector), queries ChromaDB Cloud, filters results to cosine similarity ≥ 0.60, returns the best matching past failure and its proven fix.

```
Input : "npm ERESOLVE unable to resolve dependency tree"
Output: { matched: true, similarity: 0.82, category: "nodejs",
          past_fix: "...upgrade react-testing-library to 13..." }
```

When a match is found, Gemini gets a proven fix injected into context and doesn't have to reason from scratch.

**Tool 3: `write_analysis(...)`**

Gemini fills in 12 structured fields and this tool writes them to `analysis.json`:

| Field | Example |
|---|---|
| `error_type` | `npm-dependency` |
| `patch_type` | `search_replace` |
| `search_string` | `"react": "17.0.2"` |
| `replacement_string` | `"react": "18.0.0"` |
| `affected_file` | `package.json` |
| `severity` / `confidence` | `high` / `high` |
| `pr_title` | `fix(deps): align react to 18.0.0` |
| `pr_description` | Full markdown: Problem / Root Cause / Fix / Verify |

**How Gemini knows what to put in `search_string` / `replacement_string`:** It reads the npm error output, which contains both values explicitly:
```
npm ERR! peer react@"^18.0.0" from react-dom@18.0.0   ← what is required
npm ERR! Found: react@17.0.2                            ← what is installed
```
Gemini never reads `package.json` directly — it infers the broken value and the correct value from the error message alone.

---

#### § Fix mode — tool sequence

**Tool 1: `read_analysis()`**

Loads `analysis.json` from disk (downloaded artifact). The fix job runs on a different VM with no memory of the analyze phase — this is how the diagnosis crosses the job boundary.

**Tool 2: `apply_patch(...)`**

Reads the affected file, finds `search_string`, replaces it with `replacement_string`:

```python
new_content = content.replace(search_string, replacement_string, 1)
```

Has a duplicate guard — if `replacement_string` is already in the file it skips without error. This means the job can be re-run safely without corrupting the file.

**Tool 3: `create_pull_request(...)`**

1. Names the branch from `error_type`: `fix/main-npm-dependency`
2. Sets git identity using `GITHUB_ACTOR`
3. Creates or reuses the branch (no duplicate branches)
4. `git add -A` → commit → push
5. Checks if a PR already exists on that branch (no duplicate PRs)
6. `gh pr create` → opens the PR

---

#### § System prompts

Instructions Gemini reads before the loop starts. They enforce:
- **Tool order**: `read_ci_logs` → `search_knowledge_base` → `write_analysis` (analyze); `read_analysis` → `apply_patch` → `create_pull_request` (fix)
- **Field rules**: `error_type` must be kebab-case, never `"other"`; `patch_type` must be `search_replace` for any file fix
- **PR format**: Problem / Root Cause / Proposed Fix / How to Verify

Without the ordering constraint, Gemini might call `write_analysis` before reading the logs.

---

#### § Agentic loop

Each iteration is one Gemini API call. `contents` grows on every turn so Gemini sees the full conversation history:

```
Turn 1:  "diagnose this failure" + system prompt
         → Gemini calls read_ci_logs()
         ← 206 lines → 26 error lines (89% token reduction)

Turn 2:  sees error lines
         → Gemini calls search_knowledge_base("npm ERESOLVE...")
         ← similarity=0.82, past_fix="upgrade react-testing-library..."

Turn 3:  sees logs + proven past fix
         → Gemini calls write_analysis(error_type="npm-dependency", ...)
         ← analysis.json saved ✅

Turn 4:  no function calls → loop breaks, agent done
```

If Gemini returns no function calls → loop exits cleanly. If `MAX_TURNS=12` is reached without finishing → script exits with error. After analyze mode, the script verifies `analysis.json` was written — if Gemini skipped `write_analysis`, the job fails.

---

#### § Data flow

```
ANALYZE mode
  ci_failure.log (206 lines)
      │ read_ci_logs() → 26 error lines
      ▼
  Gemini sees errors
      │ search_knowledge_base("npm ERESOLVE") → similarity=0.82
      ▼
  Gemini sees logs + proven fix
      │ write_analysis(...) → analysis.json on disk
      ▼
  ci.yml: upload-artifact → GitHub storage

FIX mode
  ci.yml: download-artifact → analysis.json on disk
      │ read_analysis() → 12 fields
      ▼
  Gemini reads diagnosis
      │ apply_patch("package.json", search_replace) → file patched
      ▼
      │ create_pull_request() → branch + commit + push + PR
      ▼
  PR opened on GitHub
```

---

### 4. `notify_slack.py` — Slack Notifications

Posts flat JSON to a **Slack Workflow webhook**. The Slack Workflow formats and sends the message.

Three notification types, controlled by `NOTIFICATION_TYPE` env var:

| Type | When sent | Contains |
|---|---|---|
| `approval` | After analysis completes | Error type, root cause, severity, confidence, proposed fix, link to approve in GitHub |
| `success` | After PR is created | PR title, fix branch name, link to run |
| `rejected` | After human rejects or timeout | Error summary, asks for manual fix |

---

## Setup

### One-time setup — populate ChromaDB

Run this once locally to embed the 10 CI failure patterns into ChromaDB Cloud:

```bash
GEMINI_API_KEY=... CHROMA_API_KEY=... CHROMA_TENANT=... CHROMA_DATABASE=... \
python3 .github/scripts/embed_failures.py
```

This uses `gemini-embedding-001` to embed each failure document and stores them in the `ci_failures` collection. Only needs to run again if you add new failure patterns.

### Secrets (repo → Settings → Secrets and variables → Actions → Secrets)

| Secret | Required | Value |
|---|---|---|
| `GEMINI_API_KEY` | Yes | API key from [aistudio.google.com](https://aistudio.google.com) |
| `SLACK_WEBHOOK_URL` | Yes | Webhook URL from Slack Workflow Builder |
| `CHROMA_API_KEY` | Yes | API key from [trychroma.com](https://trychroma.com) |
| `CHROMA_TENANT` | Yes | Tenant ID from ChromaDB Cloud dashboard |
| `CHROMA_DATABASE` | No | Database name (defaults to `default_database`) |

### Variables (repo → Settings → Secrets and variables → Actions → Variables)

| Variable | Required | Value |
|---|---|---|
| `GEMINI_MODEL` | No | Override the default model (e.g. `gemini-2.0-flash`) |

### Environment (repo → Settings → Environments → New environment)

| Environment | Configuration |
|---|---|
| `fix-approval` | Add yourself as a **Required reviewer** |

`GITHUB_TOKEN` is provided automatically — no setup needed.

---

## Tech Stack

| Component | Version |
|---|---|
| Python | 3.14 |
| google-genai | 2.20.0 |
| Node.js | 24 LTS |
| actions/checkout | v7.0.1 |
| actions/setup-python | v7.0.0 |
| actions/setup-node | v7.0.0 |

---

## Why Not MCP?

MCP (Model Context Protocol) lets an AI agent call external tools — like reading GitHub issues or posting to Slack — through a standardized server interface. It's a valid architecture, but it requires running a persistent MCP server outside GitHub Actions (a webhook server, a hosted process), which adds infrastructure complexity.

This project uses **Gemini's native function calling** instead:

| Capability | MCP approach | This project |
|---|---|---|
| Read CI logs | GitHub MCP server | `gh api` CLI in the workflow step, file passed to agent |
| Search past failures | RAG MCP server | ChromaDB Cloud queried directly from Python |
| Post to Slack | Slack MCP server | `notify_slack.py` — plain HTTP POST to Slack Workflow webhook |
| Create GitHub PR | GitHub MCP server | `subprocess` calls to `git` + `gh` CLI inside `agent.py` |

The agent still **behaves like it's using MCP** — Gemini calls named tools, gets results back, and decides what to do next. The difference is the tools are plain Python functions running inside the GitHub Actions runner, not external MCP servers. No extra infrastructure needed.

---

## Guardrail

The AI **proposes** the fix. A human **reviews and approves** before any PR is created. The agent never pushes directly to main, never merges its own PR, and every PR description ends with:

> ⚠️ This fix was proposed by AI analysis. Review before merging.

