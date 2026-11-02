#!/usr/bin/env python3
"""Global Codex hook gate for the non-negotiable SKILLNULL rules."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"
LOCAL_SKILL_PATH = SKILL_PATH.with_name("SKILL.local.md")
STATE_ROOT = Path.home() / ".codex" / "state" / "skillnull-gate"
SOURCE_SUFFIXES = {
	".c",
	".cc",
	".cpp",
	".css",
	".go",
	".h",
	".hpp",
	".html",
	".java",
	".js",
	".jsx",
	".kt",
	".less",
	".mjs",
	".py",
	".rb",
	".rs",
	".sass",
	".scss",
	".swift",
	".ts",
	".tsx",
	".vue",
}
REMOVAL_WORDS = re.compile(
	r"删除|移除|废弃|下线|清空|重写|替换|remove|delete|deprecate|rewrite|replace",
	re.IGNORECASE,
)
REMOVAL_DENIAL_WORDS = re.compile(
	r"(?:不要|不得|不能|不可|不需要|无需|禁止|避免)\s*"
	r"(?:删除|移除|废弃|下线|清空|重写|替换)"
	r"|(?:do\s+not|don't|must\s+not|without)\s+"
	r"(?:remove|delete|deprecate|rewrite|replace)",
	re.IGNORECASE,
)
IMPORT_PATTERN = re.compile(
	r"\bimport\s+(?:type\s+)?(.+?)\s+from\s+[\"'][^\"']+[\"']\s*;?",
	re.DOTALL,
)
DESTRUCTIVE_COMMANDS = (
	re.compile(r"(?:^|[;&|]\s*)git\s+(?:reset|clean|checkout|restore)\b", re.IGNORECASE),
	re.compile(r"(?:^|[;&|]\s*)(?:rm|truncate)\b", re.IGNORECASE),
	re.compile(r"(?:^|[;&|]\s*)(?:sed\s+-i|perl\s+-pi)\b", re.IGNORECASE),
	re.compile(
		r">>?\s*[^\s]+\.(?:c|cc|cpp|css|go|h|hpp|html|java|js|jsx|kt|less|mjs|py|rb|rs|sass|scss|swift|ts|tsx|vue)\b",
		re.IGNORECASE,
	),
)


def emit(payload: dict[str, Any]) -> None:
	json.dump(payload, sys.stdout, ensure_ascii=False)
	sys.stdout.write("\n")


def run_git(cwd: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["git", *arguments],
		cwd=cwd,
		capture_output=True,
		text=True,
		check=False,
	)


def find_repo_root(cwd_value: str | None) -> Path | None:
	if not cwd_value:
		return None
	cwd = Path(cwd_value).expanduser()
	if not cwd.exists():
		return None
	result = run_git(cwd, ["rev-parse", "--show-toplevel"])
	if result.returncode != 0:
		return None
	return Path(result.stdout.strip()).resolve()


def state_path(session_id: str) -> Path:
	digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
	return STATE_ROOT / f"{digest}.json"


def load_state(session_id: str) -> dict[str, Any]:
	path = state_path(session_id)
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (FileNotFoundError, json.JSONDecodeError, OSError):
		return {}


def save_state(session_id: str, state: dict[str, Any]) -> None:
	STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
	if os.name != "nt":
		STATE_ROOT.chmod(0o700)
	path = state_path(session_id)
	temporary_path = path.with_suffix(".tmp")
	temporary_path.write_text(
		json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
	)
	if os.name != "nt":
		temporary_path.chmod(0o600)
	temporary_path.replace(path)


def clear_state(session_id: str) -> None:
	try:
		state_path(session_id).unlink()
	except FileNotFoundError:
		pass


def diff_check_lines(repo_root: Path) -> list[str]:
	result = run_git(repo_root, ["diff", "--check"])
	return sorted(line for line in result.stdout.splitlines() if line.strip())


def working_tree_zero_sources(repo_root: Path) -> list[str]:
	result = run_git(
		repo_root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
	)
	if result.returncode != 0:
		return []
	zero_sources: list[str] = []
	for status_entry in result.stdout.split("\0"):
		if len(status_entry) < 4:
			continue
		relative_target = status_entry[3:]
		target = normalize_target(repo_root, relative_target)
		if (
			target
			and target.suffix.lower() in SOURCE_SUFFIXES
			and target.exists()
			and target.stat().st_size == 0
		):
			zero_sources.append(str(target.relative_to(repo_root.resolve())))
	return sorted(set(zero_sources))


def load_skill_context() -> str:
	sections = [SKILL_PATH.read_text(encoding="utf-8")]
	if LOCAL_SKILL_PATH.exists():
		sections.append(LOCAL_SKILL_PATH.read_text(encoding="utf-8"))
	return "\n\n--- SKILLNULL 本机扩展 ---\n\n".join(sections)


def extract_prompt(payload: dict[str, Any]) -> str:
	for key in ("prompt", "user_prompt", "message"):
		value = payload.get(key)
		if isinstance(value, str):
			return value
	return ""


def has_removal_authorization(prompt: str) -> bool:
	return bool(REMOVAL_WORDS.search(prompt)) and not REMOVAL_DENIAL_WORDS.search(
		prompt
	)


def extract_tool_text(payload: dict[str, Any]) -> str:
	tool_input = payload.get("tool_input")
	if isinstance(tool_input, str):
		return tool_input
	if not isinstance(tool_input, dict):
		return ""
	for key in ("patch", "input", "command", "cmd"):
		value = tool_input.get(key)
		if isinstance(value, str):
			return value
	return json.dumps(tool_input, ensure_ascii=False)


def normalize_target(repo_root: Path, raw_target: str) -> Path | None:
	repo_root = repo_root.resolve()
	target = Path(raw_target.strip())
	resolved = target.resolve() if target.is_absolute() else (repo_root / target).resolve()
	try:
		resolved.relative_to(repo_root)
	except ValueError:
		return None
	return resolved


def parse_patch_sections(patch_text: str) -> list[dict[str, Any]]:
	sections: list[dict[str, Any]] = []
	current_section: dict[str, Any] | None = None
	for line in patch_text.splitlines():
		header_match = re.match(r"\*\*\* (Update|Delete|Add) File: (.+)$", line)
		if header_match:
			if current_section:
				sections.append(current_section)
			current_section = {
				"operation": header_match.group(1).lower(),
				"target": header_match.group(2).strip(),
				"removed": [],
				"added": [],
			}
			continue
		if not current_section or line.startswith("@@"):
			continue
		if line.startswith("-") and not line.startswith("---"):
			current_section["removed"].append(line[1:])
		elif line.startswith("+") and not line.startswith("+++"):
			current_section["added"].append(line[1:])
	if current_section:
		sections.append(current_section)
	return sections


def imported_names(content: str) -> set[str]:
	names: set[str] = set()
	for import_clause in IMPORT_PATTERN.findall(content):
		clause = import_clause.strip()
		default_match = re.match(r"([A-Za-z_$][\w$]*)", clause)
		if default_match:
			names.add(default_match.group(1))
		namespace_match = re.search(r"\*\s+as\s+([A-Za-z_$][\w$]*)", clause)
		if namespace_match:
			names.add(namespace_match.group(1))
		named_match = re.search(r"\{(.*?)\}", clause, flags=re.DOTALL)
		if not named_match:
			continue
		for imported_item in named_match.group(1).split(","):
			item = re.sub(r"^\s*type\s+", "", imported_item.strip())
			if not item:
				continue
			alias_parts = re.split(r"\s+as\s+", item)
			local_name = alias_parts[-1].strip()
			if re.fullmatch(r"[A-Za-z_$][\w$]*", local_name):
				names.add(local_name)
	return names


def projected_content(
	content: str, removed_lines: list[str], added_lines: list[str]
) -> str:
	projected_lines = content.splitlines()
	for removed_line in removed_lines:
		try:
			projected_lines.remove(removed_line)
		except ValueError:
			continue
	projected_lines.extend(added_lines)
	return "\n".join(projected_lines)


def removed_import_still_used(
	content: str, removed_lines: list[str], added_lines: list[str]
) -> str | None:
	removed_names = imported_names("\n".join(removed_lines))
	if not removed_names:
		return None
	projected = projected_content(content, removed_lines, added_lines)
	remaining_imports = imported_names(projected)
	usage_content = IMPORT_PATTERN.sub("", projected)
	for removed_name in sorted(removed_names - remaining_imports):
		if re.search(rf"(?<![\w$]){re.escape(removed_name)}(?![\w$])", usage_content):
			return removed_name
	return None


def exported_names(content: str) -> set[str]:
	patterns = (
		r"\bexport\s+(?:declare\s+)?(?:const|let|var|function|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
		r"\bexport\s*\{([^}]+)\}",
	)
	names = set(re.findall(patterns[0], content))
	for export_group in re.findall(patterns[1], content, flags=re.DOTALL):
		for item in export_group.split(","):
			name = re.split(r"\s+as\s+", item.strip())[0].strip()
			if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
				names.add(name)
	return names


def reference_tokens(target: Path, content: str) -> set[str]:
	tokens = exported_names(content)
	component_name = target.parent.name if target.stem == "index" else target.stem
	if len(component_name) >= 4:
		tokens.add(component_name)
	return {token for token in tokens if len(token) >= 4}


def find_references(repo_root: Path, target: Path, content: str) -> list[str]:
	repo_root = repo_root.resolve()
	tokens = reference_tokens(target, content)
	if not tokens:
		return []
	references: list[str] = []
	for token in sorted(tokens):
		result = subprocess.run(
			[
				"rg",
				"-n",
				"--fixed-strings",
				"--glob",
				"!.git/**",
				"--glob",
				f"!{target.relative_to(repo_root)}",
				token,
				str(repo_root),
			],
			capture_output=True,
			text=True,
			check=False,
		)
		for line in result.stdout.splitlines()[:5]:
			references.append(line.replace(str(repo_root) + os.sep, ""))
		if references:
			break
	return references


def deny_pretool(reason: str) -> dict[str, Any]:
	return {
		"hookSpecificOutput": {
			"hookEventName": "PreToolUse",
			"permissionDecision": "deny",
			"permissionDecisionReason": reason,
		}
	}


def check_patch(
	repo_root: Path, patch_text: str, removal_authorized: bool
) -> tuple[str | None, list[str]]:
	repo_root = repo_root.resolve()
	touched: list[str] = []
	for section in parse_patch_sections(patch_text):
		target = normalize_target(repo_root, section["target"])
		if target is None:
			continue
		relative_target = str(target.relative_to(repo_root))
		touched.append(relative_target)
		if target.suffix.lower() not in SOURCE_SUFFIXES:
			continue
		operation = section["operation"]
		content = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
		removed_count = len(section["removed"])
		added_count = len(section["added"])
		current_line_count = len(content.splitlines())
		missing_import = removed_import_still_used(
			content, section["removed"], section["added"]
		)
		if missing_import:
			return (
				f"SKILLNULL 门禁：{relative_target} 删除 import 后仍使用 "
				f"{missing_import}，已阻止。",
				touched,
			)
		if operation == "delete":
			references = find_references(repo_root, target, content)
			if references:
				return (
					f"SKILLNULL 门禁：禁止删除仍被引用的源码 {relative_target}。"
					f"引用示例：{references[0]}",
					touched,
				)
			if not removal_authorized:
				return (
					f"SKILLNULL 门禁：删除源码 {relative_target} 未得到当前用户请求的明确授权。",
					touched,
				)
		if (
			operation == "update"
			and current_line_count > 0
			and removed_count >= current_line_count
			and added_count == 0
		):
			return (
				f"SKILLNULL 门禁：补丁会清空源码 {relative_target}，已阻止。"
				"如确需删除，请使用明确的整文件删除并先消除全部引用。",
				touched,
			)
		large_shrink = (
			current_line_count >= 20
			and removed_count >= 20
			and removed_count >= current_line_count / 2
			and added_count * 3 < removed_count
		)
		if large_shrink and not removal_authorized:
			return (
				f"SKILLNULL 门禁：{relative_target} 将大幅缩减"
				f"（删 {removed_count} 行、增 {added_count} 行），但当前请求未明确要求删除/重写。",
				touched,
			)
	return None, touched


def current_gate_failures(repo_root: Path, state: dict[str, Any]) -> list[str]:
	repo_root = repo_root.resolve()
	failures: list[str] = []
	baseline_diff_errors = set(state.get("baseline_diff_check", []))
	new_diff_errors = [
		line for line in diff_check_lines(repo_root) if line not in baseline_diff_errors
	]
	if new_diff_errors:
		failures.append(f"新增 diff 格式错误：{new_diff_errors[0]}")
	baseline_zero_sources = set(state.get("baseline_zero_sources", []))
	for relative_target in working_tree_zero_sources(repo_root):
		if relative_target not in baseline_zero_sources:
			failures.append(f"源码被清空：{relative_target}")
	for relative_target in state.get("touched_files", []):
		target = normalize_target(repo_root, relative_target)
		if not target or target.suffix.lower() not in SOURCE_SUFFIXES:
			continue
		if target.exists() and target.stat().st_size == 0:
			failures.append(f"源码被清空：{relative_target}")
	return list(dict.fromkeys(failures))


def handle_user_prompt(payload: dict[str, Any]) -> dict[str, Any]:
	repo_root = find_repo_root(payload.get("cwd"))
	if repo_root is None:
		return {}
	session_id = str(payload.get("session_id") or "unknown-session")
	prompt = extract_prompt(payload)
	state = {
		"removal_authorized": has_removal_authorization(prompt),
		"baseline_diff_check": diff_check_lines(repo_root),
		"baseline_zero_sources": working_tree_zero_sources(repo_root),
		"touched_files": [],
	}
	try:
		skill_context = load_skill_context()
		state["skill_loaded"] = True
		state["skill_hash"] = hashlib.sha256(
			skill_context.encode("utf-8")
		).hexdigest()
	except OSError as error:
		state["skill_loaded"] = False
		skill_context = f"SKILLNULL 读取失败，禁止修改代码：{error.__class__.__name__}"
	save_state(session_id, state)
	return {
		"hookSpecificOutput": {
			"hookEventName": "UserPromptSubmit",
			"additionalContext": (
				"全局 SKILLNULL 门禁已启用。以下规则由全局本地 Hook 直接加载，"
				"必须逐条执行。关键删除另由工具门禁强制校验。\n\n"
				+ skill_context
			),
		}
	}


def handle_pre_tool(payload: dict[str, Any]) -> dict[str, Any]:
	repo_root = find_repo_root(payload.get("cwd"))
	if repo_root is None:
		return {}
	session_id = str(payload.get("session_id") or "unknown-session")
	state = load_state(session_id)
	tool_name = str(payload.get("tool_name") or "")
	tool_text = extract_tool_text(payload)
	if "apply_patch" in tool_name.lower() and not state.get("skill_loaded"):
		return deny_pretool(
			"SKILLNULL 门禁：本轮尚未由全局 Hook 成功加载完整规则，禁止修改代码。"
		)
	if tool_name.lower() in {"bash", "exec_command"}:
		for destructive_pattern in DESTRUCTIVE_COMMANDS:
			if destructive_pattern.search(tool_text):
				return deny_pretool(
					"SKILLNULL 门禁：已阻止直接破坏或原地重写命令。"
					"请先核对影响与引用，再用可审查的 apply_patch 执行。"
				)
	if "apply_patch" in tool_name.lower():
		reason, touched = check_patch(
			repo_root, tool_text, bool(state.get("removal_authorized"))
		)
		state["touched_files"] = sorted(
			set(state.get("touched_files", [])) | set(touched)
		)
		save_state(session_id, state)
		if reason:
			return deny_pretool(reason)
	return {}


def handle_post_tool(payload: dict[str, Any]) -> dict[str, Any]:
	repo_root = find_repo_root(payload.get("cwd"))
	if repo_root is None:
		return {}
	session_id = str(payload.get("session_id") or "unknown-session")
	failures = current_gate_failures(repo_root, load_state(session_id))
	if not failures:
		return {}
	return {
		"hookSpecificOutput": {
			"hookEventName": "PostToolUse",
			"additionalContext": "SKILLNULL 门禁未通过：" + "；".join(failures),
		}
	}


def handle_stop(payload: dict[str, Any]) -> dict[str, Any]:
	if payload.get("stop_hook_active"):
		return {}
	repo_root = find_repo_root(payload.get("cwd"))
	if repo_root is None:
		return {}
	session_id = str(payload.get("session_id") or "unknown-session")
	failures = current_gate_failures(repo_root, load_state(session_id))
	if not failures:
		clear_state(session_id)
		return {}
	return {
		"decision": "block",
		"reason": "SKILLNULL 全局门禁阻止结束：" + "；".join(failures),
	}


def main() -> int:
	try:
		payload = json.load(sys.stdin)
	except (json.JSONDecodeError, OSError):
		emit({})
		return 0
	event_name = str(payload.get("hook_event_name") or "")
	handlers = {
		"UserPromptSubmit": handle_user_prompt,
		"PreToolUse": handle_pre_tool,
		"PostToolUse": handle_post_tool,
		"Stop": handle_stop,
	}
	handler = handlers.get(event_name)
	emit(handler(payload) if handler else {})
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
