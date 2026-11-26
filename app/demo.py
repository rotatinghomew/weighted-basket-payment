"""按样本端到端回放：python3 -m app.demo

故事一（wb-1，对齐 samples/funds.ndjson）：结账冻结 → 拣货组账实扣未知 →
门店重称换货超上限待顾客确认 → 顾客取消 → 旧实扣对账为已收 → 整单退款。
故事二（wb-2）：称重下单 → 放行交付 → 交付后按部分重量退款。
"""
import json
from pathlib import Path

from .datafiles import load_catalog, load_offers, read_ndjson
from .funds import FundsLedger, LoopbackGateway, ReplayGateway
from .service import OrderService, RefGen

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def show(title: str, obj) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def story_cancel_during_reconfirm() -> None:
    print("\n########## 故事一：重新确认期间取消，旧实扣从未知到对账退款 ##########")
    receipts = read_ndjson(SAMPLES / "funds.ndjson")
    gateway = ReplayGateway(receipts)
    service = OrderService(catalog=load_catalog(SAMPLES / "catalog.ndjson"),
                           offers=load_offers(SAMPLES / "offers.ndjson"),
                           ledger=FundsLedger(gateway),
                           ref_gen=RefGen(preset=["p01", "p02", "p03"]))
    basket = read_ndjson(SAMPLES / "baskets.ndjson")[0]

    service.checkout(basket)
    show("1. 结账：冻结最高承诺 3400（p01），预估仅供参考", service.inspect("wb-1")["quote"])

    for row in read_ndjson(SAMPLES / "weights.ndjson")[:2]:
        service.ingest_ticket(row)
    bill = service.build_bill("wb-1", "bag-31")
    show("2. 拣货组账 v1：按实称 1300g/1300g 重算，实价 3011 ≤ 上限，"
         "提交实扣 p02 → 渠道回 unknown（不能证明钱已扣）",
         {"total": bill.total, "funds": service.inspect("wb-1")["funds"]})

    for row in read_ndjson(SAMPLES / "weights.ndjson")[2:]:
        service.ingest_ticket(row)
    bill2 = service.build_bill("wb-1", "bag-32")
    show("3. 第二位店员重称换货：pear→apple 补回满减，但整篮 4097 超上限，"
         "须顾客再确认；旧实扣未知，不会再扣一笔",
         {"total": bill2.total, "blocks": bill2.blocks,
          "tickets_v2": bill2.tickets, "state": service.orders["wb-1"].state})

    service.cancel("wb-1")
    show("4. 顾客取消：p02 仍未知，不撤销冻结也不重复扣款，等待对账",
         service.inspect("wb-1")["customer_notice"])

    gateway.push_next("p02", service.apply_receipt)   # p02 v3 succeeded
    gateway.push_next("p02", service.apply_receipt)   # p02 v2 accepted（迟到，按低版本搁置）
    show("5. 异步回执乱序到达：v3 已收先成立，v2 accepted 迟到不推翻；"
         "确知已收且货不再交付 → 退款 p03（处理中顾客可见）",
         service.inspect("wb-1")["funds"])

    gateway.push_next("p03", service.apply_receipt)   # p03 v2 succeeded
    view = service.inspect("wb-1")
    show("6. 退款成功，订单关闭；客服视图：版本替换、资金去向、可交付袋",
         {"state": view["state"], "money": view["money"],
          "bills": [(b["version"], b["status"], b["replaced_by"]) for b in view["bills"]],
          "deliverable": view["deliverable_note"]})


def story_partial_refund_after_delivery() -> None:
    print("\n########## 故事二：交付后按部分重量退款 ##########")
    gateway = LoopbackGateway(sync_status={"refund": "accepted"})
    service = OrderService(catalog=load_catalog(SAMPLES / "catalog.ndjson"),
                           offers=load_offers(SAMPLES / "offers.ndjson"),
                           ledger=FundsLedger(gateway))
    basket = read_ndjson(SAMPLES / "baskets.ndjson")[1]

    service.checkout(basket)
    service.ingest_ticket({"store": "f1", "scale": "s3", "ticket": "w51", "revision": 1,
                           "bag": "bag-51", "line": "L-c", "sku": "apple",
                           "grams": 1800, "by": "picker-1"})
    bill = service.build_bill("wb-2", "bag-51")
    voucher = service.release("wb-2")
    service.deliver("wb-2")
    show("1. 称重 1800g 组账 3834 ≤ 上限 5000，实扣成功后放行交付",
         {"total": bill.total, "voucher": voucher})

    first = service.refund_line("wb-2", "L-c", 600)
    show("2. 交付后退 600g：按成交行净额 3284 的比例退 1095，回执前顾客可见处理中",
         {"refund": first, "notice": service.inspect("wb-2")["customer_notice"]})

    gateway.queue_async({"channel": "wallet-a", "ref": first["ref"], "version": 2,
                         "action": "refund", "status": "succeeded",
                         "parent": first.get("parent"), "amount": first["amount"]})
    gateway.deliver_async(service.apply_receipt)
    second = service.refund_line("wb-2", "L-c", 1200)
    show("3. 退清整行：累计可退按整行净额 3284 结清余分，本次 2189；"
         "不因退款后不满门槛再向住户扣款",
         {"refund": second, "line_refunds": service.inspect("wb-2")["line_refunds"],
          "money": service.inspect("wb-2")["money"]})


if __name__ == "__main__":
    story_cancel_during_reconfirm()
    story_partial_refund_after_delivery()
