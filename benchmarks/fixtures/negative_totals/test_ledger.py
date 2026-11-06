import unittest

from ledger import total


class TotalTests(unittest.TestCase):
    def test_positive_amounts(self) -> None:
        self.assertEqual(total([2, 3]), 5)

    def test_empty(self) -> None:
        self.assertEqual(total([]), 0)
