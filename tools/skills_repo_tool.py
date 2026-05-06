"""Skills Repo Tool -- Agent-Managed Git Operations on the Portable Skills Repo.

Allows the agent to view, stage, commit, push, pull, and create skills in the
shared portable-skills git repository at ``skills.repo_dir`` (config.yaml) or
found by scanning ``skills.external_dirs`` for a ``.git/`` parent.

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
_MAX_CONTENT_SIZE = 256 * 1024  # 256 KiB

# Minimal YAML frontmatter check: must start with '---' and have at least
# a 'name:' field. Full validation is deferred to the skill loader, but
# this catches obvious malformed content early.
_FRONTMATTER_MIN_RE = re.compile(
    r"^---\s*\n.*\bname\s*:.*\n(?:.|\n)*?---\s*\n",
    re.MULTILINE,
)


def _find_repo_dir() -> Optional[Path]:
    """Resolve the skills repo directory from config or external_dirs scan."""
    try:
        from hermes_cli.config import cfg_get, load_config
        cfg = load_config()
        repo_dir = cfg_get(cfg, "skills", "repo_dir")
        if repo_dir:
            path = Path(str(repo_dir))
            if (path / ".git").is_dir():
                return path.resolve()
    except Exception:
        pass

    try:
        from hermes_cli.config import cfg_get, load_config
        cfg = load_config()
        external_dirs = cfg_get(cfg, "skills", "external_dirs") or []
        for d in external_dirs:
            p = Path(str(d))
            git_dir = p
            while git_dir != git_dir.parent:
                if (git_dir / ".git").is_dir():
                    return git_dir.resolve()
                git_dir = git_dir.parent
    except Exception:
        pass

    return None


def _run_git(args: List[str], repo_dir: Path,
             timeout: int = _GIT_TIMEOUT) -> tuple:
    """Run a git command. Returns (ok, stdout, stderr)."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Clear env vars that could override per-repo git config
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_SSH_COMMAND", None)
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
            "skills_repo: no git repo found. Set skills.repo_dir in config.yaml "
            "or ensure a skills.external_dirs entry has a .git/ parent."
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
    """Check that content has basic YAML frontmatter with a 'name' field."""
    if not _FRONTMATTER_MIN_RE.match(content.strip()):
        return (
            "content must start with YAML frontmatter (---\\n...\\n---) "
            "containing at least a 'name:' field"
        )
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
    """Run security scan on a new skill. Returns error string or None."""
    try:
        from tools.skill_manager_tool import _guard_agent_created_enabled
        from tools.skills_guard import scan_skill, should_allow_install, format_scan_report
        if not _guard_agent_created_enabled():
            return None
        report = scan_skill(skill_dir, is_external=False)
        allowed, _ = should_allow_install(report)
        if not allowed:
            return format_scan_report(report)
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("skipping security scan: %s", exc)
    return None


def _check_name_unique_in_repo(repo_dir: Path, name: str,
                                category: str = "") -> Optional[str]:
    """Check that name does not collide with an existing skill in the repo.

    Checks under skills/<category>/<name> when category is set,
    and also under skills/<name> for flat directory collisions.
    """
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


def _handle_push(repo_dir: Path) -> str:
    ok, stdout, stderr = _run_git(
        ["push", "origin", "main"], repo_dir, timeout=60
    )
    if not ok:
        if "Permission denied" in stderr or "Could not read from remote repository" in stderr:
            return json.dumps({
                "error": "no push credential configured — push from the workstation instead"
            })
        return json.dumps({"error": stderr or "git push failed"})
    return json.dumps({"push": stdout if stdout else "push succeeded"})


def _handle_pull(repo_dir: Path) -> str:
    ok, stdout, stderr = _run_git(
        ["pull", "--ff-only", "origin", "main"], repo_dir, timeout=60
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

    # Validate content
    if not content or not content.strip():
        return json.dumps({"error": "content is required for create"})

    fc_err = _validate_frontmatter(content)
    if fc_err:
        return json.dumps({"error": fc_err})

    size_err = _validate_content_size(content)
    if size_err:
        return json.dumps({"error": size_err})

    # Check name uniqueness in repo (with category awareness)
    dup_err = _check_name_unique_in_repo(repo_dir, name, category)
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
    "type": "function",
    "function": {
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
