#!/usr/bin/env python3
"""Reproducible before/after metrics for the v1.1 restructure.

Usage:
    .venv/bin/python scripts/metrics_report.py          # markdown table
    .venv/bin/python scripts/metrics_report.py --json   # machine readable

Stdlib only. Every number here can be recomputed from a clean checkout so the
baseline commit and the post-refactor commit are directly comparable.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SOURCE_ROOTS = {
    "brain": ("brain", {".py"}),
    "senses": ("senses", {".py"}),
    "protocol": ("protocol", {".py"}),
    "tests": ("tests", {".py"}),
    "scripts": ("scripts", {".py", ".sh"}),
    "shell_macos": ("shell_macos/Sources", {".swift"}),
    "body_web": ("body_web", {".py", ".js", ".html", ".css"}),
    "body_godot": ("body_godot", {".gd"}),
}

EXCLUDED_DIRS = {".git", ".venv", ".build", "dist", "node_modules", "__pycache__"}

PRODUCT_ROOTS = ("brain", "senses", "protocol", "scripts", "shell_macos", "body_web", "body_godot")


def iter_files(root: Path, suffixes: set[str]):
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        yield path


def count_lines(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def loc_table() -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for name, (rel, suffixes) in SOURCE_ROOTS.items():
        base = ROOT / rel
        if not base.exists():
            result[name] = {"files": 0, "lines": 0}
            continue
        files = list(iter_files(base, suffixes))
        result[name] = {"files": len(files), "lines": sum(count_lines(p) for p in files)}
    return result


def largest_files(limit: int = 10) -> list[dict[str, object]]:
    entries = []
    for name, (rel, suffixes) in SOURCE_ROOTS.items():
        base = ROOT / rel
        if not base.exists():
            continue
        for path in iter_files(base, suffixes):
            entries.append(
                {"path": str(path.relative_to(ROOT)), "lines": count_lines(path), "area": name}
            )
    entries.sort(key=lambda item: item["lines"], reverse=True)
    return entries[:limit]


def swift_test_count() -> int:
    tests_dir = ROOT / "shell_macos" / "Tests"
    if not tests_dir.exists():
        return 0
    count = 0
    for path in tests_dir.rglob("*.swift"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        count += len(re.findall(r"\bfunc test\w*\s*\(", text))
    return count


def python_test_count(python: str) -> int | None:
    try:
        proc = subprocess.run(
            [python, "-m", "pytest", "--collect-only", "-q"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+)\s+tests?\s+collected", proc.stdout + proc.stderr)
    return int(match.group(1)) if match else None


REF_SUFFIXES = {
    ".py", ".swift", ".sh", ".js", ".html", ".css", ".gd",
    ".json", ".md", ".yaml", ".yml", ".toml", ".txt",
}


def reference_counts() -> dict[str, dict[str, int]]:
    """Count `body_web` / `body_godot` references, split product / tests / docs."""
    tokens = ("body_web", "body_godot")
    groups = {
        "product": tuple(ROOT / rel for rel in PRODUCT_ROOTS),
        "tests": (ROOT / "tests",),
        "docs": (ROOT / "docs", ROOT / "README.md"),
    }
    result: dict[str, dict[str, int]] = {}
    for token in tokens:
        result[token] = {}
        for group, bases in groups.items():
            files = 0
            occurrences = 0
            for base in bases:
                if base.is_file():
                    candidates = [base]
                else:
                    candidates = [
                        p for p in base.rglob("*")
                        if p.is_file() and p.suffix in REF_SUFFIXES
                    ]
                for path in candidates:
                    if any(part in EXCLUDED_DIRS for part in path.parts):
                        continue
                    try:
                        text = path.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    hits = text.count(token)
                    if hits:
                        files += 1
                        occurrences += hits
            result[token][group] = {"files": files, "occurrences": occurrences}
    return result


def bundle_size() -> int | None:
    app = ROOT / "dist" / "Qimingxing.app"
    if not app.exists():
        return None
    try:
        proc = subprocess.run(
            ["du", "-sk", str(app)], capture_output=True, text=True, check=True
        )
        return int(proc.stdout.split()[0]) * 1024
    except (OSError, ValueError, IndexError, subprocess.CalledProcessError):
        pass
    total = 0
    for path in app.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def git_commit() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
        )
        return proc.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def collect(python: str) -> dict[str, object]:
    version = (ROOT / "version.txt").read_text(encoding="utf-8").strip()
    bundle = bundle_size()
    return {
        "commit": git_commit(),
        "version": version,
        "python_tests": python_test_count(python),
        "swift_tests": swift_test_count(),
        "loc": loc_table(),
        "largest_files": largest_files(),
        "references": reference_counts(),
        "bundle_bytes": bundle,
        "bundle_mb": round(bundle / (1024 * 1024), 1) if bundle else None,
    }


def as_markdown(data: dict[str, object]) -> str:
    lines = [
        f"commit: `{data['commit']}`  version: `{data['version']}`",
        "",
        "| area | files | lines |",
        "|---|---:|---:|",
    ]
    for name, stats in data["loc"].items():
        lines.append(f"| {name} | {stats['files']} | {stats['lines']} |")
    lines += ["", f"python tests: {data['python_tests']}", f"swift tests: {data['swift_tests']}", ""]
    lines += ["| largest file | lines |", "|---|---:|"]
    for entry in data["largest_files"]:
        lines.append(f"| `{entry['path']}` | {entry['lines']} |")
    lines += ["", "| token | group | files | occurrences |", "|---|---|---:|---:|"]
    for token, groups in data["references"].items():
        for group, stats in groups.items():
            lines.append(f"| `{token}` | {group} | {stats['files']} | {stats['occurrences']} |")
    lines += ["", f"bundle: {data['bundle_mb']} MB" if data["bundle_mb"] else "bundle: n/a"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    parser.add_argument("--python", default=sys.executable, help="interpreter used for pytest")
    args = parser.parse_args()
    data = collect(args.python)
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else as_markdown(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
