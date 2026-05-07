"""Tests for tools/skills_repo_tool.py."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Force module import to register the tool
import tools.skills_repo_tool  # noqa: F401
from tools.skills_repo_tool import (
    _find_repo_dir,
    _resolve_repo_dir,
    _validate_skill_name,
    _validate_path,
    _validate_frontmatter,
    _validate_content_size,
    _validate_category,
    _run_git,
    _handle_status,
    _handle_create,
    _check_name_unique_in_repo,
    _extract_frontmatter_name,
    _handle_commit,
    skills_repo_handle,
    _MAX_CONTENT_SIZE,
)


@pytest.fixture
def temp_git_repo(tmp_path):
    """Create a temporary git repo with a skills/ directory."""
    repo = tmp_path / "portable-skills"
    repo.mkdir()
    (repo / "skills").mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.test"], cwd=str(repo), capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True
    )
    # Initial commit so repo has HEAD
    (repo / ".gitkeep").write_text("")
    subprocess.run(["git", "add", ".gitkeep"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
    return repo


class TestValidateSkillName:
    def test_valid_name(self):
        assert _validate_skill_name("my-skill") is None
        assert _validate_skill_name("test123") is None
        assert _validate_skill_name("abc") is None

    def test_invalid_name(self):
        assert _validate_skill_name("My-Skill") is not None
        assert _validate_skill_name("a") is not None
        assert _validate_skill_name("a" * 65) is not None
        assert _validate_skill_name("") is not None
        assert _validate_skill_name("  ") is not None


class TestValidatePath:
    def test_path_under_skills(self, temp_git_repo):
        assert _validate_path(temp_git_repo, "skills/test-skill") is None
        assert _validate_path(temp_git_repo, "skills/test-skill/SKILL.md") is None

    def test_path_outside_skills(self, temp_git_repo):
        assert _validate_path(temp_git_repo, "../etc/passwd") is not None
        assert _validate_path(temp_git_repo, "repos/manifest.json") is not None


class TestValidateFrontmatter:
    def test_valid_frontmatter_passes(self):
        content = "---\nname: my-skill\ndescription: A test\n---\n# Content"
        assert _validate_frontmatter(content) is None

    def test_missing_delimiter_fails(self):
        content = "\nname: my-skill\n---\n# Content"
        assert _validate_frontmatter(content) is not None

    def test_missing_name_field_fails(self):
        content = "---\ndescription: no name\n---\n# Content"
        assert _validate_frontmatter(content) is not None

    def test_no_frontmatter_fails(self):
        content = "# Just markdown, no frontmatter"
        assert _validate_frontmatter(content) is not None

    def test_name_not_first_field_passes(self):
        content = "---\ndescription: A test\nname: my-skill\n---\n# Content"
        assert _validate_frontmatter(content) is None

    def test_multiline_frontmatter_with_name_elsewhere_passes(self):
        content = "---\nversion: 1.0\n\ndescription: |\n  A multi-line description.\nname: my-skill\n---\n# Content"
        assert _validate_frontmatter(content) is None


class TestValidateContentSize:
    def test_small_content_passes(self):
        assert _validate_content_size("# small") is None

    def test_oversized_content_fails(self):
        big = "x" * (_MAX_CONTENT_SIZE + 1)
        assert _validate_content_size(big) is not None


class TestValidateCategory:
    def test_valid_category(self):
        assert _validate_category("devops") is None
        assert _validate_category("data-science") is None
        assert _validate_category("") is None
        assert _validate_category("test123") is None

    def test_invalid_category_path_traversal(self):
        assert _validate_category("../outside") is not None
        assert _validate_category("foo/bar") is not None
        assert _validate_category(".") is not None
        assert _validate_category("..") is not None
        assert _validate_category("/etc") is not None

    def test_invalid_category_characters(self):
        assert _validate_category("My Category") is not None
        assert _validate_category("dev ops") is not None
        assert _validate_category("dev_ops") is not None

    def test_category_too_long(self):
        assert _validate_category("a" * 65) is not None


class TestStatus:
    def test_clean_repo(self, temp_git_repo):
        result = _handle_status(temp_git_repo)
        data = json.loads(result)
        assert "(clean working tree)" in data.get("status", "")


class TestCreate:
    def test_creates_skill_and_stages(self, temp_git_repo):
        content = "---\nname: test-skill\n---\n# Test"
        result = _handle_create(temp_git_repo, "test-skill", content)
        data = json.loads(result)
        assert data.get("created") == "test-skill"
        assert data.get("staged") is True
        assert (temp_git_repo / "skills" / "test-skill" / "SKILL.md").exists()

    def test_rejects_duplicate(self, temp_git_repo):
        content = "---\nname: dup-skill\n---\n# Dup"
        _handle_create(temp_git_repo, "dup-skill", content)
        result = _handle_create(temp_git_repo, "dup-skill", content)
        data = json.loads(result)
        assert "error" in data
        assert "already exists" in data["error"]

    def test_rejects_invalid_name(self, temp_git_repo):
        result = _handle_create(temp_git_repo, "X", "---\n---")
        data = json.loads(result)
        assert "error" in data

    def test_requires_content(self, temp_git_repo):
        result = _handle_create(temp_git_repo, "valid-name", "")
        data = json.loads(result)
        assert "error" in data

    def test_rejects_missing_frontmatter(self, temp_git_repo):
        result = _handle_create(temp_git_repo, "valid-name", "# No frontmatter")
        data = json.loads(result)
        assert "error" in data

    def test_rejects_oversized_content(self, temp_git_repo):
        big = "---\nname: x\n---\n" + ("x" * (_MAX_CONTENT_SIZE + 1))
        result = _handle_create(temp_git_repo, "valid-name", big)
        data = json.loads(result)
        assert "error" in data

    def test_create_with_category(self, temp_git_repo):
        content = "---\nname: cat-skill\n---\n# Categorized"
        result = _handle_create(temp_git_repo, "cat-skill", content, category="devops")
        data = json.loads(result)
        assert data.get("created") == "cat-skill"
        assert (temp_git_repo / "skills" / "devops" / "cat-skill" / "SKILL.md").exists()

    def test_category_duplicate_detection(self, temp_git_repo):
        content = "---\nname: cat-dup\n---\n# Cat"
        _handle_create(temp_git_repo, "cat-dup", content, category="devops")
        result = _handle_create(temp_git_repo, "cat-dup", content, category="devops")
        data = json.loads(result)
        assert "error" in data
        assert "already exists" in data["error"]

    def test_rejects_path_traversal_in_category(self, temp_git_repo):
        content = "---\nname: escape\n---\n# Escape"
        result = _handle_create(temp_git_repo, "escape", content, category="../outside")
        data = json.loads(result)
        assert "error" in data
        assert "category" in data["error"].lower()
        # Verify no directory was created outside skills/
        assert not (temp_git_repo / "outside").exists()

    def test_rejects_frontmatter_name_collision(self, temp_git_repo):
        # Create a skill in category "devops" with frontmatter name "shared-name"
        content1 = "---\nname: shared-name\n---\n# DevOps version"
        _handle_create(temp_git_repo, "dev-skill", content1, category="devops")
        # Try to create another skill in different directory with same frontmatter name
        content2 = "---\nname: shared-name\n---\n# Flat version"
        result = _handle_create(temp_git_repo, "other-skill", content2)
        data = json.loads(result)
        assert "error" in data

    def test_rejects_directory_basename_collision_across_categories(self, temp_git_repo):
        # Create skills/devops/tools/
        content1 = "---\nname: devops-tools\n---\n# DevOps tools"
        _handle_create(temp_git_repo, "tools", content1, category="devops")
        # Try to create skills/mlops/tools/ — same basename, different category
        content2 = "---\nname: mlops-tools\n---\n# MLOps tools"
        result = _handle_create(temp_git_repo, "tools", content2, category="mlops")
        data = json.loads(result)
        assert "error" in data
        assert "collision" in data["error"].lower() or "already exists" in data["error"].lower()
        assert "already used" in data["error"].lower() or "collision" in data["error"].lower()


class TestStageAndCommit:
    def test_stage_and_commit(self, temp_git_repo):
        # Create a file to stage
        (temp_git_repo / "skills" / "test-skill").mkdir()
        (temp_git_repo / "skills" / "test-skill" / "SKILL.md").write_text("# Test")
        with patch("tools.skills_repo_tool._resolve_repo_dir", return_value=temp_git_repo):
            result = skills_repo_handle(
                action="stage", files=["skills/test-skill/SKILL.md"]
            )
        data = json.loads(result)
        assert "error" not in data

        with patch("tools.skills_repo_tool._resolve_repo_dir", return_value=temp_git_repo):
            result2 = skills_repo_handle(action="commit", message="add test skill")
        data2 = json.loads(result2)
        assert "error" not in data2

    def test_commit_requires_message(self, temp_git_repo):
        with patch("tools.skills_repo_tool._resolve_repo_dir", return_value=temp_git_repo):
            result = skills_repo_handle(action="commit", message="")
        data = json.loads(result)
        assert "error" in data
        assert "message" in data["error"].lower()

    def test_stage_rejects_outside_skills(self, temp_git_repo):
        with patch("tools.skills_repo_tool._resolve_repo_dir", return_value=temp_git_repo):
            result = skills_repo_handle(
                action="stage", files=["../etc/passwd"]
            )
        data = json.loads(result)
        assert "error" in data

    def test_commit_rejects_staged_outside_skills(self, temp_git_repo):
        # Stage a file outside skills/
        subprocess.run(
            ["git", "add", "."],
            cwd=str(temp_git_repo), capture_output=True
        )
        with patch("tools.skills_repo_tool._resolve_repo_dir", return_value=temp_git_repo):
            result = skills_repo_handle(action="commit", message="bad commit")
        data = json.loads(result)
        # Should detect the staged .gitkeep is outside skills/
        assert "error" in data

    def test_commit_with_files_param(self, temp_git_repo):
        (temp_git_repo / "skills" / "target-skill").mkdir()
        (temp_git_repo / "skills" / "target-skill" / "SKILL.md").write_text("# Target")
        with patch("tools.skills_repo_tool._resolve_repo_dir", return_value=temp_git_repo):
            result = skills_repo_handle(
                action="commit",
                message="add target",
                files=["skills/target-skill/SKILL.md"],
            )
        data = json.loads(result)
        assert "error" not in data


class TestLog:
    def test_returns_commits(self, temp_git_repo):
        _handle_create(temp_git_repo, "log-test",
                       "---\nname: log-test\n---\n# Log")
        with patch("tools.skills_repo_tool._resolve_repo_dir", return_value=temp_git_repo):
            skills_repo_handle(action="commit", message="log test commit")
            log_result = skills_repo_handle(action="log", count=5)
        data = json.loads(log_result)
        assert "log" in data
        assert "log test commit" in data["log"]


class TestPushNoAuth:
    def test_push_returns_clear_error_without_auth(self, temp_git_repo):
        # Set origin to an invalid URL instead of removing it.
        # This exercises the real "Could not read from remote repository" path.
        subprocess.run(
            ["git", "remote", "set-url", "origin", "https://invalid.example/repo"],
            cwd=str(temp_git_repo), capture_output=True
        )
        with patch("tools.skills_repo_tool._resolve_repo_dir", return_value=temp_git_repo):
            result = skills_repo_handle(action="push")
        data = json.loads(result)
        assert "error" in data


class TestFindRepoDir:
    def test_returns_none_when_not_configured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # cfg_get is imported inside _find_repo_dir and also used at module
        # level in skills_repo_tool. Patch hermes_cli.config.return_value
        # where cfg_get is defined, with cfg=None and no matching keys.
        with patch("hermes_cli.config.cfg_get", return_value=None):
            assert _find_repo_dir() is None

    def test_finds_repo_via_external_dirs(self, temp_git_repo):
        repo_path = str(temp_git_repo)
        def mock_cfg_get(cfg, *keys, default=None):
            key_path = ".".join(keys)
            if key_path == "skills.repo_dir":
                return None
            if key_path == "skills.external_dirs":
                return [repo_path + "/skills"]
            return default
        with patch("hermes_cli.config.cfg_get", side_effect=mock_cfg_get):
            result = _find_repo_dir()
            assert result is not None

    def test_finds_repo_via_git_file_worktree(self, tmp_path):
        # Create a valid git repo with .git as a file (git worktree)
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        subprocess.run(["git", "init", "-b", "trunk"], cwd=str(main_repo), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=str(main_repo), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(main_repo), capture_output=True)
        (main_repo / "initial.txt").write_text("init")
        subprocess.run(["git", "add", "initial.txt"], cwd=str(main_repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(main_repo), capture_output=True)
        subprocess.run(["git", "branch", "devel"], cwd=str(main_repo), capture_output=True)

        # Create a linked worktree (produces .git as a file)
        worktree_path = tmp_path / "worktree-checkout"
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "devel"],
            cwd=str(main_repo), capture_output=True
        )
        assert (worktree_path / ".git").is_file()  # worktree has .git as file

        # mock config to return the worktree skills/ as external_dir
        wt_skills = str(worktree_path / "skills")
        def mock_cfg_get(cfg, *keys, default=None):
            key_path = ".".join(keys)
            if key_path == "skills.repo_dir":
                return None
            if key_path == "skills.external_dirs":
                return [wt_skills]
            return default
        with patch("hermes_cli.config.cfg_get", side_effect=mock_cfg_get):
            result = _find_repo_dir()
            assert result is not None
            assert result == worktree_path
