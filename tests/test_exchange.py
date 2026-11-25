"""核对交换资料的客户编号、时序和关联条件。"""
import json
from pathlib import Path
import unittest


SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def rows(name):
    content = (SAMPLES / f"{name}.ndjson").read_text(encoding="utf-8")
    return [json.loads(line) for line in content.splitlines()]


class ExchangeSampleTest(unittest.TestCase):
    def test_records_are_json_objects(self):
        files = sorted(SAMPLES.glob("*.ndjson"))
        self.assertEqual(len(files), 5)
        for path in files:
            with self.subTest(path=path.name):
                records = rows(path.stem)
                self.assertTrue(records)
                self.assertTrue(all(isinstance(row, dict) for row in records))

    def test_related_records_preserve_source_evidence(self):
        weights = rows("weights")
        self.assertEqual(weights[0]["ticket"], weights[2]["ticket"])
        self.assertNotEqual(weights[0]["bag"], weights[2]["bag"])
        self.assertEqual(weights[2]["ticket"], weights[3]["ticket"])
        self.assertNotEqual(weights[2]["scale"], weights[3]["scale"])
        funds = rows("funds")
        self.assertEqual(funds[1]["ref"], funds[2]["ref"])
        self.assertGreater(funds[2]["version"], funds[3]["version"])
        self.assertEqual(funds[4]["parent"], funds[2]["ref"])
