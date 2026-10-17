import json
import re
import unittest
from pathlib import Path

from shop import flags

ROOT = Path(__file__).resolve().parents[1]


class FlagsAgree(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config" / "flags.json").read_text())
        rows = re.findall(
            r"^\| (\w+) \| (on|off) \| (.+?) \|$", (ROOT / "docs" / "FLAGS.md").read_text(), re.M
        )
        self.documented = {name: (state == "on", description) for name, state, description in rows}

    def test_module_matches_config(self):
        self.assertEqual(flags.ALL, {name: flag["default"] for name, flag in self.config.items()})

    def test_constants_match(self):
        for name, value in flags.ALL.items():
            with self.subTest(flag=name):
                self.assertEqual(getattr(flags, name.upper()), value)

    def test_docs_match_config(self):
        expected = {
            name: (flag["default"], flag["description"]) for name, flag in self.config.items()
        }
        self.assertEqual(self.documented, expected)

    def test_flags_are_sorted(self):
        self.assertEqual(list(self.config), sorted(self.config))
