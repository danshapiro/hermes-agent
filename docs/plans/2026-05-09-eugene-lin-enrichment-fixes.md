# Eugene Lin Enrichment Postmortem Fixes

> **For agentic workers:** REQUIRED SUB-SKILL: Use trycycle-executing to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 24 issues from the Eugene Lin Enrichment Postmortem that are addressable within the hermes-agent-yente codebase: subagent timeout diagnostics, skill content inheritance for subagents, toolsets documentation, subagent task-sizing guidance, gws message body and JSON-output control characters, and skill documentation gaps (jq, tasks subcommand, two-step Drive lookup, contacts unavailability, Drive binary files, DuckDuckGo Lite web search pattern, timeout/task-sizing advice, email-as-anchor).

**Architecture:** Six focused code changes across `tools/delegate_tool.py`, `run_agent.py`, `skills/productivity/google-workspace/scripts/google_api.py`, and `skills/productivity/google-workspace/SKILL.md`. The two most impactful fixes are: (1) always dump diagnostic logs on ANY subagent timeout (not just 0-API-call cases), and (2) propagate loaded skill content from parent agents to child subagents so they don't waste their time budget rediscovering CLI syntax. Skill inheritance uses agent-level tracking — `skill_view` results are stored on the AIAgent instance (via post-call tracking in `run_agent.py`), then `_build_child_agent()` reads from the parent and injects content into the child's system prompt. Only full-skill loads (not reference-file sub-views) are propagated; reference views starting with `"# Reference: ..."` are skipped. Skill content is truncated per-skill at a generous 8000 chars to survive Calendar/Drive/Tasks sections.

**Tech Stack:** Python 3.12, hermes-agent-yente codebase (AIAgent framework), Google Workspace skill scripts.

**Out of scope (require live server changes, separate service deploys, or upstream fixes):**
- Issues #6, #7: gws helper shortcuts and Google Contacts blocked by service broker (server config)
- Issues #2, #3: Granola/Nyne CLI installation (other repos/server config)
- Issue #4: msgvault SQLite FTS5 bug (separate service fix)
- Issues #11–#16: browser/CAPTCHA proxy configuration (browserbase/network config)

**Filed as Later Work (separate repo, larger scope):**
- Partial-output saving for timed-out subagents: the diagnostic dump already captures config, prompt/schema sizes, activity tracker snapshot, and thread stack. Capturing actual API call results/conversation turns requires buffering during subagent execution, which is a separate feature needing child-agent run-loop changes.
- Updating `researching-a-contact` skill in `/home/user/code/portable-skills/`: add subagent timeout/task-sizing caveat to Collector Pattern, note jq unavailability in Operational Read Discipline, and warn that email addresses alone are not useful public-web anchors. This skill lives in a different repo and needs its own worktree and deployment cycle.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `tools/delegate_tool.py:1122-1264` | `_dump_subagent_timeout_diagnostic` — accept `child_api_calls`, write context-sensitive Notes |
| `tools/delegate_tool.py:1479-1549` | `_run_single_child` timeout branch — remove `api_calls == 0` gate; dump for ALL timeouts |
| `tools/delegate_tool.py:834-960` | `_build_child_agent()` — inject inherited skill content from parent into child_prompt |
| `tools/delegate_tool.py:2382-2499` | `DELEGATE_TASK_SCHEMA` — clarify web vs search, add timeout/task-sizing guidance |
| `run_agent.py:1194-1195` | AIAgent `__init__` — initialize `_loaded_skill_content: Dict[str, str]` |
| `run_agent.py:9332-9415` | `_invoke_tool()` — track `skill_view` results on agent after dispatch |
| `run_agent.py:10097-10129` | Sequential tool paths — track `skill_view` results after `handle_function_call` |
| `skills/productivity/google-workspace/scripts/google_api.py:135-150` | `_extract_message_body()` — add `_sanitize_body()` helper, strip control chars |
| `skills/productivity/google-workspace/scripts/google_api.py:95-128` | `_run_gws()` — add control-character-safe parse recovery on decode error |
| `skills/productivity/google-workspace/SKILL.md:248-284` | Documentation updates — jq→Python, tasks CLI, Drive two-step, contacts, Drive binaries, DDG Lite, email-as-anchor, timeout guidance |
| `tests/tools/test_delegate_subagent_timeout_diagnostic.py:271-286` | Update existing test for non-zero-call diagnostic behavior |
| `tests/tools/test_delegate.py` | Add `test_child_inherits_parent_loaded_skills` |
| `tests/skills/test_google_workspace_api.py` | Add `test_extract_message_body_strips_control_characters` and `test_run_gws_recovers_from_control_characters` |

---

### Task 1: Dump subagent diagnostic on ALL timeouts (Issues #17, #6-fix)

**Files:**
- Modify: `tools/delegate_tool.py:1122-1264, 1479-1549`
- Modify: `tests/tools/test_delegate_subagent_timeout_diagnostic.py:271-286`
- Test: `tests/tools/test_delegate_subagent_timeout_diagnostic.py`

**Context:** Currently `_dump_subagent_timeout_diagnostic` is only called when `is_timeout and child_api_calls == 0` (line 1489). The postmortem subagents timed out with 20-21 API calls, producing a null `diagnostic_path`. The hardcoded Notes text says "written ONLY when a subagent times out with 0 API calls" — must be corrected. The existing test `test_nonzero_api_calls_skips_dump_and_uses_old_message` (line 271, class `TestRunSingleChildTimeoutDump`) asserts `diagnostic_path is None` for 5-API-call timeouts; it must be updated to expect a diagnostic. The error message at line 1532 says "likely stuck on a slow API call or unresponsive network request" — this misdiagnoses the real cause (task scope too large). The logger message at line 1499 says "0-API-call timeout — diagnostic written" — this must not hardcode "0-API-call".

- [ ] **Step 1: Run existing test to confirm current behavior**

```bash
pytest tests/tools/test_delegate_subagent_timeout_diagnostic.py::TestRunSingleChildTimeoutDump::test_nonzero_api_calls_skips_dump_and_uses_old_message -v
```
Expected: PASS (confirms current guard is active, diagnostic_path is None)

- [ ] **Step 2: Update existing test to expect diagnostic for non-zero-API-call timeouts**

In `tests/tools/test_delegate_subagent_timeout_diagnostic.py`, replace `test_nonzero_api_calls_skips_dump_and_uses_old_message` (lines 271-286) with:

```python
def test_nonzero_api_calls_dumps_diagnostic_and_reports_calls(self, hermes_home, monkeypatch):
    """When a subagent times out with API calls, we still get a diagnostic."""
    child = _StubChild(api_call_count=5, hang_seconds=10.0)
    result = self._invoke_with_short_timeout(child, monkeypatch)

    assert result["status"] == "timeout"
    assert result["api_calls"] == 5
    # Diagnostic SHOULD be written even for timeouts that made API calls
    assert result.get("diagnostic_path") is not None
    dump_path = Path(result["diagnostic_path"])
    assert dump_path.is_file()
    assert dump_path.parent == hermes_home / "logs"
    # Error message should surface the API call count, not "without making any"
    assert "with 5 API call(s) completed" in result["error"]
    # Error message should surface the real likely cause (task scope), not just "slow call"
    assert "task scope" in result["error"] or "timeout window" in result["error"]
    assert "Diagnostic:" in result["error"]
    assert str(dump_path) in result["error"]
```

- [ ] **Step 3: Run updated test to confirm it fails (pre-fix)**

```bash
pytest tests/tools/test_delegate_subagent_timeout_diagnostic.py::TestRunSingleChildTimeoutDump::test_nonzero_api_calls_dumps_diagnostic_and_reports_calls -v
```
Expected: FAIL (diagnostic_path is None because guard is still `api_calls == 0`)

- [ ] **Step 4: Remove the `api_calls == 0` gate, update diagnostic Notes, and fix error message**

**4a.** In `tools/delegate_tool.py`, change the condition at line 1489 from:

```python
if is_timeout and child_api_calls == 0:
```

to:

```python
if is_timeout:
```

**4b.** Update the diagnostic log Notes text at lines 1253-1257. Change from:

```python
_w("## Notes")
_w("  This file is written ONLY when a subagent times out with 0 API calls.")
_w("  0-API-call timeouts mean the child never reached its first LLM request.")
_w("  Common causes: oversized prompt rejected by provider, transport hang,")
_w("  credential resolution stuck. See issue #14726 for context.")
```

To:

```python
_w("## Notes")
if child_api_calls == 0:
    _w("  0-API-call timeout: the child never reached its first LLM request.")
    _w("  Common causes: oversized prompt rejected by provider, transport hang,")
    _w("  credential resolution stuck. See issue #14726 for context.")
else:
    _w(f"  {child_api_calls} API call(s) completed before timeout.")
    _w("  Common causes: task scope too large for timeout window, too many")
    _w("  sequential tool calls needed, or an external service was slow to")
    _w("  respond. See Eugene Lin enrichment postmortem for context.")
```

This requires passing `child_api_calls` into the dump function. At lines 1490-1497, add `child_api_calls=child_api_calls` to the call:

```python
diagnostic_path = _dump_subagent_timeout_diagnostic(
    child=child,
    task_index=task_index,
    timeout_seconds=float(child_timeout),
    duration_seconds=float(duration),
    worker_thread=_worker_thread_holder.get("t"),
    goal=goal,
    child_api_calls=child_api_calls,
)
```

Update the function signature at lines 1122-1129 to accept it:

```python
def _dump_subagent_timeout_diagnostic(
    *,
    child: Any,
    task_index: int,
    timeout_seconds: float,
    duration_seconds: float,
    worker_thread: Optional[threading.Thread],
    goal: str,
    child_api_calls: int = 0,
) -> Optional[str]:
```

Update the docstring at lines 1131-1141:

```python
"""Write a structured diagnostic dump for a subagent that timed out.

See issue #14726: users hit "subagent timed out after 300s with no response"
with zero API calls and no way to inspect what happened. Extended in the
Eugene Lin enrichment postmortem to cover ALL timeout cases, not just
0-API-call hangs.

Writes a dedicated log under ``~/.hermes/logs/subagent-<sid>-<ts>.log``
capturing the child's config, system-prompt / tool-schema sizes, activity
tracker snapshot, and the worker thread's Python stack at timeout.

When `child_api_calls` > 0, the Notes section explains the likely cause
is task scope exceeding the timeout window rather than a transport hang.

Returns the absolute path to the diagnostic file, or None on failure.
"""
```

**4c.** Fix the logger message at line 1499. Change from:

```python
logger.warning(
    "Subagent %d 0-API-call timeout — diagnostic written to %s",
    task_index,
    diagnostic_path,
)
```

To:

```python
logger.warning(
    "Subagent %d timeout (%d API calls) — diagnostic written to %s",
    task_index,
    child_api_calls,
    diagnostic_path,
)
```

**4d.** Fix the error message for non-zero-call timeouts at lines 1521-1538. Change from:

```python
if is_timeout:
    if child_api_calls == 0:
        _err = (
            f"Subagent timed out after {child_timeout}s without "
            f"making any API call — the child never reached its "
            f"first LLM request (prompt construction, credential "
            f"resolution, or transport may be stuck)."
        )
        if diagnostic_path:
            _err += f" Diagnostic: {diagnostic_path}"
    else:
        _err = (
            f"Subagent timed out after {child_timeout}s with "
            f"{child_api_calls} API call(s) completed — likely "
            f"stuck on a slow API call or unresponsive network request."
        )
else:
    _err = str(_timeout_exc)
```

To:

```python
if is_timeout:
    if child_api_calls == 0:
        _err = (
            f"Subagent timed out after {child_timeout}s without "
            f"making any API call — the child never reached its "
            f"first LLM request (prompt construction, credential "
            f"resolution, or transport may be stuck)."
        )
    else:
        _err = (
            f"Subagent timed out after {child_timeout}s with "
            f"{child_api_calls} API call(s) completed — likely "
            f"task scope too large for the timeout window. Reduce "
            f"the number of sources/identifiers per subagent or "
            f"split into more parallel tasks."
        )
    if diagnostic_path:
        _err += f" Diagnostic: {diagnostic_path}"
else:
    _err = str(_timeout_exc)
```

- [ ] **Step 5: Run updated test to verify it passes**

```bash
pytest tests/tools/test_delegate_subagent_timeout_diagnostic.py::TestRunSingleChildTimeoutDump::test_nonzero_api_calls_dumps_diagnostic_and_reports_calls -v
```
Expected: PASS

- [ ] **Step 6: Run full timeout diagnostic test suite**

```bash
pytest tests/tools/test_delegate_subagent_timeout_diagnostic.py -v
```
Expected: All pass (including updated test and the existing 0-API-call test at `test_zero_api_calls_writes_dump_and_surfaces_path`)

- [ ] **Step 7: Commit**

```bash
git add tools/delegate_tool.py tests/tools/test_delegate_subagent_timeout_diagnostic.py
git commit -m "fix(delegate): dump subagent timeout diagnostic for all timeouts, not just 0-API-call cases"
```

---

### Task 2: Propagate loaded skills from parent to child subagents (Issue #20)

**Files:**
- Modify: `run_agent.py` — add `_loaded_skill_content` attribute and `_track_loaded_skill` helper, call from all tool execution paths
- Modify: `tools/delegate_tool.py:834-960` — `_build_child_agent()` injects inherited skills into `child_prompt`
- Test: `tests/tools/test_delegate.py` — add skill inheritance test

**Context:** Subagents start with a blank slate — no loaded skills. The parent may have loaded gws-gmail, gws-calendar, etc. via `skill_view`, but the child must rediscover CLI syntax through trial and error, burning its time budget.

**Design decisions from findings review:**
- **Truncation**: 3000 chars is too aggressive — the google-workspace SKILL.md is ~5200 chars, and cutting at 3000 loses Calendar/Drive/Tasks syntax. Use 8000 chars per skill.
- **Reference file handling**: `skill_view("google-workspace", file_path="references/gmail-search-syntax.md")` returns reference content starting with `"# Reference: ..."` — do NOT overwrite the full SKILL.md content with reference sub-views. Check the result's `"file"` field: if present, skip storage (keep the previously-loaded full skill).
- **Race condition**: `_build_child_agent` writes to `child_prompt` synchronously on the main thread, and `_track_loaded_skill` writes to `_loaded_skill_content` from tool worker threads. Snapshot with `dict()` before iterating to avoid `RuntimeError: dictionary changed size during iteration`.

**Approach:**
1. Track loaded skill content on the AIAgent instance itself.
2. After every `skill_view` tool call, parse the result JSON; if successful AND no `"file"` field present (reference sub-view), store `{skill_name: skill_content}` on `self._loaded_skill_content`.
3. Track in all three tool execution paths: `_invoke_tool` (concurrent path) AND the two sequential `handle_function_call` paths at lines 10098 and 10118.
4. In `_build_child_agent()`, read from `parent_agent._loaded_skill_content` (with `dict()` snapshot), truncate to 8000 chars per skill, and inject into `child_prompt` before AIAgent construction.

- [ ] **Step 1: Write failing test for skill inheritance**

```python
# tests/tools/test_delegate.py — add to TestDelegateTask class or as module-level function
def test_child_inherits_parent_loaded_skills(self):
    """Child agent system prompt includes content of skills parent loaded."""
    parent = _make_mock_parent()
    parent._loaded_skill_content = {
        "gws-gmail": "# GWS Gmail\nUse `gws gmail users messages list --params '...'`\nCalendar syntax follows...\nDrive syntax follows...\n",
        "gws-calendar": "# GWS Calendar\nUse `gws calendar events list --params '...'`\n",
    }

    with patch("run_agent.AIAgent") as MockAgent:
        MockAgent.return_value = MagicMock()
        _build_child_agent(
            task_index=0,
            goal="Search gmail for eugene",
            context=None,
            toolsets=["terminal"],
            model=None,
            max_iterations=10,
            task_count=1,
            parent_agent=parent,
        )

    call_kwargs = MockAgent.call_args[1]
    prompt = call_kwargs.get("ephemeral_system_prompt", "")

    assert "GWS Gmail" in prompt
    assert "GWS Calendar" in prompt
    assert "gws gmail users messages list" in prompt
    assert "gws calendar events list" in prompt
    assert "Inherited Skills" in prompt
```

Run: `pytest tests/tools/test_delegate.py::TestDelegateTask::test_child_inherits_parent_loaded_skills -v`
Expected: FAIL (no skill content in child prompt)

- [ ] **Step 2: Confirm test fails**

```bash
pytest tests/tools/test_delegate.py::TestDelegateTask::test_child_inherits_parent_loaded_skills -v
```
Expected: FAIL

- [ ] **Step 3: Implement skill tracking on AIAgent**

**Part A: Initialize `_loaded_skill_content` in AIAgent.__init__**

In `run_agent.py`, after the subagent delegation state block (after line 1194, `self._active_children_lock = threading.Lock()`), add:

```python
# Loaded skill content tracking — populated when skill_view succeeds.
# Dict of {skill_name: markdown_content}. Used to propagate skill
# instructions to child subagents so they don't waste their time
# budget rediscovering CLI syntax and tool patterns.
# Only full-skill views (no "file" field in result) are stored;
# reference sub-views (e.g. references/gmail-search-syntax.md) are
# skipped to avoid overwriting the comprehensive main skill content.
self._loaded_skill_content: Dict[str, str] = {}
```

**Part B: Add `_track_loaded_skill` helper method on AIAgent**

Add a helper method to AIAgent (near other `_track_*` methods or after the `_invoke_tool` block):

```python
def _track_loaded_skill(self, function_name: str, result: str) -> None:
    """If *function_name* is skill_view and result indicates success,
    store the skill content on the agent for subagent inheritance.

    Only full-skill loads (main SKILL.md content) are stored.
    Reference-file sub-views (with "file" field in result JSON) are
    skipped — they are narrow reference snippets that would overwrite
    the comprehensive main skill content already stored.
    """
    if function_name != "skill_view":
        return
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict) and parsed.get("success"):
            # Skip reference sub-views — they have a "file" field
            # and contain narrow reference content (e.g. just
            # gmail-search-syntax.md), not the full SKILL.md.
            if parsed.get("file"):
                return
            name = parsed.get("name", "")
            content = parsed.get("content", "")
            if name and content:
                self._loaded_skill_content[name] = content
    except Exception:
        pass
```

**Part C: Call `_track_loaded_skill` from `_invoke_tool` (concurrent path)**

In `run_agent.py`, in `_invoke_tool` at line 9408-9415, change the else branch from:

```python
else:
    return handle_function_call(
        function_name, function_args, effective_task_id,
        tool_call_id=tool_call_id,
        session_id=self.session_id or "",
        enabled_tools=list(self.valid_tool_names) if self.valid_tool_names else None,
        skip_pre_tool_call_hook=True,
    )
```

To:

```python
else:
    result = handle_function_call(
        function_name, function_args, effective_task_id,
        tool_call_id=tool_call_id,
        session_id=self.session_id or "",
        enabled_tools=list(self.valid_tool_names) if self.valid_tool_names else None,
        skip_pre_tool_call_hook=True,
    )
    self._track_loaded_skill(function_name, result)
    return result
```

**Part D: Call `_track_loaded_skill` from sequential paths**

In `run_agent.py`, after line 10098 (within the `quiet_mode` try block):

```python
function_result = handle_function_call(
    function_name, function_args, effective_task_id,
    tool_call_id=tool_call.id,
    session_id=self.session_id or "",
    enabled_tools=list(self.valid_tool_names) if self.valid_tool_names else None,
    skip_pre_tool_call_hook=True,
)
_spinner_result = function_result
# Track loaded skills for subagent inheritance
self._track_loaded_skill(function_name, function_result)
```

After line 10118 (no-spinner path):

```python
function_result = handle_function_call(
    function_name, function_args, effective_task_id,
    tool_call_id=tool_call.id,
    session_id=self.session_id or "",
    enabled_tools=list(self.valid_tool_names) if self.valid_tool_names else None,
    skip_pre_tool_call_hook=True,
)
# Track loaded skills for subagent inheritance
self._track_loaded_skill(function_name, function_result)
```

Note: In the try/except blocks, add tracking BEFORE the except clause so it only runs on success. The exact insertion point is immediately after the `function_result = handle_function_call(...)` assignment.

**Part E: Inject inherited skills into child_prompt in `_build_child_agent`**

In `tools/delegate_tool.py`, after line 939 (after `child_prompt = _build_child_system_prompt(...)` returns but before `parent_api_key` extraction at line 941), add:

```python
# Inherit loaded skill content from parent so subagents don't
# waste their time budget rediscovering CLI syntax and tool patterns.
parent_loaded_skills = getattr(parent_agent, "_loaded_skill_content", None)
if parent_loaded_skills:
    skill_sections = []
    # Snapshot to avoid RuntimeError from concurrent tool-worker writes
    snapshot = dict(parent_loaded_skills)
    for skill_name, content in snapshot.items():
        truncated = content[:8000]
        if len(content) > 8000:
            truncated += "\n... [truncated]"
        skill_sections.append(
            f"## Skill: {skill_name}\n{truncated}"
        )
    if skill_sections:
        child_prompt += (
            "\n\n---\n"
            "## Inherited Skills\n"
            "The following skills were loaded by your parent agent. "
            "Use these instructions instead of rediscovering syntax "
            "from scratch:\n\n"
            + "\n\n".join(skill_sections)
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/tools/test_delegate.py::TestDelegateTask::test_child_inherits_parent_loaded_skills -v
```
Expected: PASS

- [ ] **Step 5: Run broader test suites to check for regressions**

```bash
pytest tests/tools/test_delegate.py -v -x --timeout=120
pytest tests/tools/test_skills_tool.py -v -x --timeout=60
```
Expected: All previously-passing tests still pass

- [ ] **Step 6: Commit**

```bash
git add tools/delegate_tool.py run_agent.py tests/tools/test_delegate.py
git commit -m "feat(delegate): propagate parent-loaded skill content to child subagents"
```

---

### Task 3: Clarify "web" vs "search" toolsets in delegate_task documentation (Issue #19)

**Files:**
- Modify: `tools/delegate_tool.py:2456-2478`

**Context:** The `delegate_task` toolsets parameter lists `'web'` and `'search'` as separate entries without explaining the difference. "web" includes `web_search` + `web_extract`; "search" includes only `web_search`. The ambiguity caused misconfiguration in the postmortem. No test changes needed — documentation-only.

- [ ] **Step 1: Verify current schema text**

```bash
python -c "from tools.delegate_tool import DELEGATE_TASK_SCHEMA; print(DELEGATE_TASK_SCHEMA['parameters']['properties']['toolsets']['description'])"
```
Expected: Output shows ambiguous listing without distinction between "web" and "search"

- [ ] **Step 2: Update schema descriptions**

In `tools/delegate_tool.py`:

**Line 2456-2463** (top-level `toolsets` description) — change from:

```python
"description": (
    "Toolsets to enable for this subagent. "
    "Default: inherits your enabled toolsets. "
    f"Available toolsets: {_TOOLSET_LIST_STR}. "
    "Common patterns: ['terminal', 'file'] for code work, "
    "['web'] for research, ['browser'] for web interaction, "
    "['terminal', 'file', 'web'] for full-stack tasks."
),
```

To:

```python
"description": (
    "Toolsets to enable for this subagent. "
    "Default: inherits your enabled toolsets. "
    f"Available toolsets: {_TOOLSET_LIST_STR}. "
    "IMPORTANT: 'web' = web_search + web_extract (full web research and scraping). "
    "'search' = web_search ONLY (no scraping, quick lookups). "
    "Common patterns: ['terminal', 'file'] for code work, "
    "['web'] for research, ['search'] for quick lookups, "
    "['browser'] for interactive web navigation."
),
```

**Line 2478** (per-task `toolsets` description) — change from:

```python
"description": f"Toolsets for this specific task. Available: {_TOOLSET_LIST_STR}. Use 'web' for network access, 'terminal' for shell, 'browser' for web interaction.",
```

To:

```python
"description": f"Toolsets for this specific task. Available: {_TOOLSET_LIST_STR}. 'web'=search+extract, 'search'=search only. Use 'web' for research, 'search' for quick lookups, 'terminal' for shell, 'browser' for web interaction.",
```

- [ ] **Step 3: Verify the change loads correctly**

```bash
python -c "from tools.delegate_tool import DELEGATE_TASK_SCHEMA; desc = DELEGATE_TASK_SCHEMA['parameters']['properties']['toolsets']['description']; assert 'web_search + web_extract' in desc; assert 'search only' in desc; print('OK')"
```
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add tools/delegate_tool.py
git commit -m "docs(delegate): clarify difference between 'web' and 'search' toolsets"
```

---

### Task 4: Add subagent timeout and task-sizing guidance to delegate_task schema (Issues #18, #21)

**Files:**
- Modify: `tools/delegate_tool.py:2384-2432`

**Context:** The delegate_task help text doesn't mention the 600-second timeout or provide guidance on how to size tasks for reliable completion within that window. Subagents burned their full 10-minute budget without completing. No test changes needed — documentation-only.

- [ ] **Step 1: Verify current schema lacks timeout guidance**

```bash
python -c "from tools.delegate_tool import DELEGATE_TASK_SCHEMA; print(DELEGATE_TASK_SCHEMA['description'])"
```
Expected: No mention of timeout or task-sizing guidance

- [ ] **Step 2: Add timeout and task-sizing guidance**

In `tools/delegate_tool.py`, in the `DELEGATE_TASK_SCHEMA["description"]` string (lines 2384-2432), after the line "- Results are always returned as an array, one entry per task." (line 2432), replace the closing `)` on that line with a continuing string:

Change line 2432 from:
```python
        "- Results are always returned as an array, one entry per task."
```

To:
```python
        "- Results are always returned as an array, one entry per task.\n"
```

Then add before the closing `)` on line 2433:

```python
        "TIMEOUT AND TASK SIZING:\n"
        "- Subagents time out after 600s (10 min) by default. Plan tasks accordingly.\n"
        "- Single-source-family, single-identifier tasks complete reliably. "
        "Multi-family, multi-identifier tasks do not.\n"
        "- Example of good sizing: 'Search Gmail for emails from alice@example.com' "
        "(single source, single query) — completes in ~2-4 min.\n"
        "- Example of bad sizing: 'Search all sources for Alice across 5 email addresses' "
        "(multi-source, multi-identifier) — will time out.\n"
        "- When you have multiple sources or identifiers, use the 'tasks' array "
        "to dispatch parallel subagents, one per source/identifier.\n"
        "- If a subagent reports timeout, reduce scope and retry with fewer sources."
```

Note: The key fix from the planning review is ensuring line 2432 ends with `\n` so `"TIMEOUT AND TASK SIZING:\n"` starts on its own line instead of being glued to the end of the previous line as `one entry per task.TIMEOUT AND TASK SIZING:`.

- [ ] **Step 3: Verify schema loads and contains new text**

```bash
python -c "from tools.delegate_tool import DELEGATE_TASK_SCHEMA; assert 'TIMEOUT' in DELEGATE_TASK_SCHEMA['description']; assert 'Single-source-family' in DELEGATE_TASK_SCHEMA['description']; assert 'one entry per task.TIMEOUT' not in DELEGATE_TASK_SCHEMA['description']; print('OK')"
```
Expected: OK

- [ ] **Step 4: Run delegate tests to ensure no schema breakage**

```bash
pytest tests/tools/test_delegate.py::TestDelegateRequirements -v -x --timeout=60
```
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add tools/delegate_tool.py
git commit -m "docs(delegate): add timeout and task-sizing guidance to delegate_task schema"
```

---

### Task 5: Sanitize control characters in gws message body extraction and JSON parsing (Issues #9, #16)

**Files:**
- Modify: `skills/productivity/google-workspace/scripts/google_api.py:95-128, 135-150`
- Test: `tests/skills/test_google_workspace_api.py`

**Context:** Two related control-character problems exist in the gws pipeline:

1. **`_extract_message_body` (postmortem #9):** After base64 decoding a message body, the resulting text can contain raw ASCII control characters (0x00-0x1F except \t, \n, \r). These are valid Unicode (not replacement characters from `errors="replace"`) and can break downstream JSON serialization when the body is re-encoded into the agent's output.

2. **`_run_gws` JSON parsing (finding #16):** The gws CLI may emit raw control characters in its stdout JSON. When this happens, `json.loads(stdout)` at line 124 raises `JSONDecodeError` and the entire operation fails. The fix adds a control-character-stripping recovery parse before giving up.

Both fixes use a shared `_sanitize_body` helper that strips ASCII control characters except \t, \n, \r.

**Test pattern:** The existing `test_google_workspace_api.py` uses `importlib.util.spec_from_file_location` via the `api_module` fixture to load the module (there's no `__init__.py` under `skills/productivity/google-workspace/`). New tests must follow this same pattern using the `api_module` fixture — do NOT use direct imports. Functions are at module level (pytest-style), not methods — do NOT use `self` parameter.

- [ ] **Step 1: Write failing tests for control character sanitization**

Add these tests to `tests/skills/test_google_workspace_api.py`:

```python
def test_extract_message_body_strips_control_characters(api_module):
    """Body text with control characters should be sanitized."""
    import base64

    raw_body = "Hello\x00\x01\x02\x03World\x1b\x1c\x1d\x1e\x1f!"
    encoded = base64.urlsafe_b64encode(raw_body.encode("utf-8")).decode("ascii")

    msg = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": encoded},
        }
    }

    result = api_module._extract_message_body(msg)
    assert "\x00" not in result
    assert "\x01" not in result
    assert "\x1b" not in result
    assert "Hello" in result
    assert "World!" in result

    # Tabs, newlines, carriage returns should be preserved
    raw_body2 = "Line1\tindented\nLine2\r\n"
    encoded2 = base64.urlsafe_b64encode(raw_body2.encode("utf-8")).decode("ascii")
    msg2 = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": encoded2},
        }
    }
    result2 = api_module._extract_message_body(msg2)
    assert "\t" in result2
    assert "\n" in result2


def test_run_gws_recovers_from_json_control_characters(api_module, monkeypatch):
    """json.loads should recover when stdout contains control characters."""
    import subprocess as _subprocess

    # Simulate gws emitting JSON with a raw control char in a string value
    dirty_json = '{"result": "hello\x1bworld"}'
    mock_result = _subprocess.CompletedProcess(
        args=["gws", "test"],
        returncode=0,
        stdout=dirty_json,
        stderr="",
    )
    monkeypatch.setattr(api_module.subprocess, "run", lambda *a, **kw: mock_result)

    result = api_module._run_gws(["gmail", "users", "messages", "get"], params={"id": "msg1"})
    assert result["result"] == "helloworld"
```

Run: `pytest tests/skills/test_google_workspace_api.py::test_extract_message_body_strips_control_characters tests/skills/test_google_workspace_api.py::test_run_gws_recovers_from_json_control_characters -v`
Expected: Both FAIL (control characters present in output / JSONDecodeError)

- [ ] **Step 2: Confirm tests fail**

```bash
pytest tests/skills/test_google_workspace_api.py::test_extract_message_body_strips_control_characters -v
pytest tests/skills/test_google_workspace_api.py::test_run_gws_recovers_from_json_control_characters -v
```
Expected: FAIL

- [ ] **Step 3: Add control character sanitization**

In `skills/productivity/google-workspace/scripts/google_api.py`, add a shared helper before `_extract_message_body`:

```python
def _sanitize_body(text: str) -> str:
    """Remove ASCII control characters except tab, newline, carriage return."""
    return ''.join(
        c for c in text
        if c in ('\t', '\n', '\r') or ord(c) >= 0x20
    )
```

Modify `_extract_message_body` (line 135) to call `_sanitize_body` before returning. Change the final `return body` at line 150 to:

```python
    return _sanitize_body(body)
```

Modify `_run_gws` (line 123-128) to add control-character recovery on parse failure. Change from:

```python
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        print("ERROR: Unexpected non-JSON output from gws:", file=sys.stderr)
        print(stdout, file=sys.stderr)
        sys.exit(1)
```

To:

```python
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # gws CLI may emit raw control characters in JSON string values
        # that break Python's json parser. Strip them and retry once.
        cleaned = _sanitize_body(stdout)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            print("ERROR: Unexpected non-JSON output from gws:", file=sys.stderr)
            print(stdout, file=sys.stderr)
            sys.exit(1)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/skills/test_google_workspace_api.py::test_extract_message_body_strips_control_characters -v
pytest tests/skills/test_google_workspace_api.py::test_run_gws_recovers_from_json_control_characters -v
```
Expected: PASS

- [ ] **Step 5: Run broader google workspace tests**

```bash
pytest tests/skills/test_google_workspace_api.py -v -x --timeout=60
```
Expected: All previously-passing tests still pass

- [ ] **Step 6: Commit**

```bash
git add skills/productivity/google-workspace/scripts/google_api.py tests/skills/test_google_workspace_api.py
git commit -m "fix(gws): strip ASCII control characters from message bodies and recover on JSON parse errors"
```

---

### Task 6: Update google-workspace skill documentation (Issues #1, #5, #8, #10, #15, #21, #22, #23, #24)

**Files:**
- Modify: `skills/productivity/google-workspace/SKILL.md`

**Context:** The skill documentation has several gaps discovered in the postmortem:
- (a) recommends `jq` but jq isn't in the agent container (#5, #22)
- (b) doesn't document the gws tasks CLI subcommand (#8)
- (c) doesn't document the two-step Drive file lookup (#10)
- (d) doesn't document contacts unavailability (#23)
- (e) Drive binary files unreadable (#24)
- (f) no DuckDuckGo Lite web search fallback pattern when `web_search` tool is unavailable (#1)
- (g) no subagent timeout/task-sizing guidance for agents doing gws work in subagents (#21)
- (h) no warning that email addresses are not useful public-web anchors (#15)

- [ ] **Step 1: Check existing tests pass as baseline**

```bash
pytest tests/skills/test_google_workspace_credential_files.py -v
pytest tests/skills/test_google_oauth_setup.py -v
```
Expected: Existing tests pass

- [ ] **Step 2: Apply all documentation updates**

Make the following changes in `skills/productivity/google-workspace/SKILL.md`:

**A. Replace jq references with Python (issues #5, #22):**

At line 250, change:
```markdown
All commands return JSON. Parse with `jq` or read directly. Key fields:
```
To:
```markdown
All commands return JSON. Parse with Python (`json.loads` or pipe through
`python -c "import sys,json; data=json.load(sys.stdin); ..."`) — do NOT use `jq`
as it is not installed in the agent container. Key fields:
```

**B. Document tasks subcommand structure (issue #8):**

After the Docs section (after line 246, before "## Output Format"), add:

```markdown
### Tasks

The gws tasks CLI uses a `tasklists` subcommand (NOT `lists`):

```bash
# List task lists (CORRECT)
gws tasks tasklists list

# INCORRECT — do not use:
# gws tasks lists list  # returns "unrecognized subcommand 'lists'"
```

Note: If `gws` is not installed, fall back to the Python API path via `$GAPI`.
```

**C. Document two-step Drive lookup (issue #10):**

After the existing Drive section (the `$GAPI drive search "mimeType=..." --raw-query` line, around line 221), add:

```markdown
### Drive: Two-Step File Lookup

Drive file lookup by name requires two steps — `files get` does NOT accept
file names, only file IDs:

```bash
# Step 1: Search by name to get the file ID
$GAPI drive search "Tokyo Splits" --max 10
# Returns: [{id: "abc123", name: "Tokyo Splits", ...}]

# Step 2: Get file contents with the ID
$GAPI drive get abc123
```

Do NOT try `$GAPI drive get --params '{"fileId": "Tokyo Splits"}'` — it returns 404.
```

**D. Document contacts unavailability (issue #23):**

After the Contacts section (around line 227), add:

```markdown
> **Note:** The `gws people search contacts` command may return `policy_denied`
> in some environments due to service broker configuration. When the broker
> denies contacts access, skip the contacts-enrichment source family entirely.
```

**E. Document Drive binary files unreadable (issue #24):**

After the Drive: Two-Step File Lookup section added above, add:

```markdown
### Drive: Binary Files

Files with `application/octet-stream` or unknown MIME types cannot be inspected
by any agent tool. These are typically binary blobs (compiled executables,
encrypted archives, or proprietary formats). When a Drive search returns such
files, note them in the briefing as "found but unreadable" and move on — do not
retry the lookup.
```

**F. Document DuckDuckGo Lite web search fallback (issue #1):**

After the "Rules" section (line 268), add a new section:

```markdown
## Web Search Fallback

When the `web_search` tool is unavailable (postmortem #1) or search engines
block with CAPTCHAs, use DuckDuckGo Lite (HTML-only endpoint, no JavaScript,
no CAPTCHA). Parse results from the raw HTML with Python regex.

```bash
curl -sL "https://lite.duckduckgo.com/lite?q=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' 'search terms here')"
```

The HTML uses single quotes for attributes. Use regex that handles both
single-quote and double-quote attribute styles:

```python
import re, subprocess, urllib.parse
query = "eugene lin software engineer"
url = f"https://lite.duckduckgo.com/lite?q={urllib.parse.quote(query)}"
result = subprocess.run(["curl", "-sL", url], capture_output=True, text=True)
links = re.findall(r"<a\s+class=['\"]result-link['\"][^>]*>([^<]+)<\/a>", result.stdout)
snippets = re.findall(r"<td\s+class=['\"]result-snippet['\"][^>]*>([^<]+)<\/td>", result.stdout)
```

> **Note:** Email addresses alone (e.g. `eugene_lin@hotmail.com`) return zero
> results on DuckDuckGo and other search engines. Do not use email addresses
> as public-web search anchors — use names, handles, company names, or other
> identifying information instead. (postmortem #15)
```

**G. Document subagent timeout guidance (issue #21):**

In the "Rules" section (after line 267), add rule 6:

```markdown
6. **Subagent timeout awareness** — subagents dispatched via `delegate_task` time out after 600s (10 min). Size subagent tasks accordingly: single-source, single-identifier tasks (e.g., "Search Gmail for alice@example.com") complete reliably in ~2-4 min. Multi-family, multi-identifier tasks will time out. When you have many sources, dispatch parallel single-source subagents instead of one monolithic task.
```

- [ ] **Step 3: Verify no tests break**

```bash
pytest tests/skills/test_google_workspace_credential_files.py tests/skills/test_google_oauth_setup.py -v
```
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add skills/productivity/google-workspace/SKILL.md
git commit -m "docs(gws): update skill docs — jq→Python, tasks CLI, Drive two-step lookup, contacts/Drive-binary unavailability, DDG Lite fallback, email-as-anchor warning, subagent timeout guidance"
```

---

### Task 7: End-to-end verification

**Files:** All modified files

- [ ] **Step 1: Run full test suite for modified areas (failures must be investigated, not masked)**

```bash
pytest tests/tools/test_delegate.py tests/tools/test_delegate_subagent_timeout_diagnostic.py -v -x --timeout=120
```
Expected: All pass

```bash
pytest tests/tools/test_skills_tool.py -v -x --timeout=60
```
Expected: All pass

```bash
pytest tests/skills/test_google_workspace_api.py tests/skills/test_google_workspace_credential_files.py tests/skills/test_google_oauth_setup.py -v -x --timeout=60
```
Expected: All pass

If any test fails, investigate and fix the root cause — do NOT mask failures with `|| true` or `2>/dev/null`. A red suite means the implementation is not done.

- [ ] **Step 2: Run Python import check**

```bash
python -c "from tools.delegate_tool import delegate_task, DELEGATE_TASK_SCHEMA, _build_child_agent; print('delegate_tool OK')"
python -c "from tools.skills_tool import skill_view, skills_list; print('skills_tool OK')"
python -c "import run_agent; print('run_agent import OK')"
```
Expected: All imports succeed with zero errors on stderr

- [ ] **Step 3: Check that all modified files are committed**

```bash
git status
```
Expected: working tree clean (only the plan file may show as modified)

- [ ] **Step 4: Commit verification (if any remaining changes)**

```bash
git add -A
git diff --cached --stat
```
If changes remain, commit with:
```bash
git commit -m "chore: final verification of Eugene Lin enrichment fixes"
```
Otherwise, confirm clean working tree.
