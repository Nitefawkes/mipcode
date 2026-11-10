import unittest

from mipdelta.delta import analyze_delta


class DeltaContractTests(unittest.TestCase):
    def test_v2_optional_keyword_abstains_on_ordinary_consumers_and_migrated_calls_but_warns_wrappers(self):
        before = {
            "lib.py": "def dump(value):\n    return value\n",
            "consumer.py": (
                "from lib import dump\n"
                "def unrelated():\n"
                "    dump('old')\n"
                "    dump('new', policy='utf8')\n"
                "def dump(value):\n"
                "    return _forward(value)\n"
            ),
            "wrapper.py": (
                "from lib import dump as raw_dump\n"
                "def dump(value):\n"
                "    return raw_dump(value)\n"
                "def configured(value, *, policy='legacy'):\n"
                "    return raw_dump(value)\n"
                "def configured_ok(value, *, policy='legacy'):\n"
                "    return raw_dump(value, policy=policy)\n"
            ),
        }
        after = {
            "lib.py": "def dump(value, *, policy='legacy'):\n    return str(value) + policy\n",
            "consumer.py": before["consumer.py"],
            "wrapper.py": before["wrapper.py"],
        }

        v2_findings = analyze_delta(before, after)
        self.assertEqual([r for r in v2_findings if r.path == "consumer.py"], [])
        wrapper_hits = [r for r in v2_findings if r.detector == "delegating_wrapper_review"]
        self.assertEqual(len(wrapper_hits), 2)
        self.assertEqual([r.symbol for r in wrapper_hits], ["dump", "configured"])

        # Legacy mode remains available for exact v1 baseline comparison
        legacy_findings = analyze_delta(before, after, legacy_optional_keyword_all_callers=True)
        opt = [r for r in legacy_findings if r.detector == "optional_keyword_contract" and r.path == "consumer.py"]
        self.assertEqual(len(opt), 2)

    def test_existing_none_truthiness_is_not_attributed_to_this_delta(self):
        source = (
            "def existing(value=None):\n"
            "    if value:\n"
            "        return value\n"
            "    return ''\n"
        )
        self.assertEqual(analyze_delta({"existing.py": source}, {"existing.py": source}), [])

    def test_callback_lambda_that_supplies_new_keyword_is_not_direct_binding_warning(self):
        before = {
            "formatting.py": "def dump_header(value):\n    return str(value)\n",
            "response.py": "from formatting import dump_header\nheader_property(dump_func=dump_header)\n",
        }
        after = {
            "formatting.py": "def dump_header(value, *, strict=False):\n    return str(value)\n",
            "response.py": (
                "from formatting import dump_header\n"
                "header_property(dump_func=lambda value: dump_header(value, strict=True))\n"
            ),
        }

        found = analyze_delta(before, after)
        direct_bindings = [row for row in found if row.detector == "callback_binding_review"]
        self.assertEqual(direct_bindings, [])

    def test_eager_nested_call_is_not_treated_as_callback_value_binding(self):
        before = {
            "formatting.py": "def dump_header(value):\n    return str(value)\n",
            "caller.py": "from formatting import dump_header\nheaders.set('Vary', dump_header(items))\n",
        }
        after = {
            "formatting.py": "def dump_header(value, *, strict=False):\n    return str(value)\n",
            "caller.py": "from formatting import dump_header\nheaders.set('Vary', dump_header(items, strict=True))\n",
        }

        found = analyze_delta(before, after)
        self.assertEqual([r for r in found if r.detector == "callback_binding_review"], [])

    def test_paired_field_symmetry_flags_different_related_field_but_not_same_field_flow(self):
        before = {
            "reporter.py": (
                "class Reporter:\n"
                "    def attach(self, observer):\n"
                "        self.observers.append(observer)\n"
                "    def notify(self):\n"
                "        for observer in self.observers:\n"
                "            observer()\n"
            )
        }
        after = {
            "reporter.py": (
                "class Reporter:\n"
                "    def attach(self, observer, minimum_level=None):\n"
                "        self.observers.append(observer)\n"
                "        self._observer_minimum_levels.append(minimum_level)\n"
                "    def notify(self):\n"
                "        for observer in self.observers:\n"
                "            observer()\n"
            )
        }
        found = analyze_delta(before, after)
        state_findings = [row for row in found if row.detector == "state_field_review"]

        self.assertEqual(len(state_findings), 1)
        self.assertEqual(state_findings[0].symbol, "Reporter.notify")

        same_field_before = {"directory.py": "class Directory:\n    def ensure(self):\n        return self.name\n"}
        same_field_after = {
            "directory.py": "class Directory:\n    def ensure(self):\n        self.name = 'ready'\n        return self.name\n"
        }
        same_field_findings = [
            row
            for row in analyze_delta(same_field_before, same_field_after)
            if row.detector == "state_field_review"
        ]
        self.assertEqual(same_field_findings, [])

    def test_return_contract_review_catches_tuple_arity_and_newly_nullable_return_but_abstains_when_guarded(self):
        before = {
            "parser.py": (
                "def split_header(raw):\n"
                "    return raw.strip(), True\n"
                "def find_token(raw):\n"
                "    return Token(raw)\n"
            ),
            "caller.py": (
                "from parser import split_header, find_token\n"
                "def unpack_caller(s):\n"
                "    val, ok = split_header(s)\n"
                "    return val\n"
                "def unguarded_deref(s):\n"
                "    tok = find_token(s)\n"
                "    return tok.kind\n"
                "def guarded_deref(s):\n"
                "    tok = find_token(s)\n"
                "    if tok is not None:\n"
                "        return tok.kind\n"
                "    return ''\n"
            ),
        }
        after = {
            "parser.py": (
                "def split_header(raw):\n"
                "    return raw.strip(), True, 'utf-8'\n"
                "def find_token(raw):\n"
                "    if not raw:\n"
                "        return None\n"
                "    return Token(raw)\n"
            ),
            "caller.py": before["caller.py"],
        }
        found = [r for r in analyze_delta(before, after) if r.detector == "return_contract_review"]
        self.assertEqual(len(found), 2)
        self.assertEqual([(r.path, r.start_line, r.symbol) for r in found], [
            ("caller.py", 3, "split_header"),
            ("caller.py", 6, "find_token"),
        ])

    def test_exception_contract_review_catches_uncaught_new_raise_in_narrow_try_but_abstains_on_subclass_or_broad(self):
        before = {
            "errors.py": (
                "class ParseError(Exception):\n    pass\n"
                "class StrictHeaderError(ParseError):\n    pass\n"
                "class QuotaExceededError(Exception):\n    pass\n"
            ),
            "service.py": (
                "from errors import ParseError\n"
                "def decode(payload):\n"
                "    if not payload:\n"
                "        raise ParseError('empty')\n"
                "    return payload\n"
            ),
            "client.py": (
                "from errors import ParseError\n"
                "from service import decode\n"
                "def narrow_caller(x):\n"
                "    try:\n"
                "        return decode(x)\n"
                "    except ParseError:\n"
                "        return None\n"
                "def broad_caller(x):\n"
                "    try:\n"
                "        return decode(x)\n"
                "    except Exception:\n"
                "        return None\n"
                "def plain_caller(x):\n"
                "    return decode(x)\n"
            ),
        }
        # Raising StrictHeaderError (subclass of ParseError) should abstain; raising QuotaExceededError should warn narrow_caller
        after = {
            "errors.py": before["errors.py"],
            "service.py": (
                "from errors import ParseError, StrictHeaderError, QuotaExceededError\n"
                "def decode(payload):\n"
                "    if not payload:\n"
                "        raise StrictHeaderError('empty')\n"
                "    if len(payload) > 100:\n"
                "        raise QuotaExceededError('too big')\n"
                "    return payload\n"
            ),
            "client.py": before["client.py"],
        }
        found = [r for r in analyze_delta(before, after) if r.detector == "exception_contract_review"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path, "client.py")
        self.assertEqual(found[0].start_line, 5)
        self.assertIn("QuotaExceededError", found[0].evidence[0])
        self.assertNotIn("StrictHeaderError", found[0].evidence[0])

    def test_delegating_wrapper_abstains_when_conditional_branch_forwards_optional_param(self):
        from mipdelta.model import ObservedCallEdge

        before = {
            "nt.py": (
                "class NTSerializer:\n"
                "    def serialize(self, triples):\n"
                "        return list(triples)\n"
            ),
            "exporter.py": (
                "class GraphExporter:\n"
                "    def serialize(self, plugin, triples):\n"
                "        return plugin.serialize(triples)\n"
            ),
        }
        rt_edges = [
            ObservedCallEdge(
                caller_path="exporter.py",
                caller_qualname="GraphExporter.serialize",
                instruction_offset=10,
                callee_qualname="NTSerializer.serialize",
                callee_name="serialize",
                receiver_type="nt.NTSerializer",
                pid=1,
            )
        ]
        after_unfixed = {
            "nt.py": (
                "class NTSerializer:\n"
                "    def serialize(self, triples, *, base_iri=None):\n"
                "        return (base_iri, list(triples))\n"
            ),
            "exporter.py": before["exporter.py"],
        }
        unfixed_findings = [
            r for r in analyze_delta(before, after_unfixed, runtime_edges=rt_edges) if r.detector == "delegating_wrapper_review"
        ]
        self.assertEqual(len(unfixed_findings), 1)

        after_fixed_conditional = {
            "nt.py": after_unfixed["nt.py"],
            "exporter.py": (
                "class GraphExporter:\n"
                "    def serialize(self, plugin, triples, *, base_iri=None):\n"
                "        if base_iri is not None:\n"
                "            return plugin.serialize(triples, base_iri=base_iri)\n"
                "        return plugin.serialize(triples)\n"
            ),
        }
        fixed_findings = [
            r for r in analyze_delta(before, after_fixed_conditional, runtime_edges=rt_edges) if r.detector == "delegating_wrapper_review"
        ]
        self.assertEqual(len(fixed_findings), 0)


if __name__ == "__main__":
    unittest.main()
