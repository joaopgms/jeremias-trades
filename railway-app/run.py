#!/usr/bin/env python3
"""
NBA Betting Sim — Railway runner
Handles scout (14:00 PT) and commit phases, including dynamic commit trigger.

Usage:
    python run.py scout
    python run.py commit
    python run.py commit_if_ready
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
import requests
from datetime import timedelta


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


def get_first_nba_game_start_utc() -> datetime | None:
    """Fetch today's first NBA game start in UTC using ESPN scoreboards."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={today}"
    try:
        res = requests.get(url, timeout=12)
        res.raise_for_status()
        data = res.json()
        events = data.get("events", [])
        starts = []
        for ev in events:
            start = ev.get("date")
            if not start:
                continue
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                starts.append(dt)
            except Exception:
                continue
        if not starts:
            return None
        return min(starts)
    except Exception as e:
        print(f"Could not fetch NBA schedule: {e}")
        return None


def run_commit_if_ready() -> None:
    repo = get_repo()
    state_json = read_github_file(repo, "nba_sim_state.json")
    first_game_time = None
    if state_json:
        try:
            state_obj = json.loads(state_json)
            first_game_time = state_obj.get("first_game_time")
        except Exception:
            first_game_time = None

    if first_game_time:
        try:
            first_game = datetime.fromisoformat(first_game_time.replace("Z", "+00:00"))
        except Exception:
            first_game = None
    else:
        first_game = get_first_nba_game_start_utc()

    if not first_game:
        print("No NBA game schedule found for today. Skipping dynamic commit run.")
        return

    commit_trigger = first_game - timedelta(minutes=15)
    now = datetime.now(timezone.utc)
    print(f"First NBA game (UTC): {first_game.isoformat()}")
    print(f"Dynamic commit trigger (UTC): {commit_trigger.isoformat()}")

    if now < commit_trigger:
        print("Not yet time for dynamic commit. Wait until 15 minutes before first game.")
        return

    print("Dynamic commit window reached — running commit phase now.")
    run_phase("commit")


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


def sanitize_error_text(err: str) -> str:
    if not err:
        return ""
    text = str(err).strip()
    # Remove excessive newlines and whitespace
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    # If JSON-like string, extract the most likely message field
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            if parsed.get("message"):
                return str(parsed["message"]).strip()
            if parsed.get("error") and isinstance(parsed["error"], dict):
                if parsed["error"].get("message"):
                    return str(parsed["error"]["message"]).strip()
            if parsed.get("error") and isinstance(parsed["error"], str):
                return parsed["error"].strip()
    except Exception:
        pass

    # Normalize common embedded messages
    for pat in [r"Your credit balance is too low.*", r"Missing <updated_state> or <updated_history> tags.*", r"Invalid JSON in Claude output: .*"]:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return m.group(0).strip()

    # fallback: if there is a quoted message text, return that
    m = re.search(r"['\"](Your .*? error.*?|.*? message.*?)[\"']", text)
    if m:
        return m.group(1).strip()

    return text

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
    first_game_time = extract_tag(final_text, "first_game_time")
    report          = extract_tag(final_text, "report") or "No report generated."

    # If Claude did not return state/history tags, capture error and commit existing state for visibility.
    if not updated_state or not updated_history:
        print("ERROR: Claude did not return the expected XML output tags.")
        print("--- Raw output (first 3000 chars) ---")
        print(final_text[:3000])

        state_obj = json.loads(state_json)
        state_obj["scout_model_output"] = "Scout output unavailable due model error."
        state_obj["scout_error"] = sanitize_error_text("Missing <updated_state> or <updated_history> tags in Claude output.")
        state_obj["scout_status"] = "unavailable"
        state_obj["scout_updated_at"] = datetime.now(timezone.utc).isoformat()
        if first_game_time:
            state_obj["first_game_time"] = first_game_time
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
        state_obj["scout_error"] = sanitize_error_text(f"Invalid JSON in Claude output: {e}")
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
    state_obj["scout_status"] = "live"
    state_obj["scout_updated_at"] = datetime.now(timezone.utc).isoformat()
    if first_game_time:
        state_obj["first_game_time"] = first_game_time
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
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    if mode not in {"scout", "commit", "commit_if_ready"}:
        print("Usage: python run.py [scout|commit|commit_if_ready]")
        sys.exit(1)
    try:
        if mode == "commit_if_ready":
            run_commit_if_ready()
        else:
            run_phase(mode)
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        print("Deployment execution encountered an unhandled exception. Exiting with code 0 to avoid scheduler retries.")
        sys.exit(0)
