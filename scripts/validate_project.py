#!/usr/bin/env python3
"""Validate SKILLNULL repository contracts beyond the Agent Skills schema."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = Path("evals/skillnull/evals.json")
REQUIRED_PATHS = (
    Path("SKILL.md"),
    Path("README.md"),
    Path("LICENSE"),
    Path("scripts/skillnull_gate.py"),
    Path("scripts/check_private_content.py"),
    Path("tests/test_skillnull_gate.py"),
    Path("tests/test_private_content.py"),
    Path("tests/test_project_validation.py"),
    Path(".githooks/pre-commit"),
    Path(".github/workflows/privacy.yml"),
    EVAL_PATH,
)
LOCAL_ONLY_PATHS = ("SKILL.local.md", ".privacy.local.json")
REQUIRED_IGNORE_LINES = {"/SKILL.local.md", "/.privacy.local.json"}
MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
NUMBERED_RULE_PATTERN = re.compile(r"^(\d+)\.\s+\*\*", re.MULTILINE)
ACTION_REFERENCE_PATTERN = re.compile(
    r"^\s*-\s+uses:\s+[^@\s]+@([^\s#]+)", re.MULTILINE
)


def run_git(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def validate_eval_file(target: Path) -> list[str]:
    errors: list[str] = []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [f"{target}: unreadable or invalid JSON"]
    if not isinstance(payload, dict):
        return [f"{target}: root must be an object"]
    if payload.get("schema_version") != "1.0":
        errors.append(f"{target}: schema_version must be 1.0")
    if payload.get("skill_name") != "skillnull":
        errors.append(f"{target}: skill_name must be skillnull")
    eval_cases = payload.get("evals")
    if not isinstance(eval_cases, list) or len(eval_cases) < 10:
        return errors + [f"{target}: at least 10 eval cases are required"]
    seen_ids: set[int] = set()
    seen_prompts: set[str] = set()
    for case_index, eval_case in enumerate(eval_cases, 1):
        location = f"{target}: eval #{case_index}"
        if not isinstance(eval_case, dict):
            errors.append(f"{location} must be an object")
            continue
        case_id = eval_case.get("id")
        if not isinstance(case_id, int) or isinstance(case_id, bool):
            errors.append(f"{location} id must be an integer")
        elif case_id in seen_ids:
            errors.append(f"{location} id is duplicated")
        else:
            seen_ids.add(case_id)
        prompt = eval_case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"{location} prompt must be non-empty")
        elif prompt in seen_prompts:
            errors.append(f"{location} prompt is duplicated")
        else:
            seen_prompts.add(prompt)
        expected_output = eval_case.get("expected_output")
        if not isinstance(expected_output, str) or not expected_output.strip():
            errors.append(f"{location} expected_output must be non-empty")
        assertions = eval_case.get("assertions")
        if not isinstance(assertions, list) or len(assertions) < 2 or any(
            not isinstance(assertion, str) or not assertion.strip()
            for assertion in assertions or []
        ):
            errors.append(f"{location} requires at least two assertions")
        files = eval_case.get("files")
        if not isinstance(files, list) or any(
            not isinstance(file_name, str) for file_name in files or []
        ):
            errors.append(f"{location} files must be a string array")
        tags = eval_case.get("tags")
        if not isinstance(tags, list) or not tags or any(
            not isinstance(tag, str) or not tag.strip() for tag in tags or []
        ):
            errors.append(f"{location} tags must be a non-empty string array")
    return errors


def tracked_markdown_files(root: Path) -> tuple[list[Path], list[str]]:
    result = run_git(root, ["ls-files", "-z", "--", "*.md"])
    if result.returncode != 0:
        return [], ["git ls-files failed while discovering Markdown files"]
    relative_paths = [Path(value) for value in result.stdout.split("\0") if value]
    return relative_paths, []


def validate_markdown_links(root: Path) -> list[str]:
    relative_paths, errors = tracked_markdown_files(root)
    for relative_path in relative_paths:
        source = root / relative_path
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            errors.append(f"{relative_path}: Markdown file is unreadable")
            continue
        for raw_target in MARKDOWN_LINK_PATTERN.findall(content):
            target_value = raw_target.strip().strip("<>")
            if target_value.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_without_anchor = target_value.split("#", 1)[0]
            if not target_without_anchor:
                continue
            if target_without_anchor.startswith("/"):
                errors.append(f"{relative_path}: absolute local link is not portable")
                continue
            resolved = (source.parent / target_without_anchor).resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError:
                errors.append(f"{relative_path}: link escapes the repository")
                continue
            if not resolved.exists():
                errors.append(f"{relative_path}: broken link to {target_without_anchor}")
    return errors


def validate_project(root: Path) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    for required_path in REQUIRED_PATHS:
        target = root / required_path
        if not target.exists():
            errors.append(f"missing required path: {required_path}")
        elif target.is_file() and target.stat().st_size == 0:
            errors.append(f"required file is empty: {required_path}")
    skill_path = root / "SKILL.md"
    if skill_path.exists():
        skill_content = skill_path.read_text(encoding="utf-8")
        if len(skill_content.splitlines()) > 500:
            errors.append("SKILL.md exceeds the 500-line progressive-disclosure limit")
        rule_numbers = [
            int(value) for value in NUMBERED_RULE_PATTERN.findall(skill_content)
        ]
        if rule_numbers != list(range(1, len(rule_numbers) + 1)):
            errors.append("SKILL.md numbered rules must be unique and contiguous")
    ignore_path = root / ".gitignore"
    if not ignore_path.exists():
        errors.append("missing required path: .gitignore")
    else:
        ignore_lines = {
            line.strip()
            for line in ignore_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        for required_line in sorted(REQUIRED_IGNORE_LINES - ignore_lines):
            errors.append(f".gitignore is missing {required_line}")
    tracked_files = run_git(root, ["ls-files", "--", *LOCAL_ONLY_PATHS])
    if tracked_files.returncode != 0:
        errors.append("git ls-files failed while checking local-only files")
    elif tracked_files.stdout.strip():
        errors.append("local-only configuration must not be tracked by Git")
    errors.extend(validate_eval_file(root / EVAL_PATH))
    errors.extend(validate_markdown_links(root))
    workflow_path = root / ".github/workflows/privacy.yml"
    if workflow_path.exists():
        workflow_content = workflow_path.read_text(encoding="utf-8")
        for action_reference in ACTION_REFERENCE_PATTERN.findall(workflow_content):
            if not re.fullmatch(r"[0-9a-fA-F]{40}", action_reference):
                errors.append(
                    ".github/workflows/privacy.yml: actions must use immutable commit SHAs"
                )
                break
    hook_path = root / ".githooks/pre-commit"
    if hook_path.exists():
        hook_content = hook_path.read_text(encoding="utf-8")
        for required_command in ("validate_project.py", "check_private_content.py"):
            if required_command not in hook_content:
                errors.append(f".githooks/pre-commit: missing {required_command}")
    return list(dict.fromkeys(errors))


def emit_result(output_format: str, errors: list[str]) -> None:
    status = "passed" if not errors else "failed"
    if output_format == "json":
        print(
            json.dumps(
                {
                    "status": status,
                    "error_count": len(errors),
                    "errors": errors,
                },
                ensure_ascii=False,
            )
        )
        return
    if errors:
        print("SKILLNULL project validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return
    print("SKILLNULL project validation passed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repository root to validate.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        dest="output_format",
        help="Choose human-readable text or machine-readable JSON output.",
    )
    options = parser.parse_args()
    try:
        errors = validate_project(options.root)
    except (OSError, UnicodeError) as error:
        errors = [f"validation could not read repository data: {error.__class__.__name__}"]
    emit_result(options.output_format, errors)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
