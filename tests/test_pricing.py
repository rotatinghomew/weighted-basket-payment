"""计价口径测试：单品券、满减分摊、运费门槛与部分退款。"""
import unittest
from pathlib import Path

from app.datafiles import load_catalog, load_offers
from app.pricing import (allocate, compute_bill, line_refund_cumulative,
                         price_line, round_half_up)

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def fixture():
    catalog = load_catalog(SAMPLES / "catalog.ndjson")
    offers = load_offers(SAMPLES / "offers.ndjson")
    return catalog, offers


class RoundTest(unittest.TestCase):
    def test_round_half_up(self):
        self.assertEqual(round_half_up(1212500, 1000), 1213)  # 1212.5 进位
        self.assertEqual(round_half_up(1212499, 1000), 1212)
        self.assertEqual(round_half_up(0, 7), 0)

    def test_price_line(self):
        self.assertEqual(price_line(970, 1250), 1213)   # 四舍五入到分
        self.assertEqual(price_line(1500, 1300), 1950)


class BillTest(unittest.TestCase):
    def test_bag31_matches_captured_amount(self):
        """bag-31 按 week47 重算应得 3011，与样本实扣金额一致。"""
        catalog, offers = fixture()
        bill = compute_bill([("L-a", "pear", 1300), ("L-b", "greens", 1300)],
                            offers["week47"], catalog)
        by_line = {pl.line: pl for pl in bill.lines}
        self.assertEqual(by_line["L-a"].gross, 1950)
        self.assertEqual(by_line["L-a"].single_off, 150)
        self.assertEqual(by_line["L-a"].basket_off, 294)
        self.assertEqual(by_line["L-a"].net, 1506)
        self.assertEqual(by_line["L-b"].basket_off, 206)
        self.assertEqual(by_line["L-b"].net, 1055)
        self.assertEqual(bill.merchandise, 2561)
        self.assertEqual(bill.shipping, 450)   # 净额 2561 未达 3000
        self.assertEqual(bill.total, 3011)

    def test_bag32_substitution_recomputes(self):
        """换货后重算：apple 无单品券，满减重分摊，净额达 3000 免运费。"""
        catalog, offers = fixture()
        bill = compute_bill([("L-a", "apple", 1800), ("L-b", "greens", 1250)],
                            offers["week47"], catalog)
        by_line = {pl.line: pl for pl in bill.lines}
        self.assertEqual(by_line["L-a"].gross, 3384)
        self.assertEqual(by_line["L-a"].single_off, 0)
        self.assertEqual(by_line["L-a"].basket_off, 368)
        self.assertEqual(by_line["L-b"].gross, 1213)
        self.assertEqual(by_line["L-b"].basket_off, 132)
        self.assertEqual(bill.merchandise, 4097)
        self.assertEqual(bill.shipping, 0)
        self.assertEqual(bill.total, 4097)

    def test_missing_line_drops_threshold(self):
        """少一份商品跨回满减门槛：小计不足则无满减，运费恢复。"""
        catalog, offers = fixture()
        bill = compute_bill([("L-a", "pear", 1300)], offers["week47"], catalog)
        self.assertEqual(bill.lines[0].single_off, 150)
        self.assertEqual(bill.lines[0].basket_off, 0)
        self.assertEqual(bill.merchandise, 1800)
        self.assertEqual(bill.shipping, 450)
        self.assertEqual(bill.total, 2250)

    def test_single_coupon_capped_at_line(self):
        catalog, offers = fixture()
        terms = offers["week47"]
        big = type(terms)(terms.offer, terms.single_sku, 99999, terms.basket_min,
                          terms.basket_off, terms.shipping_min, terms.shipping_fee)
        bill = compute_bill([("L-a", "pear", 100)], big, catalog)
        self.assertEqual(bill.lines[0].single_off, 150)  # 不超过该行金额 150
        self.assertEqual(bill.lines[0].net, 0)

    def test_single_coupon_once_per_basket(self):
        catalog, offers = fixture()
        bill = compute_bill([("L-a", "pear", 1000), ("L-b", "pear", 1000)],
                            offers["week47"], catalog)
        offs = [pl.single_off for pl in bill.lines]
        self.assertEqual(sorted(offs), [0, 150])

    def test_shipping_boundary(self):
        catalog, offers = fixture()
        # 净额恰好等于 shipping_min 时免收
        bill = compute_bill([("L-a", "apple", 2000)], offers["week48"], catalog)
        # 3384? apple 2000g → 3760 - 100 = 3660 ≥ 3600 → 免运费
        self.assertEqual(bill.merchandise, 3660)
        self.assertEqual(bill.shipping, 0)


class AllocateTest(unittest.TestCase):
    def test_remainder_by_fraction_then_line_id(self):
        # 小数余量相同按行标识排序
        self.assertEqual(allocate(1, {"L-b": 50, "L-a": 50}), {"L-a": 1, "L-b": 0})
        # 小数余量从大到小
        self.assertEqual(allocate(1, {"L-a": 1, "L-b": 2}), {"L-a": 0, "L-b": 1})
        self.assertEqual(allocate(3, {"L-a": 1, "L-b": 1, "L-c": 1}),
                         {"L-a": 1, "L-b": 1, "L-c": 1})

    def test_zero_amount_and_zero_total(self):
        self.assertEqual(allocate(0, {"L-a": 5}), {"L-a": 0})
        self.assertEqual(allocate(5, {"L-a": 0}), {"L-a": 0})


class RefundTest(unittest.TestCase):
    def test_partial_then_whole_line_settles_remainder(self):
        # 行净额 1055 / 1300g：先退 650g，再退清整行时处理余分
        first = line_refund_cumulative(1055, 1300, 650)
        self.assertEqual(first, 528)  # 527.5 四舍五入
        total = line_refund_cumulative(1055, 1300, 1300)
        self.assertEqual(total, 1055)
        self.assertEqual(total - first, 527)

    def test_cumulative_never_exceeds_net(self):
        self.assertEqual(line_refund_cumulative(1055, 1300, 99999), 1055)
        self.assertEqual(line_refund_cumulative(1055, 1300, 0), 0)


if __name__ == "__main__":
    unittest.main()
