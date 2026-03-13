#!/usr/bin/env python3
"""
NBA Betting Sim — Railway runner
Handles both scout (14:00 PT) and commit (22:30 PT) phases autonomously.

Usage:
    python run.py scout
    python run.py commit
"""

import sys
import json
import os
import re
import base64
from pathlib import Path
from datetime import datetime, timezone

import anthropic
from duckduckgo_search import DDGS
import github
from github import GithubException


def _load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

# Load local .env and fallback to .env.example for local testing
_load_env_file(Path(__file__).parent / ".env")
_load_env_file(Path(__file__).parent / ".env.example")

# ── Config ───────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO       = os.environ.get("GITHUB_REPO")   # e.g. "joaopgms/betaGOD"
MODEL             = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")

if not ANTHROPIC_API_KEY or not GITHUB_TOKEN or not GITHUB_REPO:
    missing = [k for k in ["ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GITHUB_REPO"] if not os.environ.get(k)]
    print("ERROR: Missing required environment variables:", ", ".join(missing))
    print("Set them in Railway environment variables or local .env")
    sys.exit(1)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web for NBA scores, injury reports, betting odds, team news, "
            "standings, and any other information needed for the simulation. "
            "Returns up to 6 result snippets with titles, URLs, and summaries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query string"
                }
            },
            "required": ["query"],
        },
    }
]


# ── Web Search ────────────────────────────────────────────────────────────────

def web_search(query: str) -> str:
    """Search the web using DuckDuckGo (no API key required)."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=6))
        if not results:
            return f"No results found for: {query}"
        return "\n\n".join(
            f"**{r['title']}**\n{r['href']}\n{r['body']}"
            for r in results
        )
    except Exception as e:
        return f"Search error for '{query}': {e}"


# ── GitHub Helpers ────────────────────────────────────────────────────────────

def get_repo():
    return github.Github(auth=github.Auth.Token(GITHUB_TOKEN)).get_repo(GITHUB_REPO)


def read_github_file(repo, path: str) -> str | None:
    try:
        content = repo.get_contents(path)
        return base64.b64decode(content.content).decode("utf-8")
    except GithubException:
        return None


def write_github_file(repo, path: str, content: str, message: str) -> None:
    try:
        existing = repo.get_contents(path)
        repo.update_file(path, message, content, existing.sha)
    except GithubException:
        repo.create_file(path, message, content)
    print(f"  ✔ committed {path}")


# ── Tool Call Handler ─────────────────────────────────────────────────────────

def handle_tool_call(name: str, inputs: dict) -> str:
    if name == "web_search":
        query = inputs.get("query", "")
        print(f"  🔍 web_search: {query}")
        return web_search(query)
    return f"Unknown tool: {name}"


# ── Agentic Loop ──────────────────────────────────────────────────────────────

def run_agent(system: str, messages: list) -> str:
    """
    Run Claude in a tool-use loop until end_turn.
    Returns the final assistant text response, or an error message if Anthropic fails.
    """
    while True:
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=system,
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.BadRequestError as e:
            error_info = str(e)
            print(f"⚠️ Anthropic API error: {error_info}")
            return f"[Anthropic Error: {error_info}]"

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return "".join(
                block.text for block in response.content if hasattr(block, "text")
            )

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = handle_tool_call(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            # Unexpected stop (max_tokens, etc.) — return whatever we have
            return "".join(
                block.text for block in response.content if hasattr(block, "text")
            )


# ── XML Parser ────────────────────────────────────────────────────────────────

def extract_tag(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.DOTALL)
    return m.group(1).strip() if m else None


# ── data.js Generator ─────────────────────────────────────────────────────────

def generate_data_js(history_json: str, draft_picks: list, scout_model_output: str = "", scout_error: str = "", scout_status: str = "unavailable", scout_updated_at: str = "") -> str:
    ts = datetime.now(timezone.utc).isoformat()
    return (
        f"// Auto-generated by NBA sim — do not edit manually\n"
        f"// Last updated: {ts}\n\n"
        f"const HISTORY_DATA = {history_json};\n\n"
        f"const DRAFT_PICKS = {json.dumps(draft_picks, indent=2)};\n\n"
        f"const SCOUT_MODEL_OUTPUT = {json.dumps(scout_model_output)};\n"
        f"const SCOUT_ERROR = {json.dumps(scout_error)};\n"
        f"const SCOUT_STATUS = {json.dumps(scout_status)};\n"
        f"const SCOUT_UPDATED_AT = {json.dumps(scout_updated_at)};\n"
    )


# ── Phase Runner ──────────────────────────────────────────────────────────────

def run_phase(phase: str) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"\n▶ NBA Sim — {phase.upper()} phase — {today}")
    print("─" * 50)

    # Load state from GitHub
    repo = get_repo()
    state_json   = read_github_file(repo, "nba_sim_state.json")
    history_json = read_github_file(repo, "nba_sim_history.json")

    if not state_json or not history_json:
        print("ERROR: Could not read state files from GitHub. Check GITHUB_REPO and GITHUB_TOKEN.")
        sys.exit(1)

    # Load system prompt
    prompt_path = Path(__file__).parent / "prompts" / f"{phase}.md"
    system_prompt = prompt_path.read_text(encoding="utf-8")

    # Build user message
    user_message = f"""Today's date: {today}

Current nba_sim_state.json:
```json
{state_json}
```

Current nba_sim_history.json:
```json
{history_json}
```

Execute the {phase.upper()} PHASE now, following all steps in your instructions.
Use web_search for all research. Output the updated files using the XML tags at the end."""

    print("⚙ Running Claude agent...")
    messages = [{"role": "user", "content": user_message}]
    final_text = run_agent(system_prompt, messages)

    # Parse Claude's output
    updated_state   = extract_tag(final_text, "updated_state")
    updated_history = extract_tag(final_text, "updated_history")
    report          = extract_tag(final_text, "report") or "No report generated."

    # If Claude did not return state/history tags, capture error and commit existing state for visibility.
    if not updated_state or not updated_history:
        print("ERROR: Claude did not return the expected XML output tags.")
        print("--- Raw output (first 3000 chars) ---")
        print(final_text[:3000])

        state_obj = json.loads(state_json)
        state_obj["scout_model_output"] = "Scout output unavailable due model error."
        state_obj["scout_error"] = "Missing <updated_state> or <updated_history> tags in Claude output."
        state_obj["scout_status"] = "unavailable"
        state_obj["scout_updated_at"] = datetime.now(timezone.utc).isoformat()
        state_obj["last_report"] = report

        draft_picks = state_obj.get("draft_picks", [])
        data_js = generate_data_js(
            history_json,
            draft_picks,
            state_obj["scout_model_output"],
            state_obj["scout_error"],
            state_obj["scout_status"],
            state_obj["scout_updated_at"],
        )
        updated_state = json.dumps(state_obj, indent=2)

        print("\n📤 Committing error state to GitHub for debugging...")
        commit_msg = f"NBA sim {phase} error: {today}"
        write_github_file(repo, "nba_sim_state.json", updated_state, commit_msg)
        write_github_file(repo, "data.js", data_js, commit_msg)
        print("✅ Error state committed. Exiting cleanly so deployment is successful.")
        return

    # Validate JSON before committing
    try:
        state_obj = json.loads(updated_state)
        json.loads(updated_history)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in Claude's output: {e}")
        state_obj = json.loads(state_json)
        state_obj["scout_model_output"] = "Scout output unavailable due invalid JSON from model."
        state_obj["scout_error"] = f"Invalid JSON in Claude output: {e}"
        state_obj["scout_status"] = "unavailable"
        state_obj["scout_updated_at"] = datetime.now(timezone.utc).isoformat()
        state_obj["last_report"] = report
        draft_picks = state_obj.get("draft_picks", [])
        data_js = generate_data_js(
            history_json,
            draft_picks,
            state_obj["scout_model_output"],
            state_obj["scout_error"],
            state_obj["scout_status"],
            state_obj["scout_updated_at"],
        )
        updated_state = json.dumps(state_obj, indent=2)
        commit_msg = f"NBA sim {phase} invalid-json: {today}"
        write_github_file(repo, "nba_sim_state.json", updated_state, commit_msg)
        write_github_file(repo, "data.js", data_js, commit_msg)
        print("✅ JSON error state committed. Exiting cleanly so deployment is successful.")
        return

    # Add model output and errors into state and data.js
    state_obj["scout_model_output"] = report
    state_obj["scout_error"] = ""
    state_obj["last_report"] = report
    updated_state = json.dumps(state_obj, indent=2)

    state_obj["scout_model_output"] = report
    state_obj["scout_error"] = ""
    state_obj["scout_status"] = "live"
    state_obj["scout_updated_at"] = datetime.now(timezone.utc).isoformat()
    state_obj["last_report"] = report
    updated_state = json.dumps(state_obj, indent=2)

    draft_picks = state_obj.get("draft_picks", []) if phase == "scout" else []
    data_js = generate_data_js(
        updated_history,
        draft_picks,
        state_obj["scout_model_output"],
        state_obj["scout_error"],
        state_obj["scout_status"],
        state_obj["scout_updated_at"],
    )

    # Commit everything to GitHub
    print("\n📤 Committing to GitHub...")
    commit_msg = f"NBA sim {phase}: {today}"
    write_github_file(repo, "nba_sim_state.json",   updated_state,   commit_msg)
    write_github_file(repo, "nba_sim_history.json", updated_history, commit_msg)
    write_github_file(repo, "data.js",              data_js,         commit_msg)

    print(f"\n✅ {phase.upper()} phase complete.\n")
    print("=" * 50)
    print(report)
    print("=" * 50)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in {"scout", "commit"}:
        print("Usage: python run.py [scout|commit]")
        sys.exit(1)
    try:
        run_phase(sys.argv[1])
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        print("Deployment execution encountered an unhandled exception. Exiting with code 0 to avoid scheduler retries.")
        sys.exit(0)
