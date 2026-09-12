"""load_frame missing stamp raises clear FileNotFoundError."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from cu_perceive.act import load_frame


class TestLoadFrameMissing(unittest.TestCase):
    def test_missing_stamp_raises(self):
        missing = Path(r"C:\Users\jawn\agent-bus\archive\shots\perceive") / "__no_such_stamp__.json"
        with self.assertRaises(FileNotFoundError) as cm:
            load_frame(stamp="__no_such_stamp__")
        self.assertIn("stamp json not found", str(cm.exception))
        self.assertIn(str(missing), str(cm.exception))

    def test_missing_json_path_raises(self):
        with self.assertRaises(FileNotFoundError) as cm:
            load_frame(json_path=r"C:\Users\jawn\src\cu-perceive\__missing_frame__.json")
        self.assertIn("stamp json not found", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
