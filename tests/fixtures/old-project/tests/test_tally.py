import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import tally  # noqa: E402


class Total(unittest.TestCase):
    def test_adds_the_numbers(self):  # covers: TALLY-1
        self.assertEqual(tally.total([2, 3, 4]), 9)

    def test_no_numbers_make_zero(self):  # covers: TALLY-1
        self.assertEqual(tally.total([]), 0)


if __name__ == "__main__":
    unittest.main()
