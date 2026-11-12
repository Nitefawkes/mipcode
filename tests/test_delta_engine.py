import unittest

from mipdelta.cache import FactCache
from mipdelta.delta import analyze_delta


class DeltaEngineTests(unittest.TestCase):
    def test_added_required_keyword_finds_unmigrated_imported_call(self):
        before = {
            "lib.py": "def encode(value):\n    return value\n",
            "caller.py": "from lib import encode\n\nencode('text')\n",
        }
        after = {
            "lib.py": "def encode(value, *, policy):\n    return value\n",
            "caller.py": before["caller.py"],
        }

        found = analyze_delta(before, after)
        matches = [row for row in found if row.detector == "required_keyword_missing"]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].path, "caller.py")
        self.assertEqual(matches[0].start_line, 3)
        self.assertIn("encode", matches[0].symbol)

    def test_callback_value_binding_is_reported_for_changed_function(self):
        before = {
            "formatting.py": "def dump_header(value):\n    return str(value)\n",
            "response.py": (
                "from formatting import dump_header\n"
                "def header_property():\n"
                "    return property(fget=None, fset=None, dump_func=dump_header)\n"
            ),
        }
        after = {
            "formatting.py": (
                "def dump_header(value, *, strict=False):\n"
                "    return str(value)\n"
            ),
            "response.py": before["response.py"],
        }

        found = analyze_delta(before, after)
        matches = [row for row in found if row.detector == "callback_binding_review"]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].path, "response.py")
        self.assertEqual(matches[0].start_line, 3)

    def test_base_signature_change_reports_stale_override(self):
        source_before = (
            "class Base:\n"
            "    def encode(self, value):\n"
            "        return value\n"
            "\n"
            "class Child(Base):\n"
            "    def encode(self, value):\n"
            "        return value\n"
        )
        source_after = (
            "class Base:\n"
            "    def encode(self, value, *, strict):\n"
            "        return value\n"
            "\n"
            "class Child(Base):\n"
            "    def encode(self, value):\n"
            "        return value\n"
        )

        found = analyze_delta({"api.py": source_before}, {"api.py": source_after})
        matches = [row for row in found if row.detector == "override_signature_review"]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].path, "api.py")
        self.assertEqual(matches[0].start_line, 6)
        self.assertIn("Child.encode", matches[0].symbol)

    def test_added_optional_base_parameter_reports_override_without_contract_slot(self):
        before = {
            "api.py": (
                "class Base:\n"
                "    def encode(self, value):\n"
                "        return value\n"
                "class Child(Base):\n"
                "    def encode(self, value):\n"
                "        return value\n"
            )
        }
        after = {
            "api.py": (
                "class Base:\n"
                "    def encode(self, value, *, strict=False):\n"
                "        return value\n"
                "class Child(Base):\n"
                "    def encode(self, value):\n"
                "        return value\n"
            )
        }

        findings = analyze_delta(before, after)
        overrides = [row for row in findings if row.detector == "override_signature_review"]

        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0].symbol, "Child.encode")
        self.assertIn("strict", overrides[0].evidence[0])

    def test_changed_base_default_reports_drift_but_kwargs_override_is_compatible(self):
        before = {
            "api.py": (
                "class Base:\n"
                "    def encode(self, value, *, strict=False):\n"
                "        return value\n"
                "class Child(Base):\n"
                "    def encode(self, value, *, strict=False):\n"
                "        return value\n"
                "class Flexible(Base):\n"
                "    def encode(self, value, **kwargs):\n"
                "        return value\n"
            )
        }
        after = {
            "api.py": (
                "class Base:\n"
                "    def encode(self, value, *, strict=True):\n"
                "        return value\n"
                "class Child(Base):\n"
                "    def encode(self, value, *, strict=False):\n"
                "        return value\n"
                "class Flexible(Base):\n"
                "    def encode(self, value, **kwargs):\n"
                "        return value\n"
            )
        }

        findings = analyze_delta(before, after)
        overrides = [row for row in findings if row.detector == "override_signature_review"]

        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0].symbol, "Child.encode")
        self.assertIn("default", " ".join(overrides[0].evidence))

    def test_added_unused_parameter_emits_candidate_sink_review(self):
        before = {"api.py": "def serialize(value):\n    return str(value)\n"}
        after = {
            "api.py": (
                "def serialize(value, *, base_iri=None):\n"
                "    return str(value)\n"
            )
        }

        findings = analyze_delta(before, after)
        sinks = [row for row in findings if row.detector == "parameter_sink_review"]

        self.assertEqual(len(sinks), 1)
        self.assertEqual(sinks[0].symbol, "serialize")
        self.assertIn("base_iri", sinks[0].evidence[0])

    def test_new_parallel_state_field_surfaces_review_candidate(self):
        source_before = (
            "class Reporter:\n"
            "    def __init__(self):\n"
            "        self.observers = []\n"
            "\n"
            "    def attach(self, observer):\n"
            "        self.observers.append(observer)\n"
            "\n"
            "    def detach(self, observer):\n"
            "        index = self.observers.index(observer)\n"
            "        del self.observers[index]\n"
            "\n"
            "    def notify(self, message):\n"
            "        for observer in self.observers:\n"
            "            observer(message)\n"
        )
        source_after = (
            "class Reporter:\n"
            "    def __init__(self):\n"
            "        self.observers = []\n"
            "        self.observer_thresholds = []\n"
            "\n"
            "    def attach(self, observer, minimum_level=None):\n"
            "        self.observers.append(observer)\n"
            "        self.observer_thresholds.append(minimum_level)\n"
            "\n"
            "    def detach(self, observer):\n"
            "        index = self.observers.index(observer)\n"
            "        del self.observers[index]\n"
            "        del self.observer_thresholds[index]\n"
            "\n"
            "    def notify(self, message):\n"
            "        for observer in self.observers:\n"
            "            observer(message)\n"
        )

        found = analyze_delta({"reporter.py": source_before}, {"reporter.py": source_after})
        matches = [row for row in found if row.detector == "state_field_review"]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].path, "reporter.py")
        self.assertEqual(matches[0].symbol, "Reporter.notify")
        self.assertIn("observer_thresholds", matches[0].evidence[0])

    def test_enabled_truthiness_review_is_candidate_not_fact(self):
        before = {"serialize.py": "def serialize(data):\n    return str(data)\n"}
        after = {
            "serialize.py": (
                "def serialize(data, base=None):\n"
                "    if base:\n"
                "        return base + str(data)\n"
                "    return str(data)\n"
            )
        }

        found = analyze_delta(before, after, enable=["parameter_truthiness_review"])
        matches = [row for row in found if row.detector == "parameter_truthiness_review"]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].path, "serialize.py")
        self.assertEqual(matches[0].start_line, 2)
        self.assertEqual(matches[0].confidence, "candidate")

    def test_same_name_unrelated_methods_do_not_cross_bind(self):
        before = {
            "base.py": "class Base:\n    def encode(self, value):\n        return value\n",
            "other.py": (
                "class Other:\n"
                "    def encode(self, value):\n"
                "        return value\n"
                "def run():\n"
                "    return Other().encode('x')\n"
            ),
        }
        after = {
            "base.py": "class Base:\n    def encode(self, value, *, strict):\n        return value\n",
            "other.py": before["other.py"],
        }

        found = analyze_delta(before, after)
        cross_file_false_claims = [
            row for row in found if row.detector == "required_keyword_missing" and row.path == "other.py"
        ]

        self.assertEqual(cross_file_false_claims, [])

    def test_replay_reuses_exact_content_facts_without_stale_path_cache(self):
        before = {
            "lib.py": "def encode(value):\n    return value\n",
            "caller.py": "from lib import encode\nencode('x')\n",
        }
        after = {
            "lib.py": "def encode(value, *, strict=False):\n    return value\n",
            "caller.py": before["caller.py"],
        }
        cache = FactCache()

        first = analyze_delta(before, after, cache=cache)
        parses_after_first = cache.stats.parses
        second = analyze_delta(before, after, cache=cache)

        self.assertEqual(first, second)
        self.assertGreater(cache.stats.hits, 0)
        self.assertEqual(cache.stats.parses, parses_after_first)
        self.assertGreater(cache.stats.invalidations, 0)


if __name__ == "__main__":
    unittest.main()
