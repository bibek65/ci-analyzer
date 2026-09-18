"""
CI Failure Analyzer — Slack Notification

POSTs flat JSON key-value pairs to a Slack Workflow webhook URL.
The Slack Workflow uses these variables to format and send the message.

Set SLACK_WEBHOOK_URL in GitHub repo secrets to the URL generated
by the Slack Workflow Builder "From a webhook" trigger.

NOTIFICATION_TYPE controls the message label:
  approval  — after analysis, asks human to approve/reject via GitHub Actions link
  success   — after PR is created (human approved)
  rejected  — after human rejected or approval timed out
"""

import json
import os
import sys
import urllib.error
import urllib.request

ANALYSIS_FILE = "analysis.json"

SLACK_WEBHOOK_URL     = os.environ.get("SLACK_WEBHOOK_URL", "")
GITHUB_SERVER_URL     = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
GITHUB_REPOSITORY     = os.environ.get("GITHUB_REPOSITORY", "")
RUN_ID                = os.environ.get("RUN_ID", "unknown")
TRIGGER_BRANCH        = os.environ.get("TRIGGER_BRANCH", "main")
NOTIFICATION_TYPE     = os.environ.get("NOTIFICATION_TYPE", "approval")
DEMO_SCENARIO         = os.environ.get("DEMO_SCENARIO", "")

if not SLACK_WEBHOOK_URL:
    print("WARNING: SLACK_WEBHOOK_URL not set — skipping Slack notification")
    sys.exit(0)

run_url = f"{GITHUB_SERVER_URL}/{GITHUB_REPOSITORY}/actions/runs/{RUN_ID}"

data = {}
if os.path.exists(ANALYSIS_FILE):
    with open(ANALYSIS_FILE) as f:
        data = json.load(f)

payload = {
    "notification_type": NOTIFICATION_TYPE,
    "trigger_branch":    TRIGGER_BRANCH,
    "demo_scenario":     DEMO_SCENARIO if DEMO_SCENARIO else "push-triggered",
    "error_type":        data.get("error_type", "unknown"),
    "root_cause":        data.get("root_cause", "—"),
    "severity":          data.get("severity", "—"),
    "confidence":        data.get("confidence", "—"),
    "fix_command":       data.get("fix_command", "—"),
    "affected_file":     data.get("affected_file", "—"),
    "pr_title":          data.get("pr_title", "fix: CI failure"),
    "run_url":           run_url,
}

body = json.dumps(payload).encode("utf-8")
req = urllib.request.Request(
    SLACK_WEBHOOK_URL,
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"Slack notification sent ({NOTIFICATION_TYPE}): HTTP {resp.status}")
        print(f"Payload: {json.dumps(payload, indent=2)}")
except urllib.error.HTTPError as e:
    print(f"Slack webhook error: HTTP {e.code} — {e.read().decode()}")
    sys.exit(1)
except urllib.error.URLError as e:
    print(f"Slack webhook connection error: {e.reason}")
    sys.exit(1)
