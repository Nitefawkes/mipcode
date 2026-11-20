"""Tests validating repo-root SKILL.md agent packaging and CLI parity."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO_ROOT / "SKILL.md"
CLI_PATH = REPO_ROOT / "src" / "mipdelta" / "cli.py"


def _registered_cli_flags() -> set[str]:
    cli_text = CLI_PATH.read_text(encoding="utf-8")
    flags = set(re.findall(r'add_argument\([^)]*?"(--[a-z0-9-]+)"', cli_text, flags=re.DOTALL))
    # --help is provided automatically by argparse.ArgumentParser
    flags.add("--help")
    return flags


def test_skill_md_frontmatter_and_cli_flags():
    assert SKILL_PATH.is_file(), "SKILL.md must exist at repository root"
    text = SKILL_PATH.read_text(encoding="utf-8")

    fm = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, flags=re.DOTALL)
    assert fm is not None, "SKILL.md must start with YAML frontmatter"
    frontmatter = fm.group(1)
    assert re.search(r"^name:\s*mipcode\s*$", frontmatter, flags=re.MULTILINE)
    assert re.search(r"^description:\s*\S+", frontmatter, flags=re.MULTILINE)

    assert "python -m pip install coherion-mipcode" in text
    assert not re.search(r"pip install mipcode\b", text, flags=re.IGNORECASE)

    # Every --flag documented in SKILL.md must be a real flag in mipdelta.cli
    documented_flags = {f"--{name}" for name in re.findall(r"(?<![A-Za-z0-9-])--([a-z][a-z0-9-]+)", text)}
    registered = _registered_cli_flags()
    unknown = sorted(documented_flags - registered)
    assert not unknown, f"SKILL.md documents unknown CLI flags: {unknown}"


def test_skill_md_honest_posture_and_clean_links():
    text = SKILL_PATH.read_text(encoding="utf-8")

    for required_url in (
        "https://coherion.net/mipcode",
        "https://coherion.net/mipcode/llms.txt",
        "https://coherion.net/mipcode/skill.md",
        "https://coherion.net/mipcode/findings.json",
        "https://coherion.net/mipcode/results",
        "https://github.com/Nitefawkes/mipcode/issues",
    ):
        assert required_url in text

    assert "20 findings over 14 distinct sites across 43 distinct repositories" in text
    assert "4 of 14" in text
    assert "0 of 2" in text
    assert not re.search(
        r"zero false positives|no false positives|got this fixed upstream|fixed upstream by mipcode",
        text,
        flags=re.IGNORECASE,
    )
    # No local filesystem paths, emails, or loopback URLs
    assert not re.search(r"""(?:^|[\s"'(=,])/(?:tmp|home|Users|var|etc|root)/""", text)
    assert not re.search(r"""(?:^|[\s"'(=,])[A-Za-z]:\\[\\A-Za-z0-9_.$-]""", text)
    assert not re.search(r"localhost:\d+|127\.0\.0\.1:\d+", text)
