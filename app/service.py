"""称重订单服务：称重回传、顾客确认、资金回执与出货放行。

约定（出自《称重与资金协议》）：
- 账单按已认可的秤单与确认时锁定的活动条款重算，不沿用预估，也不因活动结束重定价；
- 整篮实价高于已确认上限，或换货超出允诺范围，须让顾客看到新整篮和实际差额再确认；
  对上一版账单的同意不能拿来批准新账单；
- 任何实扣结果未知时，不再对同一订单扣款，也不把货当作已付款而出店；
- 确知已收且这份货不再交付时退款；仅冻结时才可撤销冻结；
- 交付只认最后确认的那份装袋记录与对应资金结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import pricing
from .funds import FundsLedger, LoopbackGateway, TERMINAL
from .scales import ScaleBook


class OrderError(Exception):
    """业务规则拒绝。"""


# 订单状态
PLACED = "placed"                    # 已下单，等待拣货称重
PENDING_CUSTOMER = "pending_customer"  # 最新账单待顾客确认
CONFIRMED = "confirmed"              # 当前账单已确认，资金处理中
CANCEL_PENDING = "cancel_pending"    # 取消中：等待资金对账或退款
CANCELLED = "cancelled"
RELEASED = "released"                # 已出店（交付凭证已生成）
DELIVERED = "delivered"


class RefGen:
    """业务编号生成：提交按编号去重；可预置编号以对齐样本回传。"""

    def __init__(self, preset: list[str] | None = None) -> None:
        self._preset = list(preset or [])
        self._n: dict[str, int] = {}

    def new_ref(self, order_id: str, action: str) -> str:
        if self._preset:
            return self._preset.pop(0)
        n = self._n.get(order_id, 0) + 1
        self._n[order_id] = n
        return f"{order_id}-{action}-{n}"


@dataclass
class BillVersion:
    version: int
    bag: str
    tickets: list[str]                 # 钉住的秤单 "store/scale/ticket@revision"
    lines: list[pricing.PricedLine]
    merchandise: int
    shipping: int
    total: int
    status: str                        # confirmed | pending_customer | superseded | rejected
    consent: str | None                # checkout_consent | customer_confirm
    notes: list[str]                   # 信息性变化（缺货、允诺内替代）
    blocks: list[str]                  # 需顾客重新确认的原因
    diff_vs_cap: int                   # 与已确认上限的实际差额
    diff_vs_previous: int | None       # 与上一版账单的差额
    replaced_by: int | None = None


@dataclass
class Order:
    order: str
    store: str
    customer: str
    offer: pricing.OfferTerms          # 确认时锁定的条款快照
    max_pay: int
    items: dict[str, dict]             # line → {sku, min, max, allow_group}
    state: str = PLACED
    quote: dict | None = None          # 结账预估（仅供参考，结算必须重算）
    bills: list[BillVersion] = field(default_factory=list)
    funds: list[str] = field(default_factory=list)   # 资金动作 ref，按提交顺序
    voucher: dict | None = None
    line_refunds: dict[str, dict] = field(default_factory=dict)          # 已成功
    pending_line_refunds: dict[str, dict] = field(default_factory=dict)  # 处理中
    funding_note: str = ""


def _line_view(pl: pricing.PricedLine) -> dict:
    return {"line": pl.line, "sku": pl.sku, "grams": pl.grams, "per_kg": pl.per_kg,
            "gross": pl.gross, "single_off": pl.single_off,
            "basket_off": pl.basket_off, "net": pl.net}


def _bill_view(b: BillVersion) -> dict:
    return {"version": b.version, "bag": b.bag, "tickets": list(b.tickets),
            "lines": [_line_view(pl) for pl in b.lines],
            "merchandise": b.merchandise, "shipping": b.shipping, "total": b.total,
            "status": b.status, "consent": b.consent,
            "notes": list(b.notes), "blocks": list(b.blocks),
            "diff_vs_cap": b.diff_vs_cap, "diff_vs_previous": b.diff_vs_previous,
            "replaced_by": b.replaced_by}


class OrderService:
    def __init__(self, *, catalog: dict, offers: dict | None = None,
                 ledger: FundsLedger | None = None, channel: str = "wallet-a",
                 ref_gen: RefGen | None = None) -> None:
        self.catalog = {
            sku: (e if isinstance(e, pricing.CatalogEntry) else pricing.CatalogEntry(**e))
            for sku, e in catalog.items()
        }
        self.offers = {}
        for offer_id, terms in (offers or {}).items():
            self.offers[offer_id] = {
                "terms": terms if isinstance(terms, pricing.OfferTerms)
                else pricing.OfferTerms.from_dict({"offer": offer_id, **terms}),
                "active": True,
            }
        self.ledger = ledger or FundsLedger(LoopbackGateway())
        self.channel = channel
        self.ref_gen = ref_gen or RefGen()
        self.scales = ScaleBook()
        self.orders: dict[str, Order] = {}
        self._voucher_seq = 0

    # ------------------------------------------------------------------ 下单

    def checkout(self, msg: dict) -> Order:
        """结账：顾客选定允许重量区间与可替代组，看到预估与最高应付，并冻结上限。"""
        offer_id = msg["offer"]
        entry = self.offers.get(offer_id)
        if entry is None:
            raise OrderError(f"未知活动 {offer_id}")
        if not entry["active"]:
            raise OrderError(f"活动 {offer_id} 已结束，不能按该活动下单")
        items = {i["line"]: dict(i) for i in msg["items"]}
        for item in items.values():
            if item["sku"] not in self.catalog:
                raise OrderError(f"未知商品 {item['sku']}")
        order = Order(order=msg["basket"], store=msg["store"], customer=msg["customer"],
                      offer=entry["terms"], max_pay=int(msg["max_pay"]), items=items)
        # 预估按申报上限重量计算，仅供结账时参考；实际结算必须按已认可重量重算。
        estimate = [(line, i["sku"], int(i["max"])) for line, i in items.items()]
        quote = pricing.compute_bill(estimate, order.offer, self.catalog)
        order.quote = {"basis": "declared_max",
                       "lines": [_line_view(pl) for pl in quote.lines],
                       "merchandise": quote.merchandise, "shipping": quote.shipping,
                       "total": quote.total, "max_pay": order.max_pay}
        self.orders[order.order] = order
        self._submit(order, "hold", order.max_pay, purpose="hold:checkout")
        return order

    def end_offer(self, offer_id: str) -> None:
        """活动结束：新下单拒绝；已确认订单按锁定条款继续，不重定价。"""
        self.offers[offer_id]["active"] = False

    # -------------------------------------------------------------- 称重回传

    def ingest_ticket(self, msg: dict) -> dict:
        ticket, outcome = self.scales.ingest(msg)
        return {"ticket": ticket.ref(), "bag": ticket.bag, "outcome": outcome}

    def build_bill(self, order_id: str, bag_id: str) -> BillVersion:
        """按装袋候选组账：依据已认可重量与锁定条款重算，形成新版账单。"""
        order = self._order(order_id)
        if order.state in (CANCEL_PENDING, CANCELLED):
            raise OrderError("订单已取消，不能再组账")
        if order.state in (RELEASED, DELIVERED):
            raise OrderError("订单已出店，不能再改篮")
        bag = self.scales.bag(bag_id)
        for t in bag.tickets:
            if t.line not in order.items:
                raise OrderError(f"装袋 {bag_id} 含有本订单之外的行 {t.line}")
            if t.sku not in self.catalog:
                raise OrderError(f"装袋 {bag_id} 含未知商品 {t.sku}")
        last = order.bills[-1] if order.bills else None
        if (last is not None and last.bag == bag_id
                and sorted((pl.line, pl.sku, pl.grams) for pl in last.lines) == bag.content()):
            return last  # 同一装袋重复组账，幂等
        priced = pricing.compute_bill(bag.content(), order.offer, self.catalog)
        notes: list[str] = []
        blocks: list[str] = []
        weighed = {t.line for t in bag.tickets}
        for line in sorted(set(order.items) - weighed):
            notes.append(f"{line}（{order.items[line]['sku']}）缺货未拣，本篮不计")
        for t in bag.tickets:
            declared = order.items[t.line]
            if t.sku != declared["sku"]:
                same_group = (self.catalog[t.sku].group
                              == self.catalog[declared["sku"]].group)
                if declared.get("allow_group") and same_group:
                    notes.append(f"{t.line} 由 {declared['sku']} 换为同组 {t.sku}（顾客已允许替代）")
                else:
                    blocks.append(f"{t.line} 由 {declared['sku']} 换为 {t.sku}，超出允诺范围")
            lo, hi = int(declared["min"]), int(declared["max"])
            if not lo <= t.grams <= hi:
                blocks.append(f"{t.line} 实称 {t.grams}g 超出允许区间 [{lo}, {hi}]")
        diff_cap = priced.total - order.max_pay
        if diff_cap > 0:
            blocks.append(f"整篮实价 {priced.total} 超过已确认上限 {order.max_pay}（超出 {diff_cap}）")
        previous = self._current_bill(order)
        version = len(order.bills) + 1
        for b in order.bills:
            if b.status in ("confirmed", "pending_customer"):
                b.status = "superseded"
                b.replaced_by = version
        status = "pending_customer" if blocks else "confirmed"
        bill = BillVersion(
            version=version, bag=bag_id,
            tickets=[t.ref() for t in bag.tickets],
            lines=priced.lines, merchandise=priced.merchandise,
            shipping=priced.shipping, total=priced.total,
            status=status, consent=None if blocks else "checkout_consent",
            notes=notes, blocks=blocks, diff_vs_cap=diff_cap,
            diff_vs_previous=(priced.total - previous.total) if previous else None,
        )
        order.bills.append(bill)
        if blocks:
            order.state = PENDING_CUSTOMER
        else:
            order.state = CONFIRMED
            self._ensure_funding(order)
        return bill

    # -------------------------------------------------------------- 顾客确认

    def confirm(self, order_id: str, version: int) -> Order:
        """顾客确认指定版本账单；对上一版账单的同意不能拿来批准新账单。"""
        order = self._order(order_id)
        if order.state in (CANCEL_PENDING, CANCELLED):
            raise OrderError("订单取消中，不能再确认账单")
        if not order.bills or order.bills[-1].version != version:
            raise OrderError("只能确认最新版本的账单；对上一版账单的同意不能拿来批准新账单")
        bill = order.bills[-1]
        if bill.status != "pending_customer":
            raise OrderError("该版本账单无需或不能由顾客确认")
        bill.status = "confirmed"
        bill.consent = "customer_confirm"
        order.state = CONFIRMED
        self._ensure_funding(order)
        return order

    def reject_bill(self, order_id: str, version: int) -> Order:
        """顾客拒绝最新待确认账单：回到待拣货，门店需重新装袋或取消。"""
        order = self._order(order_id)
        if not order.bills or order.bills[-1].version != version:
            raise OrderError("只能处理最新版本的账单")
        bill = order.bills[-1]
        if bill.status != "pending_customer":
            raise OrderError("该版本账单不在待确认状态")
        bill.status = "rejected"
        order.state = PLACED
        return order

    # ------------------------------------------------------------------ 取消

    def cancel(self, order_id: str) -> Order:
        order = self._order(order_id)
        if order.state in (RELEASED, DELIVERED):
            raise OrderError("订单已出店，取消请走售后退款")
        if order.state == CANCELLED:
            return order
        order.state = CANCEL_PENDING
        self._finalize_cancel(order)
        return order

    def _finalize_cancel(self, order: Order) -> None:
        acts = self._actions(order)
        for a in acts:
            if a.action in ("hold", "capture") and a.status not in TERMINAL:
                order.funding_note = (f"{a.action} {a.ref} 结果未知，等待渠道对账；"
                                      "不会对同一订单再扣一笔，也不会放行")
                return
        captured = [a for a in acts if a.action == "capture" and a.status == "succeeded"]
        if captured:
            # 确知已收且这份货不再交付：整单退款（含原实际运费，因未配送）。
            for cap in captured:
                if not self._find_purpose(order, f"refund:{cap.ref}"):
                    self._submit(order, "refund", cap.amount, parent=cap.ref,
                                 purpose=f"refund:{cap.ref}")
            refunds = [a for a in self._actions(order) if a.action == "refund"
                       and a.parent in {c.ref for c in captured}]
            if len(refunds) >= len(captured) and all(r.status == "succeeded" for r in refunds):
                order.state = CANCELLED
                order.funding_note = ""
            else:
                order.funding_note = "退款处理中"
            return
        # 仅冻结时才可撤销冻结。
        holds = [a for a in acts if a.action == "hold" and a.status == "succeeded"]
        for hold in holds:
            if not self._find_purpose(order, f"void:{hold.ref}"):
                self._submit(order, "void", hold.amount, parent=hold.ref,
                             purpose=f"void:{hold.ref}")
        voids = [a for a in self._actions(order) if a.action == "void"]
        if voids and all(v.status == "succeeded" for v in voids) or not holds:
            order.state = CANCELLED
            order.funding_note = ""
        else:
            order.funding_note = "撤销冻结处理中"

    # ------------------------------------------------------------------ 资金

    def _submit(self, order: Order, action: str, amount: int,
                parent: str | None = None, purpose: str = ""):
        ref = self.ref_gen.new_ref(order.order, action)
        act = self.ledger.submit(channel=self.channel, ref=ref, action=action,
                                 amount=amount, parent=parent, order=order.order,
                                 purpose=purpose)
        if ref not in order.funds:
            order.funds.append(ref)
        return act

    def _actions(self, order: Order) -> list:
        return [self.ledger.actions[(self.channel, ref)]
                for ref in order.funds if (self.channel, ref) in self.ledger.actions]

    @staticmethod
    def _purpose_matches(act, base: str) -> bool:
        return act.purpose == base or act.purpose.startswith(base + "#")

    def _find_purpose(self, order: Order, base: str):
        """该用途下仍有效力的动作（被拒绝的可换新编号重试）。"""
        for a in self._actions(order):
            if self._purpose_matches(a, base) and a.status != "rejected":
                return a
        return None

    def _submit_with_purpose(self, order: Order, action: str, amount: int,
                             parent: str | None, base: str):
        attempts = sum(1 for a in self._actions(order)
                       if self._purpose_matches(a, base))
        purpose = base if attempts == 0 else f"{base}#{attempts}"
        return self._submit(order, action, amount, parent=parent, purpose=purpose)

    def _ensure_funding(self, order: Order) -> None:
        """为当前已确认账单落实对应资金结果；任何实扣未知时不再扣一笔。"""
        if order.state == CANCEL_PENDING:
            return self._finalize_cancel(order)
        bill = self._current_bill(order)
        if bill is None:
            return
        acts = self._actions(order)
        base = f"capture:v{bill.version}"
        funded = any(a.action == "capture" and a.status == "succeeded"
                     and self._purpose_matches(a, base) and a.amount == bill.total
                     for a in acts)
        if funded or bill.total == 0:
            order.funding_note = ""
            return
        for a in acts:
            if a.action in ("hold", "capture") and a.status not in TERMINAL:
                order.funding_note = (f"{a.action} {a.ref} 结果未知，等待对账；"
                                      "不会对同一订单再扣一笔，也不会放行")
                return
        # 旧版本已实扣的货不再交付：先走退款，再为新版本扣款。
        for a in acts:
            if (a.action == "capture" and a.status == "succeeded"
                    and not self._purpose_matches(a, base)):
                if not self._find_purpose(order, f"refund:{a.ref}"):
                    self._submit(order, "refund", a.amount, parent=a.ref,
                                 purpose=f"refund:{a.ref}")
        acts = self._actions(order)
        hold = None
        for a in acts:
            if a.action == "hold" and a.status == "succeeded" and a.amount >= bill.total:
                consumed = any(c.action == "capture" and c.parent == a.ref
                               and c.status == "succeeded" for c in acts)
                voided = any(v.action == "void" and v.parent == a.ref
                             and v.status == "succeeded" for v in acts)
                if not consumed and not voided:
                    hold = a
                    break
        if hold is None:
            existing = self._find_purpose(order, f"hold:v{bill.version}")
            if existing is not None and existing.status not in TERMINAL:
                order.funding_note = f"冻结 {existing.ref} 未确认"
                return
            hold = self._submit_with_purpose(order, "hold", bill.total, None,
                                             f"hold:v{bill.version}")
            if hold.status != "succeeded":
                order.funding_note = f"冻结 {hold.ref} 未确认"
                return
        attempt = self._find_purpose(order, base)
        if attempt is not None and attempt.status not in TERMINAL:
            order.funding_note = f"实扣 {attempt.ref} 结果未知，等待对账"
            return
        cap = self._submit_with_purpose(order, "capture", bill.total, hold.ref, base)
        if cap.status == "succeeded":
            order.funding_note = ""
        elif cap.status == "rejected":
            order.funding_note = f"实扣 {cap.ref} 被拒绝，可对账后重试"
        else:
            order.funding_note = f"实扣 {cap.ref} 结果未知，等待对账"

    def apply_receipt(self, receipt: dict):
        """渠道异步回执入口（资金回执）。"""
        act, outcome = self.ledger.apply_receipt(receipt)
        self._on_funds_changed({act.ref})
        return act, outcome

    def query_funds(self, ref: str):
        """脱离会话后按 ref 重取渠道已知结论。"""
        act = self.ledger.query(self.channel, ref)
        if act is not None:
            self._on_funds_changed({act.ref})
        return act

    def sync_order(self, order_id: str) -> dict:
        """对账后重推一次订单资金编排（例如实扣被拒绝后的重试）。"""
        order = self._order(order_id)
        if order.state == CANCEL_PENDING:
            self._finalize_cancel(order)
        elif order.state == CONFIRMED:
            self._ensure_funding(order)
        return self.inspect(order_id)

    def _on_funds_changed(self, refs: set[str]) -> None:
        affected = {a.order for a in self.ledger.actions.values()
                    if a.ref in refs and a.order}
        for order_id in affected:
            order = self.orders[order_id]
            for line, pend in list(order.pending_line_refunds.items()):
                act = self.ledger.get(self.channel, pend["ref"])
                if act is None:
                    continue
                if act.status == "succeeded":
                    order.line_refunds[line] = {"grams": pend["grams"],
                                                "amount": pend["cumulative"]}
                    del order.pending_line_refunds[line]
                elif act.status == "rejected":
                    del order.pending_line_refunds[line]
            if order.state == CANCEL_PENDING:
                self._finalize_cancel(order)
            elif order.state == CONFIRMED:
                self._ensure_funding(order)

    # -------------------------------------------------------------- 出货放行

    def release(self, order_id: str) -> dict:
        """库房放行：只认最后确认的装袋记录与对应资金结果；幂等。"""
        order = self._order(order_id)
        if order.voucher is not None:
            return order.voucher  # 重复实扣回执不会重复发货
        if order.state in (CANCEL_PENDING, CANCELLED):
            raise OrderError("订单已取消，不能放行")
        if order.state == PENDING_CUSTOMER:
            raise OrderError("最新账单待顾客确认，不能放行")
        if order.state in (RELEASED, DELIVERED):
            raise OrderError("订单状态异常：已出店但无凭证")
        bill = self._current_bill(order)
        if bill is None:
            raise OrderError("没有已确认的装袋记录，不能放行")
        for a in self._actions(order):
            if a.action == "capture" and a.status not in TERMINAL:
                raise OrderError(f"实扣 {a.ref} 结果未知，不能把货当作已付款而出店")
        cap = None
        if bill.total > 0:
            cap = next((a for a in self._actions(order)
                        if a.action == "capture" and a.status == "succeeded"
                        and self._purpose_matches(a, f"capture:v{bill.version}")
                        and a.amount == bill.total), None)
            if cap is None:
                raise OrderError("该版已认可账单没有对应的实扣成功结果，不能放行")
        self._voucher_seq += 1
        order.voucher = {"voucher": f"dv-{order.order}-{self._voucher_seq}",
                         "order": order.order, "bag": bill.bag,
                         "bill_version": bill.version,
                         "capture_ref": cap.ref if cap else None,
                         "amount": bill.total}
        order.state = RELEASED
        return order.voucher

    def deliver(self, order_id: str) -> Order:
        order = self._order(order_id)
        if order.state != RELEASED:
            raise OrderError("只有已放行的订单才能确认交付")
        order.state = DELIVERED
        return order

    # -------------------------------------------------------------- 售后退款

    def refund_line(self, order_id: str, line: str, grams: int) -> dict:
        """交付后按一件或一部分重量退款：按成交时该行实际分摊的优惠和金额核对。

        不因退款之后不满门槛再向住户扣款（不重算账单、不追回优惠）。
        """
        order = self._order(order_id)
        if order.state != DELIVERED:
            raise OrderError("交付后才能按行退款；未配送的整单请走取消")
        bill = self._current_bill(order)
        pl = next((l for l in bill.lines if l.line == line), None)
        if pl is None:
            raise OrderError(f"成交账单中没有行 {line}")
        grams = int(grams)
        if grams <= 0:
            raise OrderError("退款重量必须为正")
        if line in order.pending_line_refunds:
            raise OrderError("该行有退款在处理中，请等待渠道回执")
        done = order.line_refunds.get(line, {"grams": 0, "amount": 0})
        new_grams = min(pl.grams, done["grams"] + grams)
        cumulative = pricing.line_refund_cumulative(pl.net, pl.grams, new_grams)
        amount = cumulative - done["amount"]
        if amount <= 0:
            return {"line": line, "amount": 0, "status": "nothing",
                    "refunded_grams": done["grams"], "refunded_amount": done["amount"]}
        cap = next((a for a in self._actions(order)
                    if a.action == "capture" and a.status == "succeeded"
                    and self._purpose_matches(a, f"capture:v{bill.version}")), None)
        if cap is None:
            raise OrderError("该版账单没有已实扣的资金结果，无法退款")
        seq = sum(1 for a in self._actions(order)
                  if a.action == "refund" and a.purpose.startswith(f"refund-line:{line}:")) + 1
        act = self._submit(order, "refund", amount, parent=cap.ref,
                           purpose=f"refund-line:{line}:{seq}")
        if act.status == "succeeded":
            order.line_refunds[line] = {"grams": new_grams, "amount": cumulative}
        else:
            order.pending_line_refunds[line] = {
                "grams": new_grams, "cumulative": cumulative, "ref": act.ref}
        return {"line": line, "amount": amount, "status": act.status, "ref": act.ref,
                "parent": cap.ref,
                "refunded_grams": new_grams if act.status == "succeeded" else done["grams"],
                "refundable_remaining": pl.net - cumulative}

    # ------------------------------------------------------------------ 查询

    def inspect(self, order_id: str) -> dict:
        """客服视图：哪版在哪一步被替换、钱实际冻结/实扣/退到哪里、可交出哪一袋。"""
        order = self._order(order_id)
        acts = self._actions(order)
        deliverable, reason = self._deliverable(order)
        refund_pending = any(a.action == "refund" and a.status not in TERMINAL for a in acts)
        notice = []
        if order.state == CANCEL_PENDING:
            notice.append("取消处理中")
        if refund_pending:
            notice.append("退款处理中")
        if order.funding_note:
            notice.append(order.funding_note)
        return {
            "order": order.order, "state": order.state, "store": order.store,
            "customer": order.customer, "max_pay": order.max_pay,
            "offer": dict(order.offer.__dict__),
            "quote": order.quote,
            "bills": [_bill_view(b) for b in order.bills],
            "funds": [self._action_view(a) for a in acts],
            "money": self._money(order),
            "deliverable_bag": deliverable,
            "deliverable_note": reason,
            "voucher": order.voucher,
            "line_refunds": order.line_refunds,
            "pending_line_refunds": order.pending_line_refunds,
            "customer_notice": "；".join(notice) if notice else "",
        }

    def _deliverable(self, order: Order) -> tuple[str | None, str]:
        if order.voucher is not None:
            return order.voucher["bag"], "已出店（按交付凭证）"
        if order.state in (CANCEL_PENDING, CANCELLED):
            return None, "订单已取消"
        if order.state == PENDING_CUSTOMER:
            return None, "最新账单待顾客确认"
        bill = self._current_bill(order)
        if bill is None:
            return None, "等待称重组账"
        for a in self._actions(order):
            if a.action == "capture" and a.status not in TERMINAL:
                return None, f"实扣 {a.ref} 结果未知"
        if bill.total > 0 and not any(
                a.action == "capture" and a.status == "succeeded"
                and self._purpose_matches(a, f"capture:v{bill.version}")
                and a.amount == bill.total for a in self._actions(order)):
            return None, "该版账单资金未就绪"
        return bill.bag, "可交付"

    def _money(self, order: Order) -> dict:
        acts = self._actions(order)
        captured = {a.parent for a in acts if a.action == "capture" and a.status == "succeeded"}
        voided = {a.parent for a in acts if a.action == "void" and a.status == "succeeded"}
        return {
            "frozen": sum(a.amount for a in acts if a.action == "hold"
                          and a.status == "succeeded"
                          and a.ref not in captured and a.ref not in voided),
            "captured": sum(a.amount for a in acts if a.action == "capture"
                            and a.status == "succeeded"),
            "refunded": sum(a.amount for a in acts if a.action == "refund"
                            and a.status == "succeeded"),
            "refund_pending": sum(a.amount for a in acts if a.action == "refund"
                                  and a.status not in TERMINAL),
            "voided": sum(a.amount for a in acts if a.action == "void"
                          and a.status == "succeeded"),
        }

    def _action_view(self, a) -> dict:
        return {"channel": a.channel, "ref": a.ref, "action": a.action,
                "amount": a.amount, "status": a.status, "version": a.version,
                "parent": a.parent, "purpose": a.purpose, "conflict": a.conflict,
                "receipts": len(a.receipts), "stale": len(a.stale)}

    def _current_bill(self, order: Order) -> BillVersion | None:
        for bill in reversed(order.bills):
            if bill.status == "confirmed":
                return bill
        return None

    def _order(self, order_id: str) -> Order:
        try:
            return self.orders[order_id]
        except KeyError:
            raise OrderError(f"未知订单 {order_id}") from None
