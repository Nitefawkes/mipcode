import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

from mipdelta.gateway import ContainedGatewaySession, json_bytes


def sha256_str(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class GatewayIntegrationTests(unittest.TestCase):
    def _create_pair(self, tmp: str, files: dict[str, str], test_commands: dict[str, list[str]] | None = None):
        base = Path(tmp)
        root_a = base / "arm_a" / "source"
        ev_a = base / "arm_a" / "evidence"
        root_b = base / "arm_b" / "source"
        ev_b = base / "arm_b" / "evidence"
        for root in (root_a, root_b):
            for rel, txt in files.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(txt.encode("utf-8"))
        sess_a = ContainedGatewaySession(root_a, "A", ev_a, test_commands=test_commands)
        sess_b = ContainedGatewaySession(root_b, "B", ev_b, test_commands=test_commands)
        return sess_a, sess_b

    def test_arm_a_and_arm_b_have_identical_tool_schemas_and_zero_mip_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess_a, sess_b = self._create_pair(
                tmp,
                {"pkg/core.py": "def f(x):\n    return x\n"},
                test_commands={"unit": [sys.executable, "-m", "unittest"]},
            )
            self.assertEqual(json_bytes(sess_a.tool_schemas()), json_bytes(sess_b.tool_schemas()))
            names = [s["name"] for s in sess_b.tool_schemas()]
            self.assertEqual(names, ["list", "read", "search", "continue", "write", "test", "finalize"])

    def test_read_search_and_local_write_have_zero_wire_overhead_in_arm_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            initial = {
                "pkg/core.py": "def compute(x):\n    return x + 1\n",
                "pkg/caller.py": "from .core import compute\ndef run(v):\n    return compute(v)\n",
            }
            sess_a, sess_b = self._create_pair(tmp, initial)

            for req in (
                {"tool": "read", "args": {"path": "pkg/core.py"}},
                {"tool": "search", "args": {"pattern": "compute"}},
                {
                    "tool": "write",
                    "args": {
                        "path": "pkg/core.py",
                        "expected_sha256": sha256_str(initial["pkg/core.py"]),
                        "edits": [{"old_text": "return x + 1", "new_text": "y = x * 2\n    return y + 1"}],
                    },
                },
            ):
                res_a = sess_a.dispatch(req)
                res_b = sess_b.dispatch(req)
                self.assertEqual(res_a["status"], "OK")
                self.assertEqual(res_b["status"], "OK")
                self.assertNotIn("impact", res_a)
                self.assertNotIn("impact", res_b)

            self.assertEqual(sess_b.metrics["advisory_writes"], 0)
            self.assertEqual(sess_b.metrics["advisory_utf8_bytes"], 0)

    def test_contract_mutating_write_emits_bounded_impact_and_clears_after_caller_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            initial = {
                "pkg/http.py": "def dump_header(iterable):\n    return ','.join(iterable)\n",
                "pkg/response.py": (
                    "from .http import dump_header\n"
                    "def bind_prop(fn):\n"
                    "    return fn\n"
                    "csp = bind_prop(dump_header)\n"
                ),
            }
            _, sess_b = self._create_pair(tmp, initial)

            # Mutate dump_header signature -> triggers callback_binding_review on pkg/response.py:4
            w1 = sess_b.dispatch({
                "tool": "write",
                "args": {
                    "path": "pkg/http.py",
                    "expected_sha256": sha256_str(initial["pkg/http.py"]),
                    "content": "def dump_header(iterable, *, strict=False):\n    return ','.join(iterable) if not strict else ''\n",
                },
            })
            self.assertEqual(w1["status"], "OK")
            self.assertIn("impact", w1)
            self.assertIn("[mip-delta]", w1["impact"])
            self.assertIn("pkg/response.py:4 callback_binding_review[static]", w1["impact"])
            self.assertLessEqual(len(w1["impact"].encode("utf-8")), 250)

            # Agent follows the advisory and repairs pkg/response.py with a lambda adapter
            w2 = sess_b.dispatch({
                "tool": "write",
                "args": {
                    "path": "pkg/response.py",
                    "expected_sha256": sha256_str(initial["pkg/response.py"]),
                    "edits": [
                        {
                            "old_text": "csp = bind_prop(dump_header)",
                            "new_text": "csp = bind_prop(lambda v: dump_header(v, strict=True))",
                        }
                    ],
                },
            })
            self.assertEqual(w2["status"], "OK")
            self.assertNotIn("impact", w2)
            fin = sess_b.dispatch({"tool": "finalize", "args": {}})
            self.assertEqual(fin["status"], "FINALIZED")
            self.assertEqual(fin["changed_files"], 2)

    def test_test_tool_ingests_sys_monitoring_shards_and_resolves_dynamic_dispatch_on_next_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            initial = {
                "pkg/__init__.py": "",
                "pkg/impl.py": (
                    "class Worker:\n"
                    "    def process(self, val):\n"
                    "        return val + 10\n"
                    "def run_all(items):\n"
                    "    return [item.process(5) for item in items]\n"
                ),
                "tests/test_impl.py": (
                    "import unittest\n"
                    "from pkg.impl import Worker, run_all\n"
                    "class ImplTest(unittest.TestCase):\n"
                    "    def test_run(self):\n"
                    "        self.assertEqual(run_all([Worker()]), [15])\n"
                ),
            }
            _, sess_b = self._create_pair(
                tmp,
                initial,
                test_commands={"unit": [sys.executable, "-m", "unittest", "discover", "-s", "tests"]},
            )
            # 1. Run test tool -> captures Worker.process call at item.process(5) via sys.monitoring
            t_res = sess_b.dispatch({"tool": "test", "args": {"command": "unit"}})
            self.assertEqual(t_res["status"], "OK")
            assert sess_b.impact is not None
            self.assertGreater(sess_b.impact.ingested_runtime_edges_count, 0)

            # 2. Mutate Worker.process to require keyword `policy` -> item.process(5) resolves as [rt-obs]!
            w_res = sess_b.dispatch({
                "tool": "write",
                "args": {
                    "path": "pkg/impl.py",
                    "expected_sha256": sha256_str(initial["pkg/impl.py"]),
                    "edits": [
                        {
                            "old_text": "    def process(self, val):\n        return val + 10",
                            "new_text": "    def process(self, val, *, policy):\n        return val + policy",
                        }
                    ],
                },
            })
            self.assertIn("impact", w_res)
            self.assertIn("required_keyword_missing[rt-obs]", w_res["impact"])

    def test_write_before_test_emits_new_rt_obs_advisory_on_test_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            initial = {
                "pkg/__init__.py": "",
                "pkg/impl.py": (
                    "class Worker:\n"
                    "    def process(self, val):\n"
                    "        return val + 10\n"
                    "def get_worker():\n"
                    "    return Worker()\n"
                ),
                "pkg/pipeline.py": (
                    "from .impl import get_worker\n"
                    "class Pipeline:\n"
                    "    def process(self, val):\n"
                    "        w = get_worker()\n"
                    "        return w.process(val)\n"
                ),
                "tests/test_impl.py": (
                    "import unittest\n"
                    "from pkg.pipeline import Pipeline\n"
                    "class ImplTest(unittest.TestCase):\n"
                    "    def test_run(self):\n"
                    "        self.assertEqual(Pipeline().process(5), 15)\n"
                ),
            }
            _, sess_b = self._create_pair(
                tmp,
                initial,
                test_commands={"unit": [sys.executable, "-m", "unittest", "discover", "-s", "tests"]},
            )
            # 1. Agent writes Worker.process BEFORE running test (adding optional parameter `scale=1`)
            w_res = sess_b.dispatch({
                "tool": "write",
                "args": {
                    "path": "pkg/impl.py",
                    "expected_sha256": sha256_str(initial["pkg/impl.py"]),
                    "edits": [
                        {
                            "old_text": "    def process(self, val):\n        return val + 10",
                            "new_text": "    def process(self, val, *, scale=1):\n        return (val + 10) * scale",
                        }
                    ],
                },
            })
            self.assertEqual(w_res["status"], "OK")
            self.assertNotIn("impact", w_res)

            # 2. Agent runs test -> sys.monitoring captures Pipeline.process -> Worker.process
            # and attaches newly discovered [rt-obs] delegating_wrapper_review to the test response!
            t_res = sess_b.dispatch({"tool": "test", "args": {"command": "unit"}})
            self.assertEqual(t_res["status"], "OK")
            self.assertIn("impact", t_res)
            self.assertIn("pkg/pipeline.py:5 delegating_wrapper_review[rt-obs] Pipeline.process", t_res["impact"])

    def test_auto_pyi_stub_discovery_emits_type_cand_advisory(self):
        with tempfile.TemporaryDirectory() as tmp:
            initial = {
                "pkg/__init__.py": "",
                "pkg/reporter.py": (
                    "class Reporter:\n"
                    "    def system_message(self, level, msg):\n"
                    "        return (level, msg)\n"
                ),
                "pkg/nodes.py": (
                    "class Document:\n"
                    "    def __init__(self, reporter):\n"
                    "        self.reporter = reporter\n"
                ),
                "pkg/nodes.pyi": (
                    "from .reporter import Reporter\n"
                    "class Document:\n"
                    "    reporter: Reporter\n"
                    "class Node:\n"
                    "    document: Document\n"
                ),
                "pkg/transforms.py": (
                    "from .nodes import Node\n"
                    "class Transform(Node):\n"
                    "    def apply(self):\n"
                    "        return self.document.reporter.system_message(1, 'ok')\n"
                ),
            }
            _, sess_b = self._create_pair(tmp, initial)
            w_res = sess_b.dispatch({
                "tool": "write",
                "args": {
                    "path": "pkg/reporter.py",
                    "expected_sha256": sha256_str(initial["pkg/reporter.py"]),
                    "content": (
                        "class Reporter:\n"
                        "    def system_message(self, level, msg, *, strict):\n"
                        "        return (level, msg, strict)\n"
                    ),
                },
            })
            self.assertEqual(w_res["status"], "OK")
            self.assertIn("impact", w_res)
            self.assertIn("pkg/transforms.py:4 required_keyword_missing[type-cand] Reporter.system_message", w_res["impact"])

    def test_out_of_band_disk_mutation_marks_impact_unavailable_and_blocks_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            initial = {
                "pkg/a.py": "def alpha(x):\n    return x\n",
                "pkg/b.py": "def beta(y):\n    return y\n",
            }
            _, sess_b = self._create_pair(tmp, initial)
            # Mutate pkg/b.py out of band on disk (same byte count: y -> z)
            (sess_b.root / "pkg/b.py").write_bytes(b"def beta(z):\n    return z\n")

            w_res = sess_b.dispatch({
                "tool": "write",
                "args": {
                    "path": "pkg/a.py",
                    "expected_sha256": sha256_str(initial["pkg/a.py"]),
                    "content": "def alpha(x):\n    return x + 1\n",
                },
            })
            self.assertEqual(w_res["status"], "OK")
            self.assertIn("Impact unavailable", w_res["impact"])

            fin = sess_b.dispatch({"tool": "finalize", "args": {}})
            self.assertEqual(fin["status"], "ERROR")
            self.assertEqual(fin["code"], "OUT_OF_BAND_CHANGE")

    def test_cli_check_git_and_run_pytest_drop_in_hook(self):
        import io
        import subprocess
        from contextlib import redirect_stdout
        from mipdelta.cli import main_cli

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "git_repo"
            (repo / "pkg").mkdir(parents=True)
            (repo / "pkg/__init__.py").write_text("", encoding="utf-8")
            (repo / "pkg/impl.py").write_text(
                "class Worker:\n"
                "    def process(self, val):\n"
                "        return val + 1\n",
                encoding="utf-8",
            )
            (repo / "pkg/pipeline.py").write_text(
                "from .impl import Worker\n"
                "class Pipeline:\n"
                "    def process(self, val):\n"
                "        w = getattr(__import__('pkg.impl', fromlist=['Worker']), 'Worker')()\n"
                "        return w.process(val)\n",
                encoding="utf-8",
            )
            (repo / "test_pkg.py").write_text(
                "from pkg.pipeline import Pipeline\n"
                "def test_pipe():\n"
                "    assert Pipeline().process(5) == 6\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

            # 1. Local non-contract edit -> --check-git emits 0 bytes
            (repo / "pkg/impl.py").write_text(
                "class Worker:\n"
                "    def process(self, val):\n"
                "        return val + 2\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main_cli(["--root", str(repo), "--check-git"])
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue(), "")

            # 2. Contract edit adding optional scale=1 to Worker.process, then --run-pytest
            (repo / "pkg/impl.py").write_text(
                "class Worker:\n"
                "    def process(self, val, *, scale=1):\n"
                "        return (val + 1) * scale\n",
                encoding="utf-8",
            )
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                rc2 = main_cli([
                    "--root",
                    str(repo),
                    "--run-pytest",
                    "--pytest-args=-q",
                    "--fail-on-findings",
                ])
            self.assertEqual(rc2, 1)
            self.assertIn("pkg/pipeline.py:5 delegating_wrapper_review[rt-obs] Pipeline.process", buf2.getvalue())


if __name__ == "__main__":
    unittest.main()
