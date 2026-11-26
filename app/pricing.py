"""按《称重与资金协议》计价：单品券、满减分摊、运费与部分退款口径。

金额一律用整数分，重量用整数克。所有结算都按已认可的重量和商品重算，
不沿用预估优惠，也不因活动后来结束而重定价（条款在确认时快照锁定）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


def round_half_up(numerator: int, denominator: int) -> int:
    """非负整数除法四舍五入（协议口径：数量价格乘重量后四舍五入到分）。"""
    if numerator < 0 or denominator <= 0:
        raise ValueError("round_half_up 要求 numerator >= 0 且 denominator > 0")
    quotient, remainder = divmod(numerator, denominator)
    return quotient + 1 if 2 * remainder >= denominator else quotient


@dataclass(frozen=True)
class CatalogEntry:
    sku: str
    per_kg: int  # 每公斤分
    group: str   # 可替代组


@dataclass(frozen=True)
class OfferTerms:
    """确认时适用的活动与运费条款快照。"""
    offer: str
    single_sku: str | None
    single_off: int
    basket_min: int
    basket_off: int
    shipping_min: int
    shipping_fee: int

    @classmethod
    def from_dict(cls, data: dict) -> "OfferTerms":
        return cls(
            offer=data["offer"],
            single_sku=data.get("single_sku"),
            single_off=int(data.get("single_off", 0)),
            basket_min=int(data.get("basket_min", 0)),
            basket_off=int(data.get("basket_off", 0)),
            shipping_min=int(data.get("shipping_min", 0)),
            shipping_fee=int(data.get("shipping_fee", 0)),
        )


@dataclass
class PricedLine:
    line: str
    sku: str
    grams: int
    per_kg: int
    gross: int            # 行原始金额（分）
    single_off: int = 0   # 单品券扣减
    basket_off: int = 0   # 满减分摊
    net: int = 0          # 行净额（成交时该件实际分摊后的金额）


@dataclass
class PricedBill:
    lines: list[PricedLine]
    merchandise: int  # 优惠后商品净额
    shipping: int     # 实际运费
    total: int        # 整篮实价


def price_line(per_kg: int, grams: int) -> int:
    """数量价格按行乘重量后四舍五入到分。"""
    return round_half_up(per_kg * grams, 1000)


def allocate(amount: int, weights: dict[str, int]) -> dict[str, int]:
    """按比例分摊；余分按小数余量从大到小分，余量相同按行标识排序。"""
    keys = sorted(weights)
    total = sum(weights[k] for k in keys)
    result = {k: 0 for k in keys}
    if amount <= 0 or total <= 0:
        return result
    base = {}
    fracs = {}
    for k in keys:
        exact = amount * weights[k]
        base[k] = exact // total
        fracs[k] = exact % total
    remainder = amount - sum(base.values())
    for k in sorted(keys, key=lambda k: (-fracs[k], k))[:remainder]:
        base[k] += 1
    return base


def compute_bill(items, offer: OfferTerms, catalog: dict[str, CatalogEntry]) -> PricedBill:
    """按已认可的重量和商品重算整篮。

    items: (line, sku, grams) 可迭代。返回 PricedBill。
    """
    lines: list[PricedLine] = []
    for line, sku, grams in sorted(items, key=lambda x: x[0]):
        entry = catalog[sku]
        lines.append(PricedLine(line, sku, grams, entry.per_kg,
                                price_line(entry.per_kg, grams)))
    # 单品券先扣该 SKU 行，扣减不超过该行金额，单篮用一次。
    if offer.single_sku and offer.single_off > 0:
        for pl in lines:
            if pl.sku == offer.single_sku:
                pl.single_off = min(offer.single_off, pl.gross)
                break
    after_single = {pl.line: pl.gross - pl.single_off for pl in lines}
    subtotal = sum(after_single.values())
    alloc = {k: 0 for k in after_single}
    # 剩余商品小计达到 basket_min 才享 basket_off，按各行券后价值比例分。
    if offer.basket_off > 0 and subtotal >= offer.basket_min:
        alloc = allocate(min(offer.basket_off, subtotal), after_single)
    for pl in lines:
        pl.basket_off = alloc[pl.line]
        pl.net = pl.gross - pl.single_off - pl.basket_off
    merchandise = sum(pl.net for pl in lines)
    # 运费在优惠后商品净额达到 shipping_min 时免收。
    shipping = 0 if merchandise >= offer.shipping_min else offer.shipping_fee
    return PricedBill(lines, merchandise, shipping, merchandise + shipping)


def line_refund_cumulative(net: int, actual_grams: int, refunded_grams: int) -> int:
    """部分重量退款的累计可退款：按原行净额和实际成交重量的比例计算；整行退清时处理余分。"""
    if refunded_grams <= 0:
        return 0
    if refunded_grams >= actual_grams:
        return net
    return round_half_up(net * refunded_grams, actual_grams)
