#!/usr/bin/env python3
"""Scan publishable Git content without printing matched private text."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


LOCAL_TERMS_FILE = ".privacy.local.json"
TERMS_ENV = "PRIVATE_CONTENT_TERMS_JSON"
MAX_FILE_SIZE = 8 * 1024 * 1024
RULES = {
    "personal-path": re.compile(r"/(?:Users|home)/[\w.-]+(?:/|\b)|[A-Za-z]:[\\/]Users[\\/][\w.-]+", re.I),
    "private-network": re.compile(
        r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
        r"|https?://[^\s/]+\.(?:internal|corp|lan|local)(?=[:/\s]|$)", re.I
    ),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "access-token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
        r"|sk-(?:proj-)?[A-Za-z0-9_-]{32,}|AKIA[A-Z0-9]{16})\b"
    ),
    "credential-literal": re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd)"
        r"\s*[:=]\s*[\"'][A-Za-z0-9_+/=-]{16,}[\"']", re.I
    ),
}
LOCAL_FILE = re.compile(
    r"(?:^|/)(?:SKILL\.local\.md|\.privacy\.local\.json|\.env(?:\.(?!example$)[^/]*)?"
    r"|id_rsa|id_ed25519|[^/]+\.(?:pem|key|p12|pfx))$", re.I
)


class ScanError(Exception):
    """A scan could not complete safely."""


def git(repo: Path, *arguments: str) -> bytes:
    result = subprocess.run(["git", *arguments], cwd=repo, capture_output=True)
    if result.returncode:
        raise ScanError("Git read failed; scan incomplete.")
    return result.stdout


def load_terms(repo: Path, required: bool) -> list[str]:
    sources = []
    local_file = repo / LOCAL_TERMS_FILE
    if local_file.is_symlink():
        raise ScanError("Private terms file must be a regular file.")
    if local_file.exists():
        if not local_file.is_file():
            raise ScanError("Private terms file must be a regular file.")
        if os.name != "nt" and stat.S_IMODE(local_file.stat().st_mode) & 0o077:
            raise ScanError(
                "Private terms file must not grant group or other access (use 600)."
            )
        sources.append(local_file.read_text(encoding="utf-8"))
    if os.environ.get(TERMS_ENV):
        sources.append(os.environ[TERMS_ENV])
    terms = set()
    for source in sources:
        try:
            values = json.loads(source)
        except json.JSONDecodeError as error:
            raise ScanError("Private terms must be a JSON string array.") from error
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ScanError("Private terms must be a JSON string array.")
        terms.update(value.strip().casefold() for value in values)
    if required and not terms:
        raise ScanError("Private terms missing; configure local terms or the CI secret.")
    return sorted(terms)


def emit_result(
    output_format: str,
    status: str,
    exit_code: int,
    failures: list[str],
    message: str,
    private_terms_configured: bool,
) -> None:
    unique_failures = list(dict.fromkeys(failures))
    if output_format == "json":
        print(
            json.dumps(
                {
                    "status": status,
                    "exit_code": exit_code,
                    "failures": unique_failures,
                    "message": message,
                    "private_terms_configured": private_terms_configured,
                },
                ensure_ascii=False,
            )
        )
        return
    if status == "blocked":
        print("Private content gate blocked publication:", file=sys.stderr)
        for failure in unique_failures:
            print(f"  {failure}", file=sys.stderr)
        return
    output = sys.stderr if status == "error" else sys.stdout
    print(message, file=output)


class Scanner:
    def __init__(self, repo: Path, terms: list[str]) -> None:
        self.repo = repo
        self.terms = terms
        self.failures: list[str] = []
        self.visited: set[tuple[str, str, str]] = set()

    def categories(self, text: str) -> list[str]:
        categories = [name for name, pattern in RULES.items() if pattern.search(text)]
        if any(term in text.casefold() for term in self.terms):
            categories.append("private-term")
        return categories

    def check_text(self, scope: str, text: str) -> None:
        for line_number, line in enumerate(text.splitlines(), 1):
            categories = self.categories(line)
            if categories:
                self.failures.append(f"{scope}:{line_number}: {', '.join(categories)}")

    def check_file(self, scope: str, name: str, content: bytes, mode: str) -> None:
        path_categories = self.categories(name)
        safe_name = "[redacted-path]" if path_categories else json.dumps(name, ensure_ascii=True)
        location = f"{scope}:{safe_name}"
        if path_categories:
            self.failures.append(f"{location}: {', '.join(path_categories)}")
        if LOCAL_FILE.search(name):
            self.failures.append(f"{location}: local-only-file")
        if mode not in {"100644", "100755"}:
            self.failures.append(f"{location}: unsupported-file-mode")
            return
        if len(content) > MAX_FILE_SIZE:
            self.failures.append(f"{location}: oversized-file-needs-review")
            return
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            self.failures.append(f"{location}: binary-file-needs-review")
            return
        if "\0" in text:
            self.failures.append(f"{location}: binary-file-needs-review")
            return
        self.check_text(location, text)

    def check_entries(self, scope: str, entries: bytes, staged: bool = False) -> None:
        for record in entries.split(b"\0"):
            if not record:
                continue
            metadata, raw_name = record.split(b"\t", 1)
            fields = metadata.decode("ascii").split()
            mode, object_id = (fields[0], fields[1]) if staged else (fields[0], fields[2])
            if staged and fields[2] != "0":
                raise ScanError("Unmerged index; scan incomplete.")
            name = raw_name.decode("utf-8")
            key = (object_id, name, mode)
            if key in self.visited:
                continue
            self.visited.add(key)
            content = git(self.repo, "cat-file", "blob", object_id) if mode in {"100644", "100755"} else b""
            self.check_file(scope, name, content, mode)

    def check_index(self) -> None:
        self.check_entries("index", git(self.repo, "ls-files", "--stage", "-z"), staged=True)

    def check_worktree(self) -> None:
        names = git(self.repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        for raw_name in sorted(set(names.split(b"\0")) - {b""}):
            name = raw_name.decode("utf-8")
            target = self.repo / name
            if target.is_symlink():
                self.check_file("worktree", name, b"", "120000")
            elif target.is_file():
                self.check_file("worktree", name, target.read_bytes(), "100644")

    def check_history(self) -> None:
        if git(self.repo, "rev-parse", "--is-shallow-repository").strip() == b"true":
            raise ScanError("Shallow history cannot be audited; fetch complete history.")
        revisions = git(self.repo, "rev-list", "--all").decode().splitlines()
        head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=self.repo, capture_output=True)
        if head.returncode == 0:
            revisions.extend(git(self.repo, "rev-list", "HEAD").decode().splitlines())
        for revision in dict.fromkeys(revisions):
            self.check_text(f"commit:{revision[:12]}", git(self.repo, "cat-file", "commit", revision).decode("utf-8"))
            self.check_entries(f"history:{revision[:12]}", git(self.repo, "ls-tree", "-r", "-z", revision))
        for record in git(self.repo, "for-each-ref", "--format=%(objectname) %(objecttype) %(refname)").decode().splitlines():
            object_id, object_type, reference = record.split()
            self.check_text("ref-name", reference)
            if object_type == "tree":
                self.check_entries(f"snapshot:{object_id[:12]}", git(self.repo, "ls-tree", "-r", "-z", object_id))
            elif object_type == "tag":
                self.check_text(f"tag:{object_id[:12]}", git(self.repo, "cat-file", "tag", object_id).decode("utf-8"))
            elif object_type != "commit":
                raise ScanError("Unsupported reference object; scan incomplete.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Read the Git index instead of working files.")
    parser.add_argument("--history", action="store_true", help="Also audit all reachable commits and tags.")
    parser.add_argument("--message-file", type=Path, help="Check a pending commit message.")
    parser.add_argument("--require-private-terms", action="store_true", help="Fail if no private terms are configured.")
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        dest="output_format",
        help="Choose human-readable text or machine-readable JSON output.",
    )
    options = parser.parse_args()
    try:
        repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
        scanner = Scanner(repo, load_terms(repo, options.require_private_terms))
        if options.message_file:
            scanner.check_text("pending-commit-message", options.message_file.read_text(encoding="utf-8"))
        elif options.staged:
            scanner.check_index()
        else:
            scanner.check_worktree()
        if options.history:
            scanner.check_history()
        if scanner.failures:
            emit_result(
                options.output_format,
                "blocked",
                1,
                scanner.failures,
                "Private content gate blocked publication.",
                bool(scanner.terms),
            )
            return 1
        message = "Private content gate passed" + (
            " (generic patterns only)." if not scanner.terms else "."
        )
        emit_result(
            options.output_format,
            "passed",
            0,
            [],
            message,
            bool(scanner.terms),
        )
        return 0
    except (ScanError, OSError, UnicodeError) as error:
        message = str(error) if isinstance(error, ScanError) else "Unreadable input; scan incomplete."
        emit_result(options.output_format, "error", 2, [], message, False)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
