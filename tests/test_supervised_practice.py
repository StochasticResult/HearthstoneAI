import unittest
import subprocess
import sys

from navigation import DECK_ID, next_navigation, read_json_line


def state(mode, deck=None, **kw):
    return {"scene": {"mode": mode, "sceneLoaded": True, "transitioning": False},
            "selected_deck": deck, "popup": False, "shop_open": False, **kw}


class NavigationTests(unittest.TestCase):
    def test_startup_reader_handles_multiple_lines_in_one_chunk(self):
        p = subprocess.Popen([sys.executable, "-c", "print('{\\\"a\\\":1}'); print('{\\\"b\\\":2}')"], stdout=subprocess.PIPE)
        try:
            self.assertEqual(read_json_line(p, 3), {"a": 1})
            self.assertEqual(read_json_line(p, 3), {"b": 2})
        finally:
            p.wait(timeout=3)
            p.stdout.close()

    def test_only_expected_scenes_and_deck_advance(self):
        self.assertEqual(next_navigation(0, state(3)), (735, 484))
        self.assertEqual(next_navigation(1, state(16)), (1040, 628))
        self.assertEqual(next_navigation(2, state(13)), (1060, 735))
        self.assertEqual(next_navigation(3, state(13, DECK_ID)), (1068, 730))
        self.assertEqual(next_navigation(4, state(13, DECK_ID)), (1058, 400))
        self.assertEqual(next_navigation(5, state(13, DECK_ID)), (1080, 716))
        self.assertIsNone(next_navigation(3, state(13, DECK_ID + 1)))
        blocked = state(13, DECK_ID)
        blocked["shop_open"] = True
        self.assertIsNone(next_navigation(3, blocked))


if __name__ == "__main__":
    unittest.main(verbosity=2)
