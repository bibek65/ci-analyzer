"""
CI Failure Analyzer — Gemini Agentic Tool Use

Replaces analyze_log.py + create_pr.py with a single Gemini agent that
autonomously calls tools to diagnose and fix CI failures.

AGENT_MODE=analyze  →  Gemini calls: read_ci_logs → search_knowledge_base → write_analysis
AGENT_MODE=fix      →  Gemini calls: read_analysis → apply_patch → create_pull_request

The agent decides the order and arguments. No hardcoded pipeline steps.
"""

import json
import os
import re
import subprocess
import sys
import time
import random
import warnings

warnings.filterwarnings("ignore", message=".*automatic function calling.*")

import chromadb
from google import genai
from google.genai import types


# ── Configuration ─────────────────────────────────────────────────────────────

GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
AGENT_MODE      = os.environ.get("AGENT_MODE", "analyze")   # analyze | fix
RUN_ID          = os.environ.get("RUN_ID", "unknown")
TRIGGER_BRANCH  = os.environ.get("TRIGGER_BRANCH", "main")
GITHUB_ACTOR    = os.environ.get("GITHUB_ACTOR", "ci-bot")
COMMIT_SHA      = os.environ.get("COMMIT_SHA", "unknown")
SHORT_SHA       = COMMIT_SHA[:7]
SAFE_TRIGGER    = TRIGGER_BRANCH.replace("/", "-")

# ChromaDB Cloud — set these as GitHub repo secrets
CHROMA_API_KEY  = os.environ.get("CHROMA_API_KEY", "")
CHROMA_TENANT   = os.environ.get("CHROMA_TENANT", "")
CHROMA_DATABASE = os.environ.get("CHROMA_DATABASE", "default_database")
CHROMA_COLLECTION = "ci_failures"
EMBED_MODEL     = "gemini-embedding-001"

LOG_FILE        = "ci_failure.log"
ANALYSIS_FILE   = "analysis.json"
CHARS_PER_TOKEN = 4
MAX_TURNS       = 12

_env_model   = os.environ.get("GEMINI_MODEL") or "gemini-2.0-flash"
_candidates  = [_env_model, "gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-flash-8b"]
_seen: set   = set()
MODEL_PRIORITY = [m for m in _candidates if m and not (m in _seen or _seen.add(m))]
MODEL          = MODEL_PRIORITY[0]

DIVIDER = "─" * 62

ERROR_PATTERNS = re.compile(
    r"(error|err |failed|failure|exception|traceback|exit code [^0]|"
    r"cannot|fatal|eresolve|enoent|permission denied|warn deprecated)",
    re.IGNORECASE,
)



# ── Retry helper ──────────────────────────────────────────────────────────────

def with_retry(fn, label="Gemini", max_retries=5):
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            err = str(e)
            is_retryable = any(k in err for k in ["429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "quota", "high demand"])
            if is_retryable and attempt < max_retries - 1:
                m = re.search(r"retry in (\d+\.?\d*)s", err)
                wait = (float(m.group(1)) + 2.0) if m else min(60.0, 2 ** attempt + random.uniform(0, 1))
                print(f"  ⚠️  {label} (attempt {attempt+1}/{max_retries}): retrying in {wait:.1f}s")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"{label} failed after {max_retries} retries")


# ════════════════════════════════════════════════════════════════════════════════
# TOOLS — ANALYZE mode
# ════════════════════════════════════════════════════════════════════════════════

def read_ci_logs() -> dict:
    """
    Read the CI failure log file and return extracted error lines with context.
    Always call this first to understand what went wrong.
    """
    try:
        with open(LOG_FILE, "r", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return {"error": f"{LOG_FILE} not found"}

    full_chars  = sum(len(l) for l in lines)
    full_tokens = full_chars // CHARS_PER_TOKEN

    CONTEXT = 3
    kept = set()
    for i, line in enumerate(lines):
        if ERROR_PATTERNS.search(line):
            for j in range(max(0, i - CONTEXT), min(len(lines), i + CONTEXT + 1)):
                kept.add(j)

    extracted_lines = [lines[i] for i in sorted(kept)]
    if len(extracted_lines) > 120:
        extracted_lines = extracted_lines[-120:]

    extracted       = "".join(extracted_lines)
    ext_tokens      = len(extracted) // CHARS_PER_TOKEN
    saved           = full_tokens - ext_tokens
    reduction       = 100 * saved // max(full_tokens, 1)

    print(f"  [read_ci_logs] {len(lines)} lines → {len(extracted_lines)} error lines ({reduction}% token reduction)")
    return {
        "extracted_log": extracted,
        "full_lines": len(lines),
        "extracted_lines": len(extracted_lines),
        "token_savings_pct": reduction,
    }


def search_knowledge_base(query: str) -> dict:
    """
    Search ChromaDB Cloud for past CI failures semantically similar to the query.
    Uses Gemini gemini-embedding-001 to convert the query into a vector, then finds
    the closest matches in the ci_failures collection.
    Call this after read_ci_logs with key error terms you observed.
    """
    if not CHROMA_API_KEY or not CHROMA_TENANT:
        print(f"  [search_knowledge_base] CHROMA_API_KEY/CHROMA_TENANT not set — skipping RAG")
        return {"matched": False, "similarity": 0.0, "reason": "ChromaDB not configured"}

    # Embed the query with Gemini
    embed_result = client.models.embed_content(
        model=EMBED_MODEL,
        contents=query,
    )
    query_vector = list(embed_result.embeddings[0].values)

    # Query ChromaDB Cloud
    chroma = chromadb.HttpClient(
        ssl=True,
        host="api.trychroma.com",
        tenant=CHROMA_TENANT,
        database=CHROMA_DATABASE,
        headers={"x-chroma-token": CHROMA_API_KEY},
    )
    collection = chroma.get_collection(CHROMA_COLLECTION)
    results = collection.query(
        query_embeddings=[query_vector],
        n_results=3,
        include=["documents", "metadatas", "distances"],
    )

    matches = []
    for doc, meta, distance in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        similarity = round(1 - distance, 2)
        if similarity >= 0.6:
            matches.append({
                "similarity": similarity,
                "category": meta.get("category", "unknown"),
                "severity": meta.get("severity", "unknown"),
                "past_fix": doc,
            })

    if matches:
        best = matches[0]
        print(f"  [search_knowledge_base] best match: [{best['category']}] similarity={best['similarity']}")
        return {
            "matched": True,
            "similarity": best["similarity"],
            "category": best["category"],
            "past_fix": best["past_fix"],
            "all_matches": matches,
        }

    print(f"  [search_knowledge_base] no match above 0.60 threshold")
    return {"matched": False, "similarity": 0.0}


def write_analysis(
    error_type: str,
    error_slug: str,
    root_cause: str,
    affected_file: str,
    patch_type: str,
    search_string: str,
    replacement_string: str,
    fix_command: str,
    severity: str,
    confidence: str,
    pr_title: str,
    pr_description: str,
) -> dict:
    """
    Save the structured diagnosis to analysis.json.
    Call this last, after you have read the logs and searched the knowledge base.
    patch_type must be one of: search_replace | prepend | append | none.
    severity must be one of: high | medium | low.
    confidence must be one of: high | medium | low.
    """
    data = {
        "run_id":              RUN_ID,
        "model":               MODEL,
        "error_type":          error_type,
        "error_slug":          error_slug,
        "root_cause":          root_cause,
        "affected_file":       affected_file,
        "patch_type":          patch_type,
        "search_string":       search_string.replace("\\n", "\n"),
        "replacement_string":  replacement_string.replace("\\n", "\n"),
        "fix_command":         fix_command,
        "severity":            severity,
        "confidence":          confidence,
        "pr_title":            pr_title,
        "pr_description":      pr_description.replace("\\n", "\n"),
    }
    with open(ANALYSIS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  [write_analysis] saved {ANALYSIS_FILE} — error_type={error_type} severity={severity} confidence={confidence}")
    return {"saved": True, "file": ANALYSIS_FILE, "error_type": error_type}


# ════════════════════════════════════════════════════════════════════════════════
# TOOLS — FIX mode
# ════════════════════════════════════════════════════════════════════════════════

def read_analysis() -> dict:
    """
    Read the analysis.json produced in the analyze phase.
    Always call this first to get the diagnosis before applying any fix.
    """
    if not os.path.exists(ANALYSIS_FILE):
        return {"error": f"{ANALYSIS_FILE} not found — did the analyze phase complete?"}
    with open(ANALYSIS_FILE) as f:
        data = json.load(f)
    print(f"  [read_analysis] loaded — error_type={data.get('error_type')} patch_type={data.get('patch_type')}")
    return data


def read_file(file_path: str) -> dict:
    """
    Read the current content of a file in the repository.
    Use this in fix mode when apply_patch fails — read the actual file content
    to verify what is there before retrying with a corrected search_string.
    """
    if not os.path.exists(file_path):
        return {"error": f"{file_path} not found"}
    with open(file_path, "r", errors="replace") as f:
        content = f.read()
    print(f"  [read_file] {file_path} — {len(content)} chars")
    return {"file_path": file_path, "content": content, "chars": len(content)}


def apply_patch(affected_file: str, patch_type: str, search_string: str, replacement_string: str) -> dict:
    """
    Apply a code patch to the affected file.
    patch_type must be one of: search_replace | prepend | append.
    For search_replace: search_string must be an exact substring of the file content.
    """
    if not affected_file or not os.path.exists(affected_file):
        return {"patched": False, "reason": f"file not found: {affected_file}"}

    with open(affected_file, "r") as f:
        content = f.read()

    if patch_type == "search_replace":
        if not search_string:
            return {"patched": False, "reason": "search_string is empty"}
        if search_string not in content:
            return {"patched": False, "reason": f"search_string not found in {affected_file}"}
        if replacement_string and replacement_string in content:
            return {"patched": False, "reason": "patch already applied"}
        new_content = content.replace(search_string, replacement_string, 1)

    elif patch_type == "prepend":
        sep = "" if replacement_string.endswith("\n") else "\n"
        new_content = replacement_string + sep + content

    elif patch_type == "append":
        sep = "" if content.endswith("\n") else "\n"
        new_content = content + sep + replacement_string

    else:
        return {"patched": False, "reason": f"unknown patch_type: {patch_type}"}

    with open(affected_file, "w") as f:
        f.write(new_content)

    print(f"  [apply_patch] patched {affected_file} ({patch_type}) ✅")
    return {"patched": True, "file": affected_file, "patch_type": patch_type}


def create_pull_request(pr_title: str, pr_description: str, commit_message: str) -> dict:
    """
    Set up the fix branch, commit any patched files, push, and open a GitHub PR.
    Call this after apply_patch. The branch name is derived automatically.
    """
    def run(cmd, check=True, capture=False):
        return subprocess.run(cmd, check=check, capture_output=capture, text=True)

    # Read error_type from analysis.json to build branch name
    error_type = "unknown"
    if os.path.exists(ANALYSIS_FILE):
        with open(ANALYSIS_FILE) as f:
            error_type = json.load(f).get("error_type", "unknown")

    branch = f"fix/{SAFE_TRIGGER}-{error_type}"

    # Git identity
    run(["git", "config", "user.email", f"{GITHUB_ACTOR}@users.noreply.github.com"])
    run(["git", "config", "user.name", GITHUB_ACTOR])
    run(["git", "fetch", "origin"])

    # Create or reuse fix branch
    remote_check = run(["git", "ls-remote", "--heads", "origin", branch], capture=True, check=False)
    if remote_check.stdout.strip():
        run(["git", "checkout", branch])
        merge = run(["git", "merge", f"origin/{TRIGGER_BRANCH}", "--no-edit"], check=False, capture=True)
        if merge.returncode != 0:
            run(["git", "merge", "--abort"], check=False)
        print(f"  [create_pull_request] reused existing branch {branch}")
    else:
        run(["git", "checkout", "-b", branch, f"origin/{TRIGGER_BRANCH}"])
        print(f"  [create_pull_request] created branch {branch}")

    # Stage only the patched file — never commit analysis.json or CI artifacts
    affected_file = ""
    if os.path.exists(ANALYSIS_FILE):
        with open(ANALYSIS_FILE) as f:
            affected_file = json.load(f).get("affected_file", "")

    if affected_file and os.path.exists(affected_file):
        run(["git", "add", affected_file])
    else:
        run(["git", "add", "-A", "--", ":!analysis.json", ":!ci_failure.log"])
    diff = run(["git", "diff", "--cached", "--quiet"], check=False)
    if diff.returncode != 0:
        run(["git", "commit", "-m", f"{commit_message}\n\nRun: {RUN_ID}"])
        print(f"  [create_pull_request] committed changes")
    else:
        run(["git", "commit", "--allow-empty", "-m",
             f"fix(ci): AI analysis for {error_type} at {SHORT_SHA}\n\nRun: {RUN_ID}"])
        print(f"  [create_pull_request] empty commit (no file patch)")

    run(["git", "push", "origin", branch])
    print(f"  [create_pull_request] pushed origin/{branch}")

    # Check for existing PR
    existing = run(
        ["gh", "pr", "list", "--head", branch, "--json", "number,url", "--jq", ".[0]"],
        capture=True, check=False,
    )
    existing_text = existing.stdout.strip()
    if existing_text and existing_text != "null":
        try:
            ex = json.loads(existing_text)
            url = ex.get("url", "")
            print(f"  [create_pull_request] PR already exists: {url}")
            return {"created": False, "updated": True, "url": url, "branch": branch}
        except json.JSONDecodeError:
            pass

    result = run(
        ["gh", "pr", "create",
         "--title", pr_title,
         "--body", pr_description,
         "--base", TRIGGER_BRANCH,
         "--head", branch],
        capture=True, check=False,
    )

    if result.returncode == 0:
        url = result.stdout.strip()
        print(f"  [create_pull_request] PR created ✅ {url}")
        return {"created": True, "url": url, "branch": branch}
    else:
        error = result.stderr.strip()
        print(f"  [create_pull_request] PR creation failed: {error}")
        return {"created": False, "error": error}


# ════════════════════════════════════════════════════════════════════════════════
# System prompts & initial messages
# ════════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPTS = {
    "analyze": (
        "You are a Senior DevOps engineer specializing in CI/CD reliability.\n"
        "A CI build has failed. Use your tools in this order:\n"
        "1. read_ci_logs — get the extracted error lines from the failed build\n"
        "2. search_knowledge_base — search with the key error terms you found\n"
        "3. write_analysis — save your complete structured diagnosis\n\n"
        "Rules for write_analysis:\n"
        "- error_type: kebab-case category. Use npm-dependency | docker-build | "
        "test-failure | github-actions | database for known patterns. "
        "Otherwise generate a precise 2-3 word kebab-case label. NEVER use 'other'.\n"
        "- patch_type: MUST be search_replace for any code/config file fix. "
        "Use none only for infra-level actions (NAT gateway, IAM role).\n"
        "- search_string: exact text currently in the file (copy character-for-character).\n"
        "- replacement_string: the fixed replacement text.\n"
        "- ALWAYS fix the root cause in the source file (package.json, Dockerfile, config). "
        "NEVER add flags or workarounds to the CI pipeline YAML to suppress errors — "
        "that hides the problem without solving it.\n"
        "- pr_description: use this format exactly:\n"
        "  ## Problem\\n...\\n\\n## Root Cause\\n...\\n\\n## Proposed Fix\\n```\\n<fix>\\n```"
        "\\n\\n## How to Verify\\n- step 1\\n- step 2\\n\\n"
        "⚠️ This fix was proposed by AI analysis. Review before merging."
    ),
    "fix": (
        "You are a Senior DevOps engineer applying a CI fix.\n"
        "Use your tools in this order:\n"
        "1. read_analysis — load the diagnosis from the analyze phase\n"
        "2. apply_patch — patch the affected file using the exact strings from the analysis\n"
        "3. If apply_patch returns 'search_string not found', call read_file on the affected_file "
        "to see its actual current content, then retry apply_patch with the correct search_string "
        "that matches what is actually in the file. Do NOT retry with the same search_string.\n"
        "4. create_pull_request — commit the patch and open the GitHub PR\n\n"
        "Use the pr_title and pr_description from the analysis as-is.\n"
        "For commit_message use: 'fix(ci): <error_type> fix for commit <sha>'"
    ),
}

INITIAL_PROMPTS = {
    "analyze": f"A GitHub Actions build has failed on branch '{TRIGGER_BRANCH}'. Diagnose the failure and save a structured analysis.",
    "fix":     f"Apply the fix from the analysis phase and create a GitHub PR on branch '{TRIGGER_BRANCH}'. Commit SHA: {SHORT_SHA}.",
}


# ════════════════════════════════════════════════════════════════════════════════
# Main — agentic loop
# ════════════════════════════════════════════════════════════════════════════════

if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY is not set.")
    sys.exit(1)

if AGENT_MODE not in ("analyze", "fix"):
    print(f"ERROR: AGENT_MODE must be 'analyze' or 'fix', got '{AGENT_MODE}'")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

TOOL_MAP = {
    "analyze": [read_ci_logs, search_knowledge_base, write_analysis],
    "fix":     [read_analysis, read_file, apply_patch, create_pull_request],
}
tools = TOOL_MAP[AGENT_MODE]
TOOL_REGISTRY = {fn.__name__: fn for fn in tools}

print(f"{'━' * 62}")
print(f"  CI FAILURE ANALYZER — Gemini Agent ({AGENT_MODE.upper()} mode)")
print(f"  Model : {MODEL}  |  Run: {RUN_ID}  |  Branch: {TRIGGER_BRANCH}")
print(f"  Tools : {', '.join(TOOL_REGISTRY)}")
print(f"{'━' * 62}\n")

config = types.GenerateContentConfig(
    tools=tools,
    tool_config=types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(mode="AUTO")
    ),
    system_instruction=SYSTEM_PROMPTS[AGENT_MODE],
    temperature=0.0,
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
)

contents = [INITIAL_PROMPTS[AGENT_MODE]]
used_model = None

for turn in range(MAX_TURNS):
    print(f"  ── Turn {turn + 1} {'─' * 48}")

    # Try model priority list for daily quota fallback
    response = None
    for _model in MODEL_PRIORITY:
        try:
            response = with_retry(
                (lambda m: lambda: client.models.generate_content(
                    model=m, config=config, contents=contents
                ))(_model),
                label=f"agent[{_model}]",
            )
            used_model = _model
            break
        except Exception as e:
            err = str(e)
            if ("PerDay" in err or "NOT_FOUND" in err or "404" in err) and _model != MODEL_PRIORITY[-1]:
                print(f"  ⚠️  {_model} quota/not-found — trying next model")
                continue
            raise

    if response is None:
        print("  ❌  All models exhausted daily quota")
        sys.exit(1)

    # Add model response to conversation
    model_content = response.candidates[0].content
    contents.append(model_content)

    # Collect function calls from this response
    function_calls = [
        part.function_call
        for part in model_content.parts
        if hasattr(part, "function_call") and part.function_call
    ]

    if not function_calls:
        # Agent is done — print final message
        final_text = ""
        for part in model_content.parts:
            if hasattr(part, "text") and part.text:
                final_text += part.text
        print(f"\n  Agent complete:\n  {final_text[:400]}")
        break

    # Execute each tool call
    tool_parts = []
    for fc in function_calls:
        fn_name = fc.name
        fn_args = dict(fc.args) if fc.args else {}
        print(f"  → {fn_name}({', '.join(f'{k}={repr(str(v))[:40]}' for k, v in fn_args.items())})")

        if fn_name not in TOOL_REGISTRY:
            result = {"error": f"unknown tool: {fn_name}"}
        else:
            try:
                result = TOOL_REGISTRY[fn_name](**fn_args)
            except Exception as e:
                result = {"error": str(e)}

        print(f"    ← {json.dumps(result)[:200]}")
        tool_parts.append(
            types.Part.from_function_response(name=fn_name, response=result)
        )

    contents.append(types.Content(role="user", parts=tool_parts))

else:
    print(f"  ⚠️  Reached MAX_TURNS ({MAX_TURNS}) without agent finishing")
    sys.exit(1)

print(f"\n  ✅ Agent ({AGENT_MODE}) complete — model used: {used_model}")

# Validate output for analyze mode
if AGENT_MODE == "analyze" and not os.path.exists(ANALYSIS_FILE):
    print(f"  ❌  {ANALYSIS_FILE} was not written — agent did not call write_analysis")
    sys.exit(1)
