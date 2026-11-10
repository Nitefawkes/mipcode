"""Run the local fixture and delivery checks for the one-shot pilot."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mipdelta.brief import ARMS, DEFAULT_MAX_BYTES, compile_brief


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def run_oracle(case: dict, patch: dict[str, str]) -> dict:
    with tempfile.TemporaryDirectory(prefix="mipcode-brief-") as tmp:
        workspace = Path(tmp)
        for name, source in (case["files"] | patch).items():
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8", newline="\n")
        cmd = "import sys\nsys.path.insert(0, '.')\n" + case["oracle"]
        proc = subprocess.run(
            [sys.executable, "-I", "-B", "-c", cmd], cwd=workspace,
            capture_output=True, text=True, timeout=10,
        )
        error = proc.stderr.rstrip().splitlines()
        kind = error[-1].split(":", 1)[0].rsplit(".", 1)[-1] if error else None
        return {"exit_code": proc.returncode, "exception": kind}


def verify_brief(files, brief, spans):
    source_lines = {path: source.splitlines() for path, source in files.items()}
    output = brief.splitlines()
    offset = 2 if brief else 0
    delivered = set()
    for span in spans:
        assert 1 <= span.start <= span.end <= len(source_lines[span.path])
        assert output[offset].startswith(f"{span.path}:{span.start}-{span.end} [")
        offset += 1
        for number in range(span.start, span.end + 1):
            assert output[offset] == f"{number}: {source_lines[span.path][number - 1]}"
            delivered.add((span.path, number))
            offset += 1
    assert offset == len(output)
    return delivered


EXPECTED_FAILURES = {
    "SB01": ("AssertionError", "TypeError"),
    "SB02": ("AssertionError", "AssertionError"),
    "SB03": ("ValueError", "InvalidRank"),
    "SB04": ("AssertionError", "AssertionError"),
}


def verify_sibling_bounds():
    files = {
        "seed.py": "from core import first, second, third\n"
                   "def preview():\n    return first(), second(), third()\n",
        "core.py": "def first():\n    return 1\n"
                   "def second():\n    return 2\n"
                   "def third():\n    return 3\n",
        "decoy.py": "def first():\n    return 4\n",
        "homonym.py": "from decoy import first\ndef wrong():\n    return first()\n",
        "third_party.py": "from core import third\ndef beyond_bound():\n    return third()\n",
    }
    for n in range(7):
        files[f"alt{n}.py"] = f"from core import first\ndef other{n}():\n    return first()\n"
    # Large wire budget isolates the graph bounds from normal wire fitting.
    _, spans = compile_brief(files, "preview", "lod", 10_000)
    siblings = [span for span in spans if span.label.startswith("L3 sibling")]
    assert len({span.path for span in siblings}) == 4
    assert all(span.path.startswith("alt") and "core.py:1" in span.label for span in siblings)
    sparse = {path: text for path, text in files.items() if not path.startswith("alt")}
    sparse["second_party.py"] = "from core import second\ndef another():\n    return second()\n"
    _, spans = compile_brief(sparse, "preview", "lod", 10_000)
    assert {span.path for span in spans if span.label.startswith("L3 sibling")} == {"second_party.py"}


def main() -> None:
    path = Path(__file__).with_name("fixtures.json")
    cases = json.loads(path.read_bytes())
    assert set(cases) == set(EXPECTED_FAILURES)
    verify_sibling_bounds()
    results = {
        "fixture_sha256": digest(path.read_bytes()),
        "byte_cap": DEFAULT_MAX_BYTES,
        "scope": "Offline initial source delivery and fixture behavior only",
        "sibling_bounds_and_homonyms_checked": True,
        "tasks": {},
    }
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=False)
    for ident, case in cases.items():
        files = case["files"]
        assert set(case["reference"]) <= set(files) and set(case["incomplete"]) <= set(files)
        anchors = {(file, line) for file, lines in case["anchors"].items() for line in lines}
        assert all(1 <= line <= len(files[file].splitlines()) for file, line in anchors)
        checks = {
            "pristine": run_oracle(case, {}),
            "reference": run_oracle(case, case["reference"]),
            "incomplete": run_oracle(case, case["incomplete"]),
        }
        assert checks["reference"]["exit_code"] == 0, (ident, checks)
        for variant, expected in zip(("pristine", "incomplete"), EXPECTED_FAILURES[ident]):
            assert checks[variant] == {"exit_code": 1, "exception": expected}, (ident, checks)

        task = {
            "oracle": checks,
            "sources_sha256": {p: digest(text.encode("utf-8")) for p, text in sorted(files.items())},
            "ticket_sha256": digest(case["ticket"].encode("utf-8")),
            "required_lines": len(anchors),
            "arms": {},
        }
        for arm in ARMS:
            brief, spans = compile_brief(files, case["ticket"], arm)
            reversed_files = dict(reversed(list(files.items())))
            assert (brief, spans) == compile_brief(reversed_files, case["ticket"], arm)
            assert len(brief.encode("utf-8")) <= DEFAULT_MAX_BYTES
            assert bool(brief) == (arm != "none")
            assert compile_brief(files, case["ticket"], arm, 0) == ("", ())
            assert compile_brief(files, case["ticket"], arm, 1) == ("", ())
            delivered = verify_brief(files, brief, spans)
            filename = f"{ident}-{arm}.md"
            (output / filename).write_bytes(brief.encode("utf-8"))
            task["arms"][arm] = {
                "bytes": len(brief.encode("utf-8")),
                "sha256": digest(brief.encode("utf-8")),
                "delivered_lines": len(delivered),
                "required_lines_present": len(anchors & delivered),
                "missing": sorted(anchors - delivered),
                "brief": filename,
            }
        results["tasks"][ident] = task
    results["passed"] = True
    (output / "preflight.json").write_bytes((json.dumps(results, indent=2) + "\n").encode())
    for ident, task in results["tasks"].items():
        print(ident, task["required_lines"], {
            arm: (data["required_lines_present"], data["bytes"])
            for arm, data in task["arms"].items()
        })
    print("PASS", output / "preflight.json")


if __name__ == "__main__":
    main()
