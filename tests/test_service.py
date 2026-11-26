"""订单服务测试：称重回传、顾客确认、资金回执与出货放行的全流程。"""
import unittest
from pathlib import Path

from app.datafiles import load_catalog, load_offers, read_ndjson
from app.funds import FundsLedger, LoopbackGateway, ReplayGateway
from app.service import (CONFIRMED, CANCELLED, CANCEL_PENDING, PENDING_CUSTOMER,
                         OrderError, OrderService, RefGen)

SAMPLES = Path(__file__).resolve().parents[1] / "samples"

BASKET_WB1 = {"basket": "wb-1", "store": "f1", "customer": "u-w", "offer": "week47",
              "max_pay": 3400,
              "items": [{"line": "L-a", "sku": "pear", "min": 1100, "max": 1500,
                         "allow_group": True},
                        {"line": "L-b", "sku": "greens", "min": 1200, "max": 1450,
                         "allow_group": False}]}
BASKET_WB2 = {"basket": "wb-2", "store": "f1", "customer": "u-x", "offer": "week48",
              "max_pay": 5000,
              "items": [{"line": "L-c", "sku": "apple", "min": 1400, "max": 2000,
                         "allow_group": False}]}
BAG31 = [
    {"store": "f1", "scale": "s1", "ticket": "w31", "revision": 1, "bag": "bag-31",
     "line": "L-a", "sku": "pear", "grams": 1300, "by": "picker-1"},
    {"store": "f1", "scale": "s1", "ticket": "w32", "revision": 1, "bag": "bag-31",
     "line": "L-b", "sku": "greens", "grams": 1300, "by": "picker-1"},
]
BAG32 = [
    {"store": "f1", "scale": "s1", "ticket": "w31", "revision": 2, "bag": "bag-32",
     "line": "L-a", "sku": "apple", "grams": 1800, "by": "picker-2"},
    {"store": "f1", "scale": "s2", "ticket": "w31", "revision": 1, "bag": "bag-32",
     "line": "L-b", "sku": "greens", "grams": 1250, "by": "picker-2"},
]


def make_service(gateway=None, ref_gen=None):
    ledger = FundsLedger(gateway) if gateway is not None else None
    return OrderService(catalog=load_catalog(SAMPLES / "catalog.ndjson"),
                        offers=load_offers(SAMPLES / "offers.ndjson"),
                        ledger=ledger, ref_gen=ref_gen)


def weigh(service, rows):
    for row in rows:
        service.ingest_ticket(row)


def funds_of(service, order_id, action=None):
    funds = service.inspect(order_id)["funds"]
    return [f for f in funds if action is None or f["action"] == action]


class HappyPathTest(unittest.TestCase):
    def test_weigh_confirm_capture_release_deliver(self):
        service = make_service()
        service.checkout(BASKET_WB2)
        weigh(service, [{"store": "f1", "scale": "s3", "ticket": "w51", "revision": 1,
                         "bag": "bag-51", "line": "L-c", "sku": "apple",
                         "grams": 1800, "by": "picker-1"}])
        bill = service.build_bill("wb-2", "bag-51")
        self.assertEqual(bill.total, 3834)          # 3384-100，未达满减，运费 550
        self.assertEqual(bill.status, "confirmed")
        self.assertEqual(bill.consent, "checkout_consent")
        capture = funds_of(service, "wb-2", "capture")[0]
        self.assertEqual((capture["status"], capture["amount"]), ("succeeded", 3834))
        voucher = service.release("wb-2")
        self.assertEqual(voucher["bag"], "bag-51")
        self.assertEqual(voucher["bill_version"], 1)
        self.assertEqual(voucher["capture_ref"], capture["ref"])
        # 重复实扣回执不重复发货
        service.apply_receipt({"channel": "wallet-a", "ref": capture["ref"],
                               "version": 1, "action": "capture",
                               "status": "succeeded", "amount": 3834,
                               "parent": capture["parent"]})
        self.assertIs(service.release("wb-2"), voucher)
        service.deliver("wb-2")
        self.assertEqual(service.inspect("wb-2")["state"], "delivered")

    def test_build_bill_same_bag_is_idempotent(self):
        service = make_service()
        service.checkout(BASKET_WB1)
        weigh(service, BAG31)
        first = service.build_bill("wb-1", "bag-31")
        again = service.build_bill("wb-1", "bag-31")
        self.assertIs(first, again)
        self.assertEqual(len(service.orders["wb-1"].bills), 1)
        self.assertEqual(len(funds_of(service, "wb-1", "capture")), 1)

    def test_release_requires_funding(self):
        gateway = LoopbackGateway()
        service = make_service(gateway)
        service.checkout(BASKET_WB1)
        gateway.drop_next()  # 实扣发出后渠道断开
        weigh(service, BAG31)
        service.build_bill("wb-1", "bag-31")
        capture = funds_of(service, "wb-1", "capture")[0]
        self.assertEqual(capture["status"], "unknown")
        with self.assertRaises(OrderError):
            service.release("wb-1")  # 未知时不能把货当作已付款而出店
        service.query_funds(capture["ref"])  # 按 ref 重取已知结论
        self.assertEqual(service.release("wb-1")["bag"], "bag-31")

    def test_unknown_hold_blocks_second_hold(self):
        gateway = LoopbackGateway()
        gateway.drop_next()  # 冻结发出后渠道断开
        service = make_service(gateway)
        service.checkout(BASKET_WB1)
        weigh(service, BAG31)
        service.build_bill("wb-1", "bag-31")
        holds = funds_of(service, "wb-1", "hold")
        self.assertEqual(len(holds), 1)  # 冻结未知时不会再冻一笔
        self.assertEqual(holds[0]["status"], "unknown")
        self.assertEqual(funds_of(service, "wb-1", "capture"), [])
        service.query_funds(holds[0]["ref"])
        captures = funds_of(service, "wb-1", "capture")
        self.assertEqual([(c["amount"], c["status"]) for c in captures],
                         [(3011, "succeeded")])


class ReconfirmTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.service.checkout(BASKET_WB1)
        weigh(self.service, BAG31)
        self.service.build_bill("wb-1", "bag-31")

    def test_over_cap_substitution_needs_fresh_consent(self):
        weigh(self.service, BAG32)
        bill2 = self.service.build_bill("wb-1", "bag-32")
        self.assertEqual(bill2.total, 4097)
        self.assertEqual(bill2.status, "pending_customer")
        self.assertEqual(bill2.diff_vs_cap, 697)
        self.assertEqual(bill2.diff_vs_previous, 1086)
        self.assertTrue(any("超过已确认上限" in b for b in bill2.blocks))
        self.assertTrue(any("换为同组" in n for n in bill2.notes))
        # 结算所用秤单确实属于对应那一篮
        self.assertEqual(self.service.orders["wb-1"].bills[0].tickets,
                         ["f1/s1/w31@1", "f1/s1/w32@1"])
        self.assertEqual(bill2.tickets, ["f1/s1/w31@2", "f1/s2/w31@1"])
        # 对上一版账单的同意不能拿来批准新账单
        with self.assertRaises(OrderError):
            self.service.confirm("wb-1", 1)
        with self.assertRaises(OrderError):
            self.service.release("wb-1")  # 待确认不能放行
        self.service.confirm("wb-1", 2)
        view = self.service.inspect("wb-1")
        self.assertEqual(view["state"], CONFIRMED)
        # 旧版实扣已退，新版有自己的冻结与实扣
        refunds = funds_of(self.service, "wb-1", "refund")
        self.assertEqual([(r["amount"], r["status"]) for r in refunds],
                         [(3011, "succeeded")])
        captures = funds_of(self.service, "wb-1", "capture")
        self.assertEqual(sorted(c["amount"] for c in captures), [3011, 4097])
        voucher = self.service.release("wb-1")
        self.assertEqual((voucher["bag"], voucher["bill_version"]), ("bag-32", 2))
        self.assertEqual(view["bills"][0]["status"], "superseded")
        self.assertEqual(view["bills"][0]["replaced_by"], 2)

    def test_out_of_range_weight_and_sku_blocked(self):
        weigh(self.service, [
            {"store": "f1", "scale": "s1", "ticket": "w41", "revision": 1,
             "bag": "bag-41", "line": "L-a", "sku": "pear", "grams": 1600,
             "by": "picker-2"},
            {"store": "f1", "scale": "s1", "ticket": "w42", "revision": 1,
             "bag": "bag-41", "line": "L-b", "sku": "apple", "grams": 1300,
             "by": "picker-2"},
        ])
        bill = self.service.build_bill("wb-1", "bag-41")
        self.assertEqual(bill.status, "pending_customer")
        self.assertTrue(any("超出允许区间" in b for b in bill.blocks))
        self.assertTrue(any("超出允诺范围" in b for b in bill.blocks))

    def test_in_group_substitution_within_cap_auto_confirms(self):
        weigh(self.service, [
            {"store": "f1", "scale": "s1", "ticket": "w43", "revision": 1,
             "bag": "bag-43", "line": "L-a", "sku": "apple", "grams": 1200,
             "by": "picker-2"},
            {"store": "f1", "scale": "s1", "ticket": "w44", "revision": 1,
             "bag": "bag-43", "line": "L-b", "sku": "greens", "grams": 1300,
             "by": "picker-2"},
        ])
        bill = self.service.build_bill("wb-1", "bag-43")
        self.assertEqual(bill.status, "confirmed")
        self.assertTrue(any("换为同组" in n for n in bill.notes))
        self.assertEqual(self.service.orders["wb-1"].state, CONFIRMED)

    def test_missing_line_recomputes_below_threshold(self):
        weigh(self.service, [
            {"store": "f1", "scale": "s1", "ticket": "w45", "revision": 1,
             "bag": "bag-45", "line": "L-a", "sku": "pear", "grams": 1300,
             "by": "picker-1"},
        ])
        bill = self.service.build_bill("wb-1", "bag-45")
        self.assertEqual(bill.total, 2250)  # 无满减、运费恢复，仍在上限内
        self.assertEqual(bill.status, "confirmed")
        self.assertTrue(any("缺货未拣" in n for n in bill.notes))


class CancelTest(unittest.TestCase):
    def test_cancel_with_only_hold_voids_it(self):
        service = make_service()
        service.checkout(BASKET_WB1)
        service.cancel("wb-1")
        view = service.inspect("wb-1")
        self.assertEqual(view["state"], CANCELLED)
        self.assertEqual(view["money"]["voided"], 3400)
        self.assertEqual(funds_of(service, "wb-1", "capture"), [])

    def test_cancel_while_capture_unknown_then_reconcile(self):
        receipts = read_ndjson(SAMPLES / "funds.ndjson")
        gateway = ReplayGateway(receipts)
        service = make_service(gateway, RefGen(preset=["p01", "p02", "p03"]))
        service.checkout(read_ndjson(SAMPLES / "baskets.ndjson")[0])
        weigh(service, read_ndjson(SAMPLES / "weights.ndjson")[:2])
        bill = service.build_bill("wb-1", "bag-31")
        self.assertEqual(bill.total, 3011)
        self.assertEqual(funds_of(service, "wb-1", "capture")[0]["status"], "unknown")
        weigh(service, read_ndjson(SAMPLES / "weights.ndjson")[2:])
        bill2 = service.build_bill("wb-1", "bag-32")
        self.assertEqual(bill2.status, "pending_customer")
        # 取消期间旧实扣未知：不再扣一笔，也不放行
        service.cancel("wb-1")
        self.assertEqual(service.orders["wb-1"].state, CANCEL_PENDING)
        self.assertEqual(len(funds_of(service, "wb-1")), 2)  # 只有 p01、p02
        with self.assertRaises(OrderError):
            service.release("wb-1")
        # 乱序回执：v3 已收先成立，v2 accepted 迟到搁置
        gateway.push_next("p02", service.apply_receipt)
        gateway.push_next("p02", service.apply_receipt)
        funds = funds_of(service, "wb-1", "refund")
        self.assertEqual([(f["ref"], f["status"]) for f in funds], [("p03", "accepted")])
        self.assertIn("退款处理中", service.inspect("wb-1")["customer_notice"])
        gateway.push_next("p03", service.apply_receipt)
        view = service.inspect("wb-1")
        self.assertEqual(view["state"], CANCELLED)
        self.assertEqual(view["money"]["refunded"], 3011)
        self.assertIsNone(view["deliverable_bag"])
        # 重复实扣回执不产生新动作
        act, outcome = service.apply_receipt(
            {"channel": "wallet-a", "ref": "p02", "version": 3, "action": "capture",
             "status": "succeeded", "amount": 3011, "parent": "p01"})
        self.assertEqual(outcome, "duplicate")
        self.assertEqual(len(funds_of(service, "wb-1")), 3)


class FundingRetryTest(unittest.TestCase):
    def test_rejected_capture_can_retry_after_reconcile(self):
        gateway = LoopbackGateway(sync_status={"capture": "rejected"})
        service = make_service(gateway)
        service.checkout(BASKET_WB1)
        weigh(service, BAG31)
        service.build_bill("wb-1", "bag-31")
        self.assertEqual(funds_of(service, "wb-1", "capture")[0]["status"], "rejected")
        with self.assertRaises(OrderError):
            service.release("wb-1")
        gateway.sync_status["capture"] = "succeeded"
        service.sync_order("wb-1")
        captures = funds_of(service, "wb-1", "capture")
        self.assertEqual(len(captures), 2)  # 新动作有自己的编号
        self.assertEqual(captures[-1]["status"], "succeeded")
        self.assertEqual(service.release("wb-1")["bag"], "bag-31")


class OfferLockTest(unittest.TestCase):
    def test_ended_offer_does_not_reprice_existing_order(self):
        service = make_service()
        service.checkout(BASKET_WB1)
        service.end_offer("week47")
        with self.assertRaises(OrderError):
            service.checkout(dict(BASKET_WB1, basket="wb-9"))
        weigh(service, BAG31)
        bill = service.build_bill("wb-1", "bag-31")
        self.assertEqual(bill.total, 3011)  # 按确认时锁定条款，不重定价


class PostDeliveryRefundTest(unittest.TestCase):
    def setUp(self):
        self.gateway = LoopbackGateway(sync_status={"refund": "accepted"})
        self.service = make_service(self.gateway)
        self.service.checkout(BASKET_WB2)
        weigh(self.service, [{"store": "f1", "scale": "s3", "ticket": "w51",
                              "revision": 1, "bag": "bag-51", "line": "L-c",
                              "sku": "apple", "grams": 1800, "by": "picker-1"}])
        self.service.build_bill("wb-2", "bag-51")

    def test_refund_only_after_delivery(self):
        with self.assertRaises(OrderError):
            self.service.refund_line("wb-2", "L-c", 600)
        self.service.release("wb-2")
        with self.assertRaises(OrderError):
            self.service.refund_line("wb-2", "L-c", 600)
        self.service.deliver("wb-2")
        result = self.service.refund_line("wb-2", "L-c", 600)
        self.assertEqual(result["amount"], 1095)

    def test_partial_refund_uses_transacted_share_and_settles_line(self):
        self.service.release("wb-2")
        self.service.deliver("wb-2")
        first = self.service.refund_line("wb-2", "L-c", 600)
        self.assertEqual((first["amount"], first["status"]), (1095, "accepted"))
        # 退款未确认时顾客应能看出款项正在处理
        self.assertIn("退款处理中", self.service.inspect("wb-2")["customer_notice"])
        with self.assertRaises(OrderError):
            self.service.refund_line("wb-2", "L-c", 100)  # 处理中不可重复退
        self.gateway.queue_async({"channel": "wallet-a", "ref": first["ref"],
                                  "version": 2, "action": "refund",
                                  "status": "succeeded", "parent": first["parent"],
                                  "amount": first["amount"]})
        self.gateway.deliver_async(self.service.apply_receipt)
        second = self.service.refund_line("wb-2", "L-c", 1200)
        self.assertEqual(second["amount"], 2189)  # 整行退清处理余分：3284-1095
        view = self.service.inspect("wb-2")
        self.assertEqual(view["line_refunds"]["L-c"], {"grams": 600, "amount": 1095})
        # 不因退款后不满门槛再向住户扣款：账单与实扣不变
        self.assertEqual(view["bills"][0]["total"], 3834)
        self.assertEqual(view["money"]["captured"], 3834)
        self.assertEqual(view["money"]["refund_pending"], 2189)

    def test_refund_clamps_to_transacted_grams(self):
        self.service.release("wb-2")
        self.service.deliver("wb-2")
        result = self.service.refund_line("wb-2", "L-c", 99999)
        self.assertEqual(result["amount"], 3284)  # 不超过成交行净额


class InspectViewTest(unittest.TestCase):
    def test_customer_service_view(self):
        service = make_service()
        service.checkout(BASKET_WB1)
        weigh(service, BAG31)
        service.build_bill("wb-1", "bag-31")
        weigh(service, BAG32)
        service.build_bill("wb-1", "bag-32")
        view = service.inspect("wb-1")
        self.assertEqual(view["state"], PENDING_CUSTOMER)
        self.assertEqual(len(view["bills"]), 2)
        self.assertEqual(view["bills"][0]["replaced_by"], 2)  # 哪版在哪一步被替换
        self.assertEqual(view["money"]["captured"], 3011)     # 钱实际实扣到哪里
        self.assertIsNone(view["deliverable_bag"])            # 待确认，无可交付袋
        self.assertEqual(view["offer"]["offer"], "week47")    # 锁定条款快照
        self.assertEqual(view["quote"]["total"], 3007)        # 预估仅供参考


if __name__ == "__main__":
    unittest.main()
