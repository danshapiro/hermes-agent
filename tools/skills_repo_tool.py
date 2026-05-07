"""Skills Repo Tool -- Agent-Managed Git Operations on the Portable Skills Repo.

Allows the agent to view, stage, commit, push, pull, and create skills in the
shared portable-skills git repository at ``skills.repo_dir`` (config.yaml).

Actions:
  status       -- Show modified/new/deleted files (git status --porcelain)
  diff         -- Show unstaged or staged changes (git diff / git diff --cached)
  stage        -- Stage specific skill files for commit (git add <path>)
  commit       -- Commit staged changes with a message (restricted to skills/)
  push         -- Push committed changes to origin
  pull         -- Fast-forward pull from origin + invalidate skills prompt cache
  log          -- Recent commit history (git log --oneline)
  create       -- Create a new repo-backed skill, validate frontmatter, stage it

Stage, commit(files=...), and create are restricted to paths under skills/.
Push returns a clear error if SSH auth is not configured.
Pull is fast-forward only; merge conflicts surface for manual resolution.
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home, display_hermes_home

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")  # category: subdirectories only, no path separators
_FRONTMATTER_NAME_RE = re.compile(r"^name\s*:\s*(.+)$", re.MULTILINE)
_FM_NAME_CLEAN_RE = re.compile(r'(?:\s|^)#(?:[^{}\'"\\]*(?:(?:\"[^"]*\")|(?:\'[^\']*\'))?[^{}\'"\\]*)*$')  # unused now: kept for reference
_MAX_CONTENT_SIZE = 256 * 1024  # 256 KiB


def _find_repo_dir() -> Optional[Path]:
    """Resolve the skills repo directory from config ``skills.repo_dir`` only.

    ``skills.external_dirs`` are read-only discovery inputs and must not be
    treated as writable repo selectors.
    """
    try:
        from hermes_cli.config import cfg_get, load_config
        cfg = load_config()
        repo_dir = cfg_get(cfg, "skills", "repo_dir")
        if repo_dir:
            raw = str(repo_dir)
            expanded = os.path.expanduser(os.path.expandvars(raw))
            path = Path(expanded)
            if not path.is_absolute():
                from hermes_constants import get_hermes_home
                path = (get_hermes_home() / path)
            path = path.resolve()
            if (path / ".git").exists():
                return path
    except Exception:
        pass

    return None


def _run_git(args: List[str], repo_dir: Path,
             timeout: int = _GIT_TIMEOUT) -> tuple:
    """Run a git command. Returns (ok, stdout, stderr)."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Clear env vars that could override per-repo git config.
    # Do NOT clear GIT_SSH_COMMAND — it is the canonical credential path.
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    try:
        result = subprocess.run(
            ["git"] + list(args),
            capture_output=True, text=True, timeout=timeout,
            env=env, cwd=str(repo_dir),
        )
        ok = result.returncode == 0
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if not ok:
            logger.error("git %s failed (rc=%d): %s",
                         " ".join(args), result.returncode, stderr)
        return ok, stdout, stderr
    except subprocess.TimeoutExpired:
        return False, "", f"git timed out after {timeout}s: {' '.join(args)}"
    except FileNotFoundError:
        return False, "", "git not found"
    except Exception as exc:
        return False, "", str(exc)


def _resolve_repo_dir() -> Path:
    """Resolve repo dir, raising a user-friendly error if not found."""
    d = _find_repo_dir()
    if d is None:
        raise RuntimeError(
            "skills_repo: no git repo found. Set skills.repo_dir in config.yaml."
        )
    return d


def _validate_skill_name(name: str) -> Optional[str]:
    """Validate skill name; returns error string or None."""
    if not name or not name.strip():
        return "skill name is required"
    name = name.strip()
    if len(name) < 2:
        return f"skill name too short: {name!r} (minimum 2 characters)"
    if len(name) > 64:
        return f"skill name too long: {name!r} (maximum 64 characters)"
    if not _SKILL_NAME_RE.match(name):
        return f"invalid skill name: {name!r} (use lowercase letters, digits, and hyphens)"
    return None


def _validate_path(repo_dir: Path, rel_path: str) -> Optional[str]:
    """Validate a path is under skills/ in the repo. Returns error or None."""
    full = (repo_dir / rel_path).resolve()
    skills_dir = (repo_dir / "skills").resolve()
    try:
        full.relative_to(skills_dir)
    except ValueError:
        return f"path {rel_path!r} is outside skills/ directory"
    return None


def _validate_frontmatter(content: str) -> Optional[str]:
    """Check that content has basic YAML frontmatter with a 'name' field.

    Only inspects the first frontmatter block (between the opening --- and
    the closing ---). Body content after the closing delimiter is ignored.
    Uses the same start/end rules as agent/skill_utils.py parse_frontmatter.
    """
    if not content.startswith("---"):
        return (
            "content must start with YAML frontmatter (---) exactly at the "
            "beginning of the file containing at least a 'name:' field"
        )
    fm = _split_frontmatter(content)
    if fm is None:
        return "frontmatter block is missing or has no closing --- on its own line"
    if not fm.strip():
        return "frontmatter block is empty"
    if not _FRONTMATTER_NAME_RE.search(fm):
        return "frontmatter must contain a 'name:' field"
    return None


def _validate_content_size(content: str) -> Optional[str]:
    """Check content size against the maximum."""
    size = len(content.encode("utf-8"))
    if size > _MAX_CONTENT_SIZE:
        return (
            f"content too large: {size} bytes (max {_MAX_CONTENT_SIZE})"
        )
    return None


def _security_scan_skill(skill_dir: Path) -> Optional[str]:
    """Run security scan on a new skill. Returns error string or None.

    No-op when skills.guard_agent_created is disabled (the default).
    """
    try:
        from tools.skill_manager_tool import _guard_agent_created_enabled
        from tools.skills_guard import scan_skill, should_allow_install, format_scan_report
    except ImportError:
        return None
    if not _guard_agent_created_enabled():
        return None
    try:
        result = scan_skill(skill_dir, source="agent-created")
        allowed, reason = should_allow_install(result)
        if allowed is False:
            report = format_scan_report(result)
            return f"Security scan blocked this skill ({reason}):\n{report}"
        if allowed is None:
            report = format_scan_report(result)
            logger.warning("Agent-created skill blocked (dangerous findings): %s", reason)
            return f"Security scan blocked this skill ({reason}):\n{report}"
    except Exception as exc:
        logger.warning("skipping security scan: %s", exc)
    return None


def _split_frontmatter(content: str) -> Optional[str]:
    """Return the frontmatter body (between opening/closing ---) or None.

    Matches the canonical parser in agent/skill_utils.py parse_frontmatter.
    Requires --- as the very first characters and --- on its own line to close.
    """
    if not content.startswith("---"):
        return None
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return None
    return content[3:end_match.start() + 3].strip()


def _normalize_frontmatter_name(raw_name: str) -> str:
    """Normalize a frontmatter name value, stripping YAML quotes and comments."""
    raw = raw_name.strip()
    # If quoted, extract content between quotes; comments after closing quote
    # are implicitly excluded by the YAML parser.
    if len(raw) >= 2 and raw[0] in ('"', "'"):
        # Find the matching closing quote (skip escaped quotes)
        quote = raw[0]
        end = 1
        while end < len(raw):
            end = raw.find(quote, end)
            if end == -1:
                break
            # Check for escaped quote (\" or '')
            if end > 0 and (raw[end - 1] == '\\' or (quote == "'" and end + 1 < len(raw) and raw[end + 1] == "'")):
                end += 1
                continue
            break
        if end != -1:
            return raw[1:end].strip()
        # Unclosed quote: treat as raw
    # Unquoted: strip trailing comments (# preceded by whitespace)
    raw = re.sub(r'(\s)#\s.*$', r'\1', raw).strip()
    # Handle # at position 0: strip everything from # to end
    raw = re.sub(r'^#.*$', '', raw).strip()
    return raw


def _extract_frontmatter_name_from_content(content: str) -> Optional[str]:
    """Extract the 'name:' field from the first frontmatter block in raw SKILL.md content."""
    fm = _split_frontmatter(content)
    if fm is None:
        return None
    m = _FRONTMATTER_NAME_RE.search(fm)
    if m:
        return _normalize_frontmatter_name(m.group(1))
    return None


def _extract_frontmatter_name(skill_dir: Path) -> Optional[str]:
    """Extract the 'name:' field from SKILL.md frontmatter, or None."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        content = skill_md.read_text(encoding="utf-8")
    except Exception:
        return None
    fm = _split_frontmatter(content)
    if fm is None:
        return None
    m = _FRONTMATTER_NAME_RE.search(fm)
    if m:
        return _normalize_frontmatter_name(m.group(1))
    return None


def _scan_all_skill_names(repo_dir: Path) -> Dict[str, Path]:
    """Scan all SKILL.md files in skills/ and return {frontmatter_name: dir_path}."""
    names = {}
    skills_root = repo_dir / "skills"
    if not skills_root.is_dir():
        return names
    for skill_md in skills_root.rglob("SKILL.md"):
        fm_name = _extract_frontmatter_name(skill_md.parent)
        if fm_name:
            names[fm_name] = skill_md.parent
    return names


def _validate_category(category: str) -> Optional[str]:
    """Validate category name; returns error string or None."""
    if not category or not category.strip():
        return None  # empty category is fine (means no subdirectory)
    category = category.strip()
    if len(category) < 1:
        return f"category name too short: {category!r}"
    if len(category) > 64:
        return f"category name too long: {category!r} (maximum 64 characters)"
    if not _CATEGORY_RE.match(category):
        return f"invalid category: {category!r} (use lowercase letters, digits, and hyphens; no path separators)"
    return None


def _check_name_unique_in_repo(repo_dir: Path, name: str,
                                category: str = "",
                                frontmatter_name: Optional[str] = None) -> Optional[str]:
    """Check that name does not collide with an existing skill in the repo.

    Checks path-based collisions (same directory), directory-basename
    collisions across categories, and frontmatter-name collisions
    (different directory but same 'name:' in YAML frontmatter).
    When frontmatter_name differs from the directory name, both are checked.
    """
    # Path-based collision check
    if category:
        skill_path = repo_dir / "skills" / category / name
        flat_path = repo_dir / "skills" / name
        if skill_path.is_dir():
            return f"skill {name!r} already exists at skills/{category}/{name}/"
        if flat_path.is_dir():
            return f"skill {name!r} exists at skills/{name}/ (category mismatch — choose a different name or category)"
    else:
        skill_path = repo_dir / "skills" / name
        if skill_path.is_dir():
            return f"skill {name!r} already exists in the repo at skills/{name}/"

    # Directory basename collision — only for directories that contain SKILL.md.
    # Category/support folders without SKILL.md are not skills.
    skills_root = repo_dir / "skills"
    if skills_root.is_dir():
        for skill_md in skills_root.rglob("SKILL.md"):
            d = skill_md.parent
            if d.name != name:
                continue
            # Skip the exact target path (already rejected by path check above)
            if category and d == skills_root / category / name:
                continue
            if not category and d == skills_root / name:
                continue
            rel_d = d.relative_to(repo_dir)
            return (
                f"skill {name!r} already exists at {rel_d}/ "
                f"(directory basename collision; choose a unique name)"
            )

    # Frontmatter-name collision check (different directory, same 'name:' in frontmatter)
    existing = _scan_all_skill_names(repo_dir)
    check_names = {name}
    if frontmatter_name and frontmatter_name != name:
        check_names.add(frontmatter_name)
    for n in check_names:
        if n in existing:
            rel = existing[n].relative_to(repo_dir)
            return f"skill name {n!r} already used by {rel}/ (frontmatter name collision)"

    return None


def _scan_bundled_collision(name: str) -> Optional[str]:
    """Warn if name collides with a bundled skill (will be silently shadowed).

    The existing local-shadow guard in skills_tool.py (line ~541) already
    handles ~/.hermes/skills/ shadowing. This check addresses the
    bundled-vs-portable collision where portable is second in external_dirs
    and would be shadowed by the bundled dir.
    """
    try:
        from hermes_cli.config import cfg_get, load_config
        cfg = load_config()
        external_dirs = cfg_get(cfg, "skills", "external_dirs") or []
        if len(external_dirs) > 1:
            bundled = external_dirs[0]
            bundled_path = Path(str(bundled)) / name
            if bundled_path.is_dir():
                return (
                    f"skill {name!r} exists in bundled skills ({bundled}) "
                    f"and will shadow the portable version"
                )
    except Exception:
        pass
    return None


def _write_skill_md(skill_dir: Path, name: str, content: str) -> None:
    """Write SKILL.md into skill_dir, creating supporting subdirs."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


# --- Action handlers ---

def _handle_status(repo_dir: Path) -> str:
    ok, stdout, stderr = _run_git(["status", "--porcelain"], repo_dir)
    if not ok:
        return json.dumps({"error": stderr or "git status failed"})
    return json.dumps({"status": stdout if stdout else "(clean working tree)"})


def _handle_diff(repo_dir: Path, staged: bool = False) -> str:
    args = ["diff"]
    if staged:
        args.append("--cached")
    ok, stdout, stderr = _run_git(args, repo_dir)
    if not ok:
        return json.dumps({"error": stderr or "git diff failed"})
    return json.dumps({"diff": stdout if stdout else "(no changes)"})


def _handle_stage(repo_dir: Path, files: List[str]) -> str:
    if not files:
        return json.dumps({"error": "no files specified for staging"})
    for f in files:
        err = _validate_path(repo_dir, f)
        if err:
            return json.dumps({"error": err})
    ok, stdout, stderr = _run_git(["add"] + files, repo_dir)
    if not ok:
        return json.dumps({"error": stderr or "git add failed"})
    return json.dumps({"staged": files})


def _handle_commit(repo_dir: Path, message: str,
                   files: Optional[List[str]] = None) -> str:
    if not message or not message.strip():
        return json.dumps({"error": "commit message is required"})
    if files:
        # Commit only the specified files (already path-validated below)
        for f in files:
            err = _validate_path(repo_dir, f)
            if err:
                return json.dumps({"error": err})
        ok, _, stderr = _run_git(["add"] + files, repo_dir)
        if not ok:
            return json.dumps({"error": stderr or "git add failed"})
        ok, stdout, stderr = _run_git(
            ["commit", "-m", message] + files, repo_dir
        )
    else:
        # Commit only staged changes under skills/
        ok_s, staged_out, _ = _run_git(
            ["diff", "--cached", "--name-only"], repo_dir
        )
        if not ok_s:
            return json.dumps(
                {"error": "could not inspect staged changes"}
            )
        staged_files = [
            f for f in staged_out.splitlines() if f.strip()
        ]
        if not staged_files:
            return json.dumps(
                {"error": "nothing staged for commit; use stage first or pass files"}
            )
        for f in staged_files:
            err = _validate_path(repo_dir, f)
            if err:
                return json.dumps(
                    {"error": f"staged file {f!r} is outside skills/; "
                              f"unstage it with 'git reset {f}'"}
                )
        ok, stdout, stderr = _run_git(["commit", "-m", message], repo_dir)
    if not ok:
        return json.dumps({"error": stderr or "git commit failed"})
    return json.dumps({"commit": stdout})


def _current_branch(repo_dir: Path) -> Optional[str]:
    """Return the current branch name for the repo, or None."""
    ok, stdout, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir)
    if ok and stdout and stdout != "HEAD":
        return stdout
    return None


def _handle_push(repo_dir: Path) -> str:
    branch = _current_branch(repo_dir)
    if not branch:
        return json.dumps({"error": "cannot push: not on a branch (detached HEAD)"})
    ok, stdout, stderr = _run_git(
        ["push", "origin", branch], repo_dir, timeout=60
    )
    if not ok:
        if "Permission denied" in stderr or "Could not read from remote repository" in stderr:
            return json.dumps({
                "error": "no push credential configured — push from the workstation instead"
            })
        return json.dumps({"error": stderr or "git push failed"})
    return json.dumps({"push": stdout if stdout else "push succeeded"})


def _handle_pull(repo_dir: Path) -> str:
    branch = _current_branch(repo_dir)
    if not branch:
        return json.dumps({"error": "cannot pull: not on a branch (detached HEAD)"})
    ok, stdout, stderr = _run_git(
        ["pull", "--ff-only", "origin", branch], repo_dir, timeout=60
    )
    if not ok:
        return json.dumps({"error": stderr or "git pull failed"})
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
    except ImportError:
        pass
    return json.dumps({"pull": stdout if stdout else "already up to date"})


def _handle_log(repo_dir: Path, count: int = 10) -> str:
    ok, stdout, stderr = _run_git(
        ["log", "--oneline", "-n", str(max(1, min(count, 100)))], repo_dir
    )
    if not ok:
        return json.dumps({"error": stderr or "git log failed"})
    return json.dumps({"log": stdout if stdout else "(no commits)"})


def _handle_create(repo_dir: Path, name: str, content: str,
                   category: str = "") -> str:
    # Validate name
    name_err = _validate_skill_name(name)
    if name_err:
        return json.dumps({"error": name_err})

    # Validate category (prevent path traversal)
    cat_err = _validate_category(category)
    if cat_err:
        return json.dumps({"error": cat_err})

    # Validate content
    if not content or not content.strip():
        return json.dumps({"error": "content is required for create"})

    fc_err = _validate_frontmatter(content)
    if fc_err:
        return json.dumps({"error": fc_err})

    size_err = _validate_content_size(content)
    if size_err:
        return json.dumps({"error": size_err})

    # Extract frontmatter name for collision checking
    fm_name = _extract_frontmatter_name_from_content(content)

    # Check name uniqueness in repo (with category awareness and frontmatter check)
    dup_err = _check_name_unique_in_repo(repo_dir, name, category, frontmatter_name=fm_name)
    if dup_err:
        return json.dumps({"error": dup_err})

    # Warn if bundled skills will shadow this name
    bundled_warn = _scan_bundled_collision(name)
    if bundled_warn:
        logger.warning(bundled_warn)

    # Build path
    if category:
        skill_dir = repo_dir / "skills" / category / name
        rel_path = f"skills/{category}/{name}"
    else:
        skill_dir = repo_dir / "skills" / name
        rel_path = f"skills/{name}"

    # Write SKILL.md
    _write_skill_md(skill_dir, name, content)

    # Security scan
    scan_err = _security_scan_skill(skill_dir)
    if scan_err:
        # Remove the created skill dir before returning error
        import shutil
        shutil.rmtree(skill_dir, ignore_errors=True)
        return json.dumps({"error": scan_err})

    # Stage
    ok, _, stderr = _run_git(["add", rel_path], repo_dir)
    if not ok:
        return json.dumps({"error": f"skill created but git add failed: {stderr}"})

    # Invalidate skills prompt cache
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
    except ImportError:
        pass

    return json.dumps({
        "created": name,
        "path": rel_path,
        "staged": True,
    })


def skills_repo_handle(
    action: str = "",
    name: Optional[str] = None,
    content: Optional[str] = None,
    category: Optional[str] = None,
    message: Optional[str] = None,
    files: Optional[List[str]] = None,
    count: int = 10,
) -> str:
    """Dispatch actions for the skills_repo tool."""
    try:
        repo_dir = _resolve_repo_dir()
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})

    if action == "status":
        return _handle_status(repo_dir)
    elif action == "diff":
        return _handle_diff(repo_dir, staged=False)
    elif action == "diff_staged":
        return _handle_diff(repo_dir, staged=True)
    elif action == "stage":
        return _handle_stage(repo_dir, files or [])
    elif action == "commit":
        return _handle_commit(repo_dir, message or "", files)
    elif action == "push":
        return _handle_push(repo_dir)
    elif action == "pull":
        return _handle_pull(repo_dir)
    elif action == "log":
        return _handle_log(repo_dir, count)
    elif action == "create":
        if not name:
            return json.dumps({"error": "name is required for create"})
        return _handle_create(repo_dir, name, content or "", category or "")
    else:
        return json.dumps({
            "error": f"unknown action: {action!r}. "
                      f"Valid actions: status, diff, diff_staged, stage, commit, push, pull, log, create"
        })


# --- Schema ---

SKILLS_REPO_SCHEMA = {
    "name": "skills_repo",
    "description": (
        "Manage the portable skills git repository. "
        "Actions: status, diff, diff_staged, stage, commit, push, pull, log, create. "
        "All operations run in the portable-skills repo at skills.repo_dir. "
        "Stage and commit operate only on files under skills/. "
        "Pull fast-forwards remote changes; push publishes local commits. "
        "Create writes a new SKILL.md with validated YAML frontmatter under "
        "skills/<name>/ (or skills/<category>/<name>/ when category is set) "
        "and stages it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "diff", "diff_staged", "stage", "commit", "push", "pull", "log", "create"],
                "description": "The git operation to perform."
            },
            "name": {
                "type": "string",
                "description": "Skill name (required for 'create'). Lowercase letters, digits, and hyphens."
            },
            "content": {
                "type": "string",
                "description": "Full SKILL.md content for 'create' (YAML frontmatter + markdown body)."
            },
            "category": {
                "type": "string",
                "description": "Optional category subdirectory for organizing the skill (e.g. 'devops', 'data-science'). Only used with 'create'."
            },
            "message": {
                "type": "string",
                "description": "Commit message (required for 'commit')."
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File paths relative to repo root (must be under skills/). For 'stage': files to stage. For 'commit': optional files to add before committing."
            },
            "count": {
                "type": "integer",
                "description": "Number of commits for 'log' (default: 10, max: 100)."
            },
        },
        "required": ["action"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="skills_repo",
    toolset="skills",
    schema=SKILLS_REPO_SCHEMA,
    handler=lambda args, **kw: skills_repo_handle(
        action=args.get("action", ""),
        name=args.get("name"),
        content=args.get("content"),
        category=args.get("category"),
        message=args.get("message"),
        files=args.get("files"),
        count=args.get("count", 10),
    ),
    emoji="📦",
)
