from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = (
	Path(__file__).resolve().parents[1] / "scripts" / "skillnull_gate.py"
)
MODULE_SPEC = importlib.util.spec_from_file_location("skillnull_gate", SCRIPT_PATH)
assert MODULE_SPEC and MODULE_SPEC.loader
skillnull_gate = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(skillnull_gate)


class SkillnullGateTest(unittest.TestCase):
	def setUp(self) -> None:
		self.temporary_directory = tempfile.TemporaryDirectory()
		self.repo_root = Path(self.temporary_directory.name)
		subprocess.run(["git", "init", "-q", str(self.repo_root)], check=True)
		(self.repo_root / "src").mkdir()
		self.original_state_root = skillnull_gate.STATE_ROOT
		skillnull_gate.STATE_ROOT = self.repo_root / ".state"

	def tearDown(self) -> None:
		skillnull_gate.STATE_ROOT = self.original_state_root
		self.temporary_directory.cleanup()

	def write_source(self, relative_path: str, content: str) -> Path:
		target = self.repo_root / relative_path
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text(content, encoding="utf-8")
		return target

	def test_blocks_patch_that_empties_source_file(self) -> None:
		self.write_source("src/hooks.ts", "export const useQuery = () => 1;\n")
		patch_text = """*** Begin Patch
*** Update File: src/hooks.ts
@@
-export const useQuery = () => 1;
*** End Patch
"""
		reason, touched = skillnull_gate.check_patch(
			self.repo_root, patch_text, False
		)
		self.assertIn("清空源码", reason)
		self.assertEqual(touched, ["src/hooks.ts"])

	def test_allows_small_scoped_edit(self) -> None:
		self.write_source("src/value.ts", "export const value = 1;\n")
		patch_text = """*** Begin Patch
*** Update File: src/value.ts
@@
-export const value = 1;
+export const value = 2;
*** End Patch
"""
		reason, touched = skillnull_gate.check_patch(
			self.repo_root, patch_text, False
		)
		self.assertIsNone(reason)
		self.assertEqual(touched, ["src/value.ts"])

	def test_blocks_deleting_referenced_source(self) -> None:
		self.write_source("src/hooks.ts", "export const useQuery = () => 1;\n")
		self.write_source(
			"src/page.ts", 'import { useQuery } from "./hooks";\nuseQuery();\n'
		)
		patch_text = """*** Begin Patch
*** Delete File: src/hooks.ts
*** End Patch
"""
		reason, _touched = skillnull_gate.check_patch(
			self.repo_root, patch_text, True
		)
		self.assertIn("仍被引用", reason)

	def test_blocks_removing_import_that_is_still_used(self) -> None:
		self.write_source(
			"src/page.ts",
			'import { useQuery } from "./hooks";\nexport const value = useQuery();\n',
		)
		patch_text = """*** Begin Patch
*** Update File: src/page.ts
@@
-import { useQuery } from "./hooks";
*** End Patch
"""
		reason, _touched = skillnull_gate.check_patch(
			self.repo_root, patch_text, False
		)
		self.assertIn("删除 import 后仍使用 useQuery", reason)

	def test_allows_removing_import_and_its_only_usage(self) -> None:
		self.write_source(
			"src/page.ts",
			'import { useQuery } from "./hooks";\nexport const value = useQuery();\n',
		)
		patch_text = """*** Begin Patch
*** Update File: src/page.ts
@@
-import { useQuery } from "./hooks";
-export const value = useQuery();
+export const value = 1;
*** End Patch
"""
		reason, _touched = skillnull_gate.check_patch(
			self.repo_root, patch_text, True
		)
		self.assertIsNone(reason)

	def test_stop_blocks_new_empty_touched_source(self) -> None:
		target = self.write_source("src/empty.ts", "")
		state = {
			"baseline_diff_check": [],
			"baseline_zero_sources": [],
			"touched_files": [str(target.relative_to(self.repo_root))],
		}
		failures = skillnull_gate.current_gate_failures(self.repo_root, state)
		self.assertIn("源码被清空：src/empty.ts", failures)

	def test_detects_empty_source_created_outside_apply_patch(self) -> None:
		self.write_source("src/empty.ts", "")
		failures = skillnull_gate.current_gate_failures(
			self.repo_root,
			{
				"baseline_diff_check": [],
				"baseline_zero_sources": [],
				"touched_files": [],
			},
		)
		self.assertEqual(failures, ["源码被清空：src/empty.ts"])

	def test_hook_output_is_valid_json(self) -> None:
		payload = json.dumps(
			{
				"hook_event_name": "UserPromptSubmit",
				"session_id": "test-session",
				"cwd": str(self.repo_root),
				"prompt": "修改代码",
			}
		)
		result = subprocess.run(
			["python3", str(SCRIPT_PATH)],
			input=payload,
			capture_output=True,
			text=True,
			check=True,
			env={**os.environ, "HOME": str(self.repo_root)},
		)
		output = json.loads(result.stdout)
		self.assertEqual(
			output["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit"
		)
		self.assertIn(
			"SKILLNULL — 编写与迭代守则",
			output["hookSpecificOutput"]["additionalContext"],
		)

	def test_state_keeps_only_the_removal_decision(self) -> None:
		session_id = "state-privacy-test-session"
		skillnull_gate.handle_user_prompt(
			{
				"session_id": session_id,
				"cwd": str(self.repo_root),
				"prompt": "删除不再需要的旧文件",
			}
		)
		state = skillnull_gate.load_state(session_id)
		self.assertNotIn("prompt", state)
		self.assertTrue(state["removal_authorized"])
		if os.name != "nt":
			state_mode = stat.S_IMODE(
				skillnull_gate.state_path(session_id).stat().st_mode
			)
			self.assertEqual(state_mode, 0o600)

	def test_negated_removal_is_not_authorized(self) -> None:
		self.assertFalse(skillnull_gate.has_removal_authorization("不要删除任何文件"))
		self.assertFalse(skillnull_gate.has_removal_authorization("do not delete files"))
		self.assertTrue(skillnull_gate.has_removal_authorization("删除旧文件"))

	def test_successful_stop_removes_session_state(self) -> None:
		session_id = "completed-session"
		skillnull_gate.handle_user_prompt(
			{
				"session_id": session_id,
				"cwd": str(self.repo_root),
				"prompt": "修改代码",
			}
		)
		self.assertTrue(skillnull_gate.state_path(session_id).exists())
		self.assertEqual(
			skillnull_gate.handle_stop(
				{"session_id": session_id, "cwd": str(self.repo_root)}
			),
			{},
		)
		self.assertFalse(skillnull_gate.state_path(session_id).exists())

	def test_prompt_wording_does_not_disable_normal_tool_use(self) -> None:
		session_id = "normal-tool-use-test-session"
		self.addCleanup(
			skillnull_gate.state_path(session_id).unlink, missing_ok=True
		)
		prompt_output = skillnull_gate.handle_user_prompt(
			{
				"hook_event_name": "UserPromptSubmit",
				"session_id": session_id,
				"cwd": str(self.repo_root),
				"prompt": "修改 Skill 与系统提示词相关的输出格式",
			}
		)
		context = prompt_output["hookSpecificOutput"]["additionalContext"]
		self.assertIn("SKILLNULL — 编写与迭代守则", context)

		tool_output = skillnull_gate.handle_pre_tool(
			{
				"hook_event_name": "PreToolUse",
				"session_id": session_id,
				"cwd": str(self.repo_root),
				"tool_name": "exec_command",
				"tool_input": {"cmd": "pwd"},
			}
		)
		self.assertEqual(tool_output, {})

	def test_relocated_hook_loads_adjacent_rules_through_symlink(self) -> None:
		relocated_script = self.write_source(
			"relocated/scripts/skillnull_gate.py",
			SCRIPT_PATH.read_text(encoding="utf-8"),
		)
		self.write_source("relocated/SKILL.md", "portable main rules\n")
		local_rules = self.write_source(
			"relocated/SKILL.local.md", "portable local rules\n"
		)
		self.write_source(
			"relocated/.privacy.local.json", '["private-sentinel"]\n'
		)
		linked_directory = self.repo_root / "linked-skill"
		linked_directory.symlink_to(relocated_script.parent.parent, target_is_directory=True)
		payload = json.dumps(
			{
				"hook_event_name": "UserPromptSubmit",
				"session_id": "relocated-test-session",
				"cwd": str(self.repo_root),
				"prompt": "检查规则加载",
			}
		)
		for include_local in (True, False):
			with self.subTest(include_local=include_local):
				if not include_local:
					local_rules.unlink()
				result = subprocess.run(
					[sys.executable, str(linked_directory / "scripts/skillnull_gate.py")],
					input=payload,
					cwd=self.repo_root / "src",
					capture_output=True,
					text=True,
					check=True,
					env={**os.environ, "HOME": str(self.repo_root)},
				)
				context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
				self.assertIn("portable main rules", context)
				self.assertEqual("portable local rules" in context, include_local)
				self.assertNotIn("private-sentinel", context)
				self.assertNotIn("## 本地知识库", context)


if __name__ == "__main__":
	unittest.main()
