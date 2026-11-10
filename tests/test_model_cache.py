import unittest

from mipdelta.cache import FactCache
from mipdelta.model import SourceFile


class FactCacheTests(unittest.TestCase):
    def test_same_content_reuses_facts_for_normalized_path(self):
        cache = FactCache()
        first = cache.load("pkg\\mod.py", "def run():\n    return 1\n")
        second = cache.load("pkg/mod.py", "def run():\n    return 1\n")

        self.assertIs(first, second)
        self.assertEqual(cache.stats.parses, 1)
        self.assertEqual(cache.stats.hits, 1)

    def test_same_size_replacement_invalidates_by_digest(self):
        cache = FactCache()
        first = cache.load("pkg/mod.py", "def run():\n    return 1\n")
        second = cache.load("pkg/mod.py", "def run():\n    return 2\n")

        self.assertIsNot(first, second)
        self.assertIn("Constant(value=1)", first.functions["run"].body_dump)
        self.assertNotEqual(first.functions["run"].body_dump, second.functions["run"].body_dump)
        self.assertEqual(cache.stats.parses, 2)
        self.assertEqual(cache.stats.invalidations, 1)

        replacement = cache.load("pkg/mod.py", "def run():\n    return 1\n")
        self.assertIs(first, replacement)
        self.assertEqual(cache.stats.parses, 2)
        self.assertEqual(cache.stats.hits, 1)

    def test_source_file_binds_content_digest(self):
        first = SourceFile.from_text("pkg/mod.py", "x = 1\n")
        second = SourceFile.from_text("pkg/mod.py", "x = 2\n")

        self.assertEqual(first.path, "pkg/mod.py")
        self.assertNotEqual(first.sha256, second.sha256)
        self.assertEqual(first.byte_count, len(first.text.encode("utf-8")))

    def test_parse_failure_is_cached_as_explicit_uncertainty(self):
        cache = FactCache()
        first = cache.load("bad.py", "def broken(:\n pass\n")
        second = cache.load("bad.py", "def broken(:\n pass\n")

        self.assertIs(first, second)
        self.assertIn("invalid syntax", first.parse_error or "")
        self.assertEqual(cache.stats.parses, 1)


if __name__ == "__main__":
    unittest.main()
