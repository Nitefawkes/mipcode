from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call

from mipdelta.delta import analyze_delta, format_advisory, rank_findings
from mipdelta.facts import parse_file
from mipdelta.model import ObservedCallEdge
from mipdelta.monitoring import ScopedCallCollector, load_runtime_edges
from mipdelta.resolve import build_index, resolve_call_with_provenance
from mipdelta.session import DeltaSession


class SessionStubsAndMonitoringTests(unittest.TestCase):
    def test_delta_session_local_edit_parses_only_one_file_and_skips_cross_file_scan(self):
        workspace = {
            f"pkg/mod_{i}.py": f"def helper_{i}(x):\n    return x + {i}\n"
            for i in range(40)
        }
        session = DeltaSession.from_workspace(workspace)
        self.assertEqual(session.cache.stats.parses, 40)

        res = session.apply_write("pkg/mod_7.py", "def helper_7(x):\n    y = x * 2\n    return y + 7\n")
        self.assertTrue(res.content_changed)
        self.assertFalse(res.cross_file_triggered)
        self.assertEqual(res.parses_executed, 1)
        self.assertEqual(res.candidate_calls_inspected, 0)
        self.assertEqual(res.findings, ())
        self.assertEqual(res.advisory_utf8_bytes, 0)

        # Identical write executes 0 parses
        noop = session.apply_write("pkg/mod_7.py", "def helper_7(x):\n    y = x * 2\n    return y + 7\n")
        self.assertFalse(noop.content_changed)
        self.assertEqual(noop.parses_executed, 0)

    def test_delta_session_custody_detects_same_size_external_drift(self):
        workspace = {"pkg/a.py": "def f():\n    return 1\n"}
        session = DeltaSession.from_workspace(workspace)
        self.assertTrue(session.verify_custody(workspace).clean)

        # Same-size external modification (1 -> 2)
        drifted = {"pkg/a.py": "def f():\n    return 2\n"}
        report = session.verify_custody(drifted)
        self.assertFalse(report.clean)
        self.assertEqual(report.modified_externally, ("pkg/a.py",))

    def test_pyi_stub_chained_receiver_resolves_as_static_type_candidate(self):
        before = {
            "pkg/reporter.py": "class Reporter:\n    def attach_observer(self, fn):\n        pass\n",
            "pkg/parser.py": (
                "class Parser:\n"
                "    def setup(self, cb):\n"
                "        self.document.reporter.attach_observer(cb)\n"
            ),
            "pkg/untyped.py": (
                "class Untyped:\n"
                "    def setup(self, cb):\n"
                "        self.mystery.reporter.attach_observer(cb)\n"
            ),
        }
        after = {
            "pkg/reporter.py": "class Reporter:\n    def attach_observer(self, fn, *, level):\n        return level\n",
            "pkg/parser.py": before["pkg/parser.py"],
            "pkg/untyped.py": before["pkg/untyped.py"],
        }
        stubs = {
            "pkg/document.pyi": "from .reporter import Reporter\nclass Document:\n    reporter: Reporter\n",
            "pkg/parser.pyi": "from .document import Document\nclass Parser:\n    document: Document\n",
        }

        # Without stubs, chained dynamic attribute self.document.reporter abstains
        self.assertEqual(analyze_delta(before, after), [])

        # With .pyi stubs, Parser.setup resolves as static_type_candidate while Untyped.setup abstains
        with_stubs = analyze_delta(before, after, stub_files=stubs)
        self.assertEqual(len(with_stubs), 1)
        self.assertEqual(with_stubs[0].path, "pkg/parser.py")
        self.assertEqual(with_stubs[0].resolution_source, "static_type_candidate")

    @unittest.skipUnless(sys.version_info >= (3, 12), "sys.monitoring requires Python 3.12 or later")
    def test_sys_monitoring_scoped_collector_disable_modes_and_runtime_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod_path = Path(tmp) / "dyn_pkg.py"
            mod_path.write_text(
                "class Alpha:\n"
                "    def handle(self, x):\n"
                "        return x + 1\n"
                "class Beta:\n"
                "    def handle(self, x):\n"
                "        return x + 2\n"
                "def dispatch_loop(items):\n"
                "    total = 0\n"
                "    for obj in items:\n"
                "        total += obj.handle(1)\n"
                "    return total\n",
                encoding="utf-8",
            )
            ns: dict[str, object] = {"__file__": str(mod_path), "__name__": "dyn_pkg"}
            code_obj = compile(mod_path.read_text(encoding="utf-8"), str(mod_path), "exec")
            exec(code_obj, ns)
            alpha_cls = ns["Alpha"]
            beta_cls = ns["Beta"]
            dispatch_loop = ns["dispatch_loop"]
            items = [alpha_cls(), beta_cls()] * 50  # 100 dynamic calls at 1 bytecode offset

            # k=1 DISABLE: only 1 target callback at the loop call site
            c1 = ScopedCallCollector([tmp], max_events_per_site=1)
            c1.start()
            try:
                out1 = dispatch_loop(items)
            finally:
                c1.stop()
            self.assertEqual(out1, 250)
            handle_edges_k1 = [e for e in c1.observed_edges if e.callee_name == "handle"]
            self.assertEqual(len(handle_edges_k1), 1)
            self.assertLessEqual(c1.target_callbacks, 4)

            # k=8 DISABLE: captures BOTH Alpha and Beta polymorphic receivers at the same offset while capping callbacks at 8!
            c8 = ScopedCallCollector([tmp], max_events_per_site=8)
            c8.start()
            try:
                out8 = dispatch_loop(items)
            finally:
                c8.stop()
            self.assertEqual(out8, 250)
            receivers_k8 = {e.receiver_type for e in c8.observed_edges if e.callee_name == "handle"}
            self.assertEqual(receivers_k8, {"Alpha", "Beta"})
            self.assertLessEqual(c8.target_callbacks, 12)

            # Feed observed runtime edges into DeltaSession to resolve dynamic dispatch with observed_runtime provenance
            before = {
                "dyn_pkg.py": (
                    "class Alpha:\n"
                    "    def handle(self, x):\n"
                    "        return x + 1\n"
                    "def dispatch_loop(items):\n"
                    "    for obj in items:\n"
                    "        obj.handle(1)\n"
                )
            }
            session = DeltaSession.from_workspace(before, runtime_edges=c8.observed_edges)
            res = session.apply_write(
                "dyn_pkg.py",
                "class Alpha:\n"
                "    def handle(self, x, *, policy):\n"
                "        return x + policy\n"
                "def dispatch_loop(items):\n"
                "    for obj in items:\n"
                "        obj.handle(1)\n",
            )
            self.assertEqual(len(res.findings), 1)
            self.assertEqual(res.findings[0].resolution_source, "observed_runtime")
            self.assertIn("[rt-obs]", res.advisory_text)
            self.assertLessEqual(res.advisory_utf8_bytes, 250)

    def test_ranking_prioritizes_production_over_tests_and_caps_bytes(self):
        before = {
            "pkg/core.py": "def run(x):\n    return x\n",
            "tests/test_core.py": "from pkg.core import run\nrun(1)\n",
            "pkg/service.py": "from pkg.core import run\nrun(2)\n",
        }
        after = {
            "pkg/core.py": "def run(x, *, token):\n    return x + token\n",
            "tests/test_core.py": before["tests/test_core.py"],
            "pkg/service.py": before["pkg/service.py"],
        }
        findings = analyze_delta(before, after)
        ranked = rank_findings(findings)
        self.assertEqual(ranked[0].path, "pkg/service.py")
        self.assertEqual(ranked[1].path, "tests/test_core.py")
        adv = format_advisory(ranked, max_bytes=120)
        self.assertLessEqual(len(adv.encode("utf-8")), 120)

    def test_intra_file_unchanged_function_reuse_equals_fresh_parse_under_line_shifts(self):
        before_src = (
            "class BigModel:\n"
            "    def first(self, x):\n"
            "        return x + 1\n"
            "\n"
            "    @property\n"
            "    def reporter(self) -> Reporter:\n"
            "        return self._rep\n"
            "\n"
            "    def unchanged_heavy(self, flag=None):\n"
            "        self.items: Bucket = make_bucket()\n"
            "        if flag:\n"
            "            return self.first(2)\n"
            "        return self._items\n"
            "\n"
            "    def outer_with_nested(self, a):\n"
            "        def inner(b):\n"
            "            return a + b\n"
            "        return inner(3)\n"
        )
        # Insert 4 lines inside `first`, shifting `reporter`, `unchanged_heavy`, and `outer_with_nested` down by +4 lines
        after_src = (
            "class BigModel:\n"
            "    def first(self, x):\n"
            "        step1 = x + 1\n"
            "        step2 = step1 * 2\n"
            "        step3 = step2 - 1\n"
            "        step4 = step3 + 0\n"
            "        return step4\n"
            "\n"
            "    @property\n"
            "    def reporter(self) -> Reporter:\n"
            "        return self._rep\n"
            "\n"
            "    def unchanged_heavy(self, flag=None):\n"
            "        self.items: Bucket = make_bucket()\n"
            "        if flag:\n"
            "            return self.first(2)\n"
            "        return self._items\n"
            "\n"
            "    def outer_with_nested(self, a):\n"
            "        def inner(b):\n"
            "            return a + b\n"
            "        return inner(3)\n"
        )
        before_facts = parse_file("pkg/big.py", before_src)
        fresh_after = parse_file("pkg/big.py", after_src)
        reused_after = parse_file("pkg/big.py", after_src, prior_facts=before_facts)

        self.assertEqual(reused_after.calls, fresh_after.calls)
        self.assertEqual(reused_after.imports, fresh_after.imports)
        self.assertEqual(
            {k: c.attr_annotations for k, c in reused_after.classes.items()},
            {k: c.attr_annotations for k, c in fresh_after.classes.items()},
        )
        self.assertEqual(set(reused_after.functions.keys()), set(fresh_after.functions.keys()))
        for qual in fresh_after.functions:
            f_reused = reused_after.functions[qual]
            f_fresh = fresh_after.functions[qual]
            self.assertEqual(f_reused, f_fresh)
            self.assertEqual(f_reused.body_dump, f_fresh.body_dump)

    @unittest.skipUnless(sys.version_info >= (3, 12), "sys.monitoring requires Python 3.12 or later")
    def test_collector_handles_mocks_and_proxies_safely_and_roundtrips_caller_line(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mod_path = tmp / "mock_target.py"
            mod_path.write_text(
                "class Worker:\n"
                "    def ping(self, x):\n"
                "        return x + 1\n\n"
                "class UnhashableProxy:\n"
                "    __hash__ = None\n"
                "    @property\n"
                "    def __name__(self):\n"
                "        return [1, 2]\n"
                "    def __call__(self, v):\n"
                "        return v\n\n"
                "def exercise(mock_obj, w):\n"
                "    c = mock_obj.bar(1)\n"
                "    _ = call_helper(mock_obj)\n"
                "    p = UnhashableProxy()\n"
                "    p(42)\n"
                "    return (c, w.ping(3))\n\n"
                "def call_helper(m):\n"
                "    return m\n",
                encoding="utf-8",
            )
            sys.path.insert(0, str(tmp))
            try:
                import importlib
                mt = importlib.import_module("mock_target")
                col = ScopedCallCollector([tmp], max_events_per_site=1)
                self.assertEqual(col.max_events_per_site, 1)
                col.start()
                try:
                    m = MagicMock()
                    _ = call.bar(1)
                    mt.exercise(m, mt.Worker())
                finally:
                    col.stop()

                shard_dir = tmp / "shards"
                shard_dir.mkdir()
                (shard_dir / "edges-1.json").write_text(
                    json.dumps(col.to_shard_dict()), encoding="utf-8"
                )
                loaded = load_runtime_edges(shard_dir)
                ping_edges = [e for e in loaded if e.callee_name == "ping"]
                self.assertTrue(ping_edges)
                self.assertIsInstance(ping_edges[0].caller_line, int)
                self.assertGreater(ping_edges[0].caller_line, 0)
                # Ensure MagicMock did not emit bogus Mock edge entries
                self.assertFalse(any("MagicMock" in (e.callee_qualname or "") for e in loaded))
            finally:
                sys.path.pop(0)
                sys.modules.pop("mock_target", None)

    def test_runtime_resolution_handles_mro_super_nested_locals_and_call_line_disambiguation(self):
        before = {
            "base.py": (
                "class BaseA:\n"
                "    def update(self, item, *, strict=False):\n"
                "        return (item, strict)\n\n"
                "class BaseB:\n"
                "    def update(self, label):\n"
                "        return label\n\n"
                "class ChildA(BaseA):\n"
                "    def extra(self):\n"
                "        return 1\n"
            ),
            "caller.py": (
                "from base import ChildA\n\n"
                "class Overrider(ChildA):\n"
                "    def update(self, item, *, strict=False):\n"
                "        return super().update(item)\n\n"
                "def dual_update(first, second, mode):\n"
                "    if mode:\n"
                "        return first.update('a')\n"
                "    return second.update('b')\n\n"
                "def outer(obj):\n"
                "    def helper():\n"
                "        def inner():\n"
                "            return obj.update('z')\n"
                "        return inner()\n"
                "    return helper()\n"
            ),
        }
        after = {
            **before,
            "base.py": before["base.py"].replace(
                "def update(self, item, *, strict=False):",
                "def update(self, item, *, mode=False):",
            ),
        }
        edges = [
            # Line 9 calls BaseA.update via subclass ChildA (MRO walk)
            ObservedCallEdge("caller.py", "dual_update", 10, "BaseA.update", "update", "ChildA", 1, caller_line=9),
            # Line 10 calls BaseB.update in same function (should NOT collide with line 9!)
            ObservedCallEdge("caller.py", "dual_update", 20, "BaseB.update", "update", "BaseB", 1, caller_line=10),
            # Unindexed <locals> subclass calling BaseA.update via super().update
            ObservedCallEdge(
                "caller.py", "Overrider.update", 12, "BaseA.update", "update", "test_case.<locals>.DynamicSub", 1, caller_line=5
            ),
            # Multi-level <locals> caller normalization
            ObservedCallEdge(
                "caller.py", "outer.<locals>.helper.<locals>.inner", 8, "BaseA.update", "update", "BaseA", 1, caller_line=15
            ),
        ]
        # Verify super().update resolves cleanly to BaseA.update, never self-recursing on Overrider.update
        facts_map = {p: parse_file(p, s) for p, s in after.items()}
        idx = build_index(facts_map, runtime_edges=edges)
        super_call = [
            c for c in facts_map["caller.py"].calls
            if c.owner_function == "Overrider.update" and c.callee_name == "update"
        ][0]
        self.assertEqual(
            [(fn.qualname, src) for fn, src in resolve_call_with_provenance(super_call, facts_map["caller.py"], idx)],
            [("BaseA.update", "observed_runtime")],
        )

        # Removing `strict=` when caller passed no `strict` won't fail unless `strict` was passed; mutate to required `need`!
        after_req = {
            **before,
            "base.py": before["base.py"].replace(
                "def update(self, item, *, strict=False):",
                "def update(self, item, *, need: str, strict=False):",
            ),
        }
        findings = analyze_delta(before, after_req, runtime_edges=edges)
        req_sites = {(f.path, f.start_line, f.resolution_source) for f in findings if f.detector == "required_keyword_missing"}
        self.assertIn(("caller.py", 5, "observed_runtime"), req_sites)
        self.assertIn(("caller.py", 9, "observed_runtime"), req_sites)
        self.assertNotIn(("caller.py", 10, "observed_runtime"), req_sites)
        self.assertIn(("caller.py", 15, "observed_runtime"), req_sites)


if __name__ == "__main__":
    unittest.main()
