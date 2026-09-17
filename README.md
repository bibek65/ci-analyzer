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

A single script that runs a **Gemini function-calling loop**. Gemini autonomously decides which tools to call, in what order, and when it has enough information to stop.

#### Analyze mode tools

| Tool | What Gemini uses it for |
|---|---|
| `read_ci_logs()` | Reads `ci_failure.log`, extracts only error lines + 3 lines of context. Reduces ~200 log lines to ~26 (89% token savings). |
| `search_knowledge_base(query)` | Scores the query against 5 known failure patterns by keyword overlap. Returns the best match with similarity score and a proven past fix. |
| `write_analysis(...)` | Saves 12 structured fields to `analysis.json` including `error_type`, `root_cause`, `patch_type`, `search_string`, `replacement_string`, `pr_title`, `pr_description`. |

#### Fix mode tools

| Tool | What Gemini uses it for |
|---|---|
| `read_analysis()` | Loads `analysis.json` from the analyze phase. |
| `apply_patch(...)` | Applies `search_replace`, `prepend`, or `append` patch to the affected file. Has a duplicate guard — skips if the fix is already present. |
| `create_pull_request(...)` | Creates/reuses git fix branch, stages changed files, commits, pushes, opens PR via `gh pr create`. |

#### The agentic loop

```python
contents = ["A GitHub Actions build has failed. Diagnose it."]

for turn in range(MAX_TURNS):
    response = gemini.generate(contents, tools=tools)
    contents.append(response)                          # grow conversation history

    function_calls = extract_function_calls(response)

    if not function_calls:
        break                                          # agent is done

    for fc in function_calls:
        result = TOOL_REGISTRY[fc.name](**fc.args)    # execute the tool
        contents.append(tool_response(fc.name, result))  # feed result back
```

Each turn is one Gemini API call. The model sees the full conversation so far (including all previous tool results) and decides what to do next. When it stops emitting function calls, the loop ends.

**Real tool call trace from a live run:**
```
Turn 1 → read_ci_logs()
         ← 206 lines → 26 error lines (89% token reduction)

Turn 2 → search_knowledge_base(query='npm error ERESOLVE unable to resolve dependency')
         ← matched: npm ERESOLVE peer dependency conflict, similarity: 0.8

Turn 3 → write_analysis(error_type='npm-dependency', patch_type='search_replace',
                         search_string='"react": "17.0.2"',
                         replacement_string='"react": "18.0.0"', severity='high', ...)
         ← saved analysis.json ✅

Turn 4 → (no tool calls) — agent done
```

#### Model fallback chain

```python
MODEL_PRIORITY = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-flash-8b"]
```

If the primary model hits its daily quota, the agent automatically retries with the next model. No manual intervention needed.

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

### Secrets (repo → Settings → Secrets and variables → Actions → Secrets)

| Secret | Required | Value |
|---|---|---|
| `GEMINI_API_KEY` | Yes | API key from [aistudio.google.com](https://aistudio.google.com) |
| `SLACK_WEBHOOK_URL` | Yes | Webhook URL from Slack Workflow Builder |

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

## Guardrail

The AI **proposes** the fix. A human **reviews and approves** before any PR is created. The agent never pushes directly to main, never merges its own PR, and every PR description ends with:

> ⚠️ This fix was proposed by AI analysis. Review before merging.
