"""Regression checks for package version coherency, hook config, and doc link integrity."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import mipcode
import mipdelta


REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_project_version() -> str:
    in_project = False
    for raw_line in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("["):
            in_project = line == "[project]"
        elif in_project:
            match = re.match(r'^version\s*=\s*"([^"]+)"', line)
            if match:
                return match.group(1)
    raise AssertionError("Could not find [project] version in pyproject.toml")


def test_package_versions_match_pyproject():
    expected = _pyproject_project_version()
    assert mipdelta.__version__ == expected
    assert mipcode.__version__ == expected
    assert "__version__" in mipdelta.__all__


def test_cli_version_flag_outputs_package_version():
    expected = _pyproject_project_version()
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-B", "-m", "mipdelta.cli", "--version"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"mip-delta {expected}"


def test_pre_commit_hooks_yaml_is_coherent():
    text = (REPO_ROOT / ".pre-commit-hooks.yaml").read_text(encoding="utf-8")
    assert "- id: mip-delta" in text
    assert "entry: mip-delta " in text
    assert "language: python" in text
    assert "pass_filenames: false" in text


def test_markdown_links_resolve_to_tracked_files_and_headings():
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("Not a git checkout")
    tracked = set(
        subprocess.check_output(["git", "-C", str(REPO_ROOT), "ls-files"], text=True).splitlines()
    )
    md_files = sorted(p for p in tracked if p.endswith(".md"))

    def slugify(heading: str) -> str:
        s = re.sub(r"`+", "", heading.strip().lower())
        s = re.sub(r"[^\w\s-]", "", s)
        return re.sub(r"\s+", "-", s)

    anchors: dict[str, set[str]] = {}
    for md in md_files:
        heads: set[str] = set()
        counts: dict[str, int] = {}
        for line in (REPO_ROOT / md).read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
            if m:
                slug = slugify(m.group(1))
                n = counts.get(slug, -1) + 1
                counts[slug] = n
                heads.add(slug if n == 0 else f"{slug}-{n}")
        anchors[md] = heads

    link_re = re.compile(r"\[[^\]]+\]\(([^)#]+)?(?:#([^)]+))?\)")
    broken: list[str] = []
    for md in md_files:
        text = (REPO_ROOT / md).read_text(encoding="utf-8", errors="replace")
        parent = (REPO_ROOT / md).parent
        for m in link_re.finditer(text):
            target = (m.group(1) or "").strip()
            frag = (m.group(2) or "").strip()
            if "://" in target or target.startswith("mailto:"):
                continue
            if not target and not frag:
                continue
            if not target:
                dest_rel = md
            else:
                try:
                    dest_rel = (parent / target).resolve().relative_to(REPO_ROOT.resolve()).as_posix()
                except ValueError:
                    broken.append(f"{md}: {target} outside repo")
                    continue
                if dest_rel not in tracked and not any(
                    t.startswith(dest_rel.rstrip("/") + "/") for t in tracked
                ):
                    broken.append(f"{md}: {target} -> {dest_rel} not tracked")
                    continue
            if frag and dest_rel in anchors and frag not in anchors[dest_rel]:
                broken.append(f"{md}: {target}#{frag} missing heading in {dest_rel}")
    assert not broken, "Broken markdown links:\n" + "\n".join(broken)
