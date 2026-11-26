"""资金台账测试：去重、乱序回执、断开重查与冲突登记。"""
import unittest
from pathlib import Path

from app.datafiles import read_ndjson
from app.funds import FundsLedger, LoopbackGateway, ReplayGateway

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


class LedgerTest(unittest.TestCase):
    def test_submit_dedup_by_ref(self):
        ledger = FundsLedger(LoopbackGateway())
        first = ledger.submit(channel="wallet-a", ref="r1", action="hold", amount=100)
        again = ledger.submit(channel="wallet-a", ref="r1", action="hold", amount=100)
        self.assertIs(first, again)
        with self.assertRaises(ValueError):
            ledger.submit(channel="wallet-a", ref="r1", action="capture", amount=50)

    def test_out_of_order_receipts_keep_terminal(self):
        """p02：v1 unknown → v3 succeeded → v2 accepted 迟到，结论不被推翻。"""
        receipts = read_ndjson(SAMPLES / "funds.ndjson")
        gateway = ReplayGateway(receipts)
        ledger = FundsLedger(gateway)
        ledger.submit(channel="wallet-a", ref="p02", action="capture",
                      amount=3011, parent="p01")
        self.assertEqual(ledger.get("wallet-a", "p02").status, "unknown")
        gateway.push_next("p02", lambda r: ledger.apply_receipt(r))  # v3 succeeded
        act = ledger.get("wallet-a", "p02")
        self.assertEqual(act.status, "succeeded")
        self.assertEqual(act.version, 3)
        _, outcome = gateway.push_next("p02", lambda r: ledger.apply_receipt(r))  # v2 accepted
        self.assertEqual(outcome, "stale")
        self.assertEqual(act.status, "succeeded")
        self.assertEqual(len(act.stale), 1)

    def test_duplicate_receipt_is_noop(self):
        ledger = FundsLedger(LoopbackGateway())
        ledger.submit(channel="wallet-a", ref="r1", action="capture", amount=10, parent="h1")
        receipt = {"channel": "wallet-a", "ref": "r1", "version": 1,
                   "action": "capture", "status": "succeeded", "amount": 10, "parent": "h1"}
        _, outcome = ledger.apply_receipt(receipt)
        self.assertEqual(outcome, "duplicate")
        self.assertEqual(ledger.get("wallet-a", "r1").status, "succeeded")

    def test_disconnect_then_query_by_ref(self):
        """渠道在已扣成功后断开：本地未知，按 ref 查询重取结论。"""
        gateway = LoopbackGateway()
        gateway.drop_next()
        ledger = FundsLedger(gateway)
        act = ledger.submit(channel="wallet-a", ref="r9", action="capture",
                            amount=3011, parent="h1")
        self.assertEqual(act.status, "unknown")
        found = ledger.query("wallet-a", "r9")
        self.assertEqual(found.status, "succeeded")

    def test_conflicting_same_version_flagged(self):
        ledger = FundsLedger(LoopbackGateway())
        ledger.submit(channel="wallet-a", ref="r1", action="hold", amount=100)
        _, outcome = ledger.apply_receipt({"channel": "wallet-a", "ref": "r1",
                                           "version": 1, "action": "hold",
                                           "status": "rejected", "amount": 100})
        self.assertEqual(outcome, "conflict")
        self.assertTrue(ledger.get("wallet-a", "r1").conflict)
        self.assertEqual(ledger.get("wallet-a", "r1").status, "succeeded")

    def test_terminal_not_regressed_by_nonterminal(self):
        ledger = FundsLedger(LoopbackGateway())
        ledger.submit(channel="wallet-a", ref="r1", action="capture", amount=10, parent="h")
        _, outcome = ledger.apply_receipt({"channel": "wallet-a", "ref": "r1",
                                           "version": 2, "action": "capture",
                                           "status": "accepted", "amount": 10, "parent": "h"})
        self.assertEqual(outcome, "conflict")
        self.assertEqual(ledger.get("wallet-a", "r1").status, "succeeded")

    def test_replay_gateway_full_story(self):
        receipts = read_ndjson(SAMPLES / "funds.ndjson")
        gateway = ReplayGateway(receipts)
        ledger = FundsLedger(gateway)
        hold = ledger.submit(channel="wallet-a", ref="p01", action="hold", amount=3400)
        self.assertEqual(hold.status, "succeeded")
        cap = ledger.submit(channel="wallet-a", ref="p02", action="capture",
                            amount=3011, parent="p01")
        self.assertEqual(cap.status, "unknown")
        while gateway.pending("p02"):
            gateway.push_next("p02", lambda r: ledger.apply_receipt(r))
        self.assertEqual(cap.status, "succeeded")
        refund = ledger.submit(channel="wallet-a", ref="p03", action="refund",
                               amount=3011, parent="p02")
        self.assertEqual(refund.status, "accepted")  # 处理中，不能算已退
        gateway.push_next("p03", lambda r: ledger.apply_receipt(r))
        self.assertEqual(refund.status, "succeeded")


if __name__ == "__main__":
    unittest.main()
