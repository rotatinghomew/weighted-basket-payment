"""秤单台账测试：版本钉住、冲突更正与装袋候选。"""
import unittest
from pathlib import Path

from app.datafiles import read_ndjson
from app.scales import BagError, ScaleBook, ScaleConflict

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def sample_book():
    book = ScaleBook()
    for row in read_ndjson(SAMPLES / "weights.ndjson"):
        book.ingest(row)
    return book


class ScaleBookTest(unittest.TestCase):
    def test_bags_pin_their_own_revisions(self):
        """两位店员先后重称：每只袋取指向它的最高 revision。"""
        book = sample_book()
        bag31 = book.bag("bag-31")
        self.assertEqual([t.ref() for t in bag31.tickets],
                         ["f1/s1/w31@1", "f1/s1/w32@1"])
        self.assertEqual(bag31.content(), [("L-a", "pear", 1300), ("L-b", "greens", 1300)])
        bag32 = book.bag("bag-32")
        self.assertEqual([t.ref() for t in bag32.tickets],
                         ["f1/s1/w31@2", "f1/s2/w31@1"])
        self.assertEqual(bag32.content(), [("L-a", "apple", 1800), ("L-b", "greens", 1250)])

    def test_duplicate_ingest_is_idempotent(self):
        book = sample_book()
        _, outcome = book.ingest(read_ndjson(SAMPLES / "weights.ndjson")[0])
        self.assertEqual(outcome, "duplicate")
        self.assertEqual(book.bag("bag-31").content(),
                         [("L-a", "pear", 1300), ("L-b", "greens", 1300)])

    def test_same_revision_conflict_needs_clerk_correction(self):
        book = ScaleBook()
        book.ingest({"store": "f1", "scale": "s1", "ticket": "w9", "revision": 1,
                     "bag": "bag-9", "line": "L-a", "sku": "pear", "grams": 1300,
                     "by": "picker-1"})
        _, outcome = book.ingest({"store": "f1", "scale": "s1", "ticket": "w9",
                                  "revision": 1, "bag": "bag-9", "line": "L-a",
                                  "sku": "pear", "grams": 999, "by": "picker-2"})
        self.assertEqual(outcome, "conflict")
        with self.assertRaises(ScaleConflict):
            book.bag("bag-9")
        # 店员提交更高 revision 更正后可用
        book.ingest({"store": "f1", "scale": "s1", "ticket": "w9", "revision": 2,
                     "bag": "bag-9", "line": "L-a", "sku": "pear", "grams": 1301,
                     "by": "picker-2"})
        self.assertEqual(book.bag("bag-9").content(), [("L-a", "pear", 1301)])

    def test_unknown_bag_and_duplicate_line(self):
        book = sample_book()
        with self.assertRaises(BagError):
            book.bag("bag-none")
        book.ingest({"store": "f1", "scale": "s9", "ticket": "w99", "revision": 1,
                     "bag": "bag-x", "line": "L-a", "sku": "pear", "grams": 100,
                     "by": "picker-1"})
        book.ingest({"store": "f1", "scale": "s9", "ticket": "w98", "revision": 1,
                     "bag": "bag-x", "line": "L-a", "sku": "pear", "grams": 100,
                     "by": "picker-1"})
        with self.assertRaises(BagError):
            book.bag("bag-x")


if __name__ == "__main__":
    unittest.main()
