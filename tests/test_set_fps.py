"""set_fps changes an item everywhere it is used, so every reference must name exactly one item."""

import unittest

from helpers import BridgeError, fake_rpc

from ppro_bridge import ops


class SetFpsTest(unittest.TestCase):
    def setUp(self):
        self.rpc, self.fake, self.calls = fake_rpc()

    def run_fps(self, items, dry_run=False):
        return ops.set_fps(self.rpc, item=items, fps=59.94, dry_run=dry_run)

    def test_same_name_in_two_bins_is_refused(self):
        with self.assertRaises(BridgeError) as cm:
            self.run_fps(["title.mov"])
        self.assertEqual(cm.exception.code, "AMBIGUOUS")
        self.assertNotIn("set_fps", [a for a, _ in self.calls])

    def test_bin_path_names_one(self):
        result = self.run_fps(["CN/title.mov"])
        self.assertEqual(result["items"], ["CN/title.mov"])
        sent = [p for a, p in self.calls if a == "set_fps"][0]
        self.assertEqual(sent["items"], [{"name": "title.mov", "binPath": "CN"}])

    def test_dry_run_lists_uses_and_changes_nothing(self):
        result = self.run_fps(["interview.mp4"], dry_run=True)
        self.assertEqual({u["track"] for u in result["usesInActiveSequence"]}, {"V1", "A1"})
        self.assertNotIn("set_fps", [a for a, _ in self.calls])

    def test_a_single_name_is_accepted_too(self):
        result = self.run_fps("CN/title.mov")
        self.assertEqual(result["items"], ["CN/title.mov"])


if __name__ == "__main__":
    unittest.main()
