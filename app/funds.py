"""资金台账：冻结、实扣、撤销、退款的去重、乱序回执与按 ref 查询。

协议口径：
- 提交使用业务编号 ref 去重；每项新动作有自己的编号；
- capture 和 void 引用 hold，refund 引用 capture；
- accepted 或 unknown 均不能证明钱已扣，succeeded 和 rejected 为可对账结果；
- 渠道通知可异步补报、可不按顺序到达；资金查询可用 ref 重取已知结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field

TERMINAL = {"succeeded", "rejected"}
NON_TERMINAL = {"accepted", "unknown"}


class ChannelError(Exception):
    """渠道连接异常：动作可能已发出但结论未知。"""


@dataclass
class FundsAction:
    channel: str
    ref: str
    action: str           # hold | capture | void | refund
    amount: int
    parent: str | None = None
    order: str | None = None
    purpose: str = ""
    status: str = "submitted"
    version: int = 0      # 已应用的最高回执版本
    receipts: list[dict] = field(default_factory=list)
    stale: list[dict] = field(default_factory=list)   # 迟到的低版本回执
    conflict: bool = False

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


class FundsLedger:
    def __init__(self, gateway=None) -> None:
        self.gateway = gateway
        self.actions: dict[tuple[str, str], FundsAction] = {}

    def get(self, channel: str, ref: str) -> FundsAction | None:
        return self.actions.get((channel, ref))

    def submit(self, *, channel: str, ref: str, action: str, amount: int,
               parent: str | None = None, order: str | None = None,
               purpose: str = "") -> FundsAction:
        """提交资金动作；同一 ref 重复提交直接返回原记录（去重）。"""
        existing = self.actions.get((channel, ref))
        if existing is not None:
            if (existing.action, existing.amount, existing.parent) != (action, amount, parent):
                raise ValueError(f"业务编号 {ref} 已用于其他动作，不能复用")
            return existing
        act = FundsAction(channel, ref, action, amount, parent, order, purpose)
        self.actions[(channel, ref)] = act
        receipt = None
        if self.gateway is not None:
            try:
                receipt = self.gateway.send(act)
            except ChannelError:
                receipt = None
        if receipt is None:
            # 已发出但未获结论（例如渠道在已扣成功后断开连接）。
            act.status = "unknown"
        else:
            self.apply_receipt(receipt)
        return act

    def apply_receipt(self, receipt: dict) -> tuple[FundsAction, str]:
        """应用一条渠道回执。返回 (动作, applied|duplicate|stale|conflict)。"""
        key = (receipt["channel"], receipt["ref"])
        act = self.actions.get(key)
        if act is None:
            act = FundsAction(receipt["channel"], receipt["ref"], receipt["action"],
                              int(receipt["amount"]), receipt.get("parent"))
            self.actions[key] = act
        if (receipt["action"] != act.action
                or int(receipt["amount"]) != act.amount
                or receipt.get("parent") != act.parent):
            act.conflict = True
            return act, "conflict"
        version = int(receipt["version"])
        if version > act.version:
            if act.status in TERMINAL and receipt["status"] not in TERMINAL:
                # 已有对账结论又回退为非结论，按冲突登记，不推翻。
                act.conflict = True
                return act, "conflict"
            if (act.status in TERMINAL and receipt["status"] in TERMINAL
                    and receipt["status"] != act.status):
                act.conflict = True
            act.status = receipt["status"]
            act.version = version
            act.receipts.append(dict(receipt))
            return act, "applied"
        if version == act.version:
            if receipt["status"] == act.status:
                return act, "duplicate"
            act.conflict = True
            return act, "conflict"
        act.stale.append(dict(receipt))
        return act, "stale"

    def query(self, channel: str, ref: str) -> FundsAction | None:
        """脱离会话后按 ref 重取渠道已知结论并应用。"""
        if self.gateway is not None:
            try:
                receipt = self.gateway.fetch(ref)
            except ChannelError:
                receipt = None
            if receipt is not None:
                self.apply_receipt(receipt)
        return self.actions.get((channel, ref))


class LoopbackGateway:
    """本地回环渠道：按配置同步回执，可模拟断开与异步补报。"""

    def __init__(self, sync_status: dict[str, str] | None = None) -> None:
        self.sync_status = sync_status or {}
        self._versions: dict[str, int] = {}
        self._truth: dict[str, dict] = {}
        self._drop_next = False
        self._async: list[dict] = []

    def drop_next(self) -> None:
        """下一次 send 模拟渠道断开：服务端已记账，客户端收不到回执。"""
        self._drop_next = True

    def queue_async(self, receipt: dict) -> None:
        self._async.append(dict(receipt))

    def send(self, act: FundsAction) -> dict | None:
        version = self._versions.get(act.ref, 0) + 1
        self._versions[act.ref] = version
        receipt = {
            "channel": act.channel,
            "ref": act.ref,
            "version": version,
            "action": act.action,
            "status": self.sync_status.get(act.action, "succeeded"),
            "amount": act.amount,
            "parent": act.parent,
        }
        self._truth[act.ref] = receipt
        if self._drop_next:
            self._drop_next = False
            return None
        return receipt

    def fetch(self, ref: str) -> dict | None:
        return self._truth.get(ref)

    def deliver_async(self, sink) -> list[tuple[FundsAction, str]]:
        """把排队的异步回执逐条推给 sink（通常是 OrderService.apply_receipt）。"""
        outcomes = []
        while self._async:
            outcomes.append(sink(self._async.pop(0)))
        return outcomes


class ReplayGateway:
    """按样本回传：send 给出该 ref 的首条回执，其余经 push_next 异步补报。"""

    def __init__(self, receipts: list[dict]) -> None:
        self._by_ref: dict[str, list[dict]] = {}
        for receipt in receipts:
            self._by_ref.setdefault(receipt["ref"], []).append(dict(receipt))
        self._delivered: dict[str, int] = {}

    def send(self, act: FundsAction) -> dict | None:
        queue = self._by_ref.get(act.ref)
        if not queue:
            raise ChannelError(f"渠道无业务编号 {act.ref} 的回执")
        self._delivered[act.ref] = 1
        return queue[0]

    def fetch(self, ref: str) -> dict | None:
        queue = self._by_ref.get(ref)
        return queue[-1] if queue else None

    def pending(self, ref: str) -> list[dict]:
        return self._by_ref.get(ref, [])[self._delivered.get(ref, 0):]

    def push_next(self, ref: str, sink) -> tuple[FundsAction, str] | None:
        remaining = self.pending(ref)
        if not remaining:
            return None
        self._delivered[ref] += 1
        return sink(remaining[0])
