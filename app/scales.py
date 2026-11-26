"""电子秤回传：秤单版本、冲突处理与装袋候选。

秤单由 (store, scale, ticket) 标识，revision 递增。同一 revision 内容冲突需店员
更正（提交更高 revision）。装袋记录由秤单集合标识；重称和换货形成可辨认的候选，
结算所用的秤单必须确实属于顾客认可的那一篮（按 revision 钉住）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


class ScaleConflict(Exception):
    """同一 revision 内容冲突，需店员更正后才能组账。"""


class BagError(Exception):
    """装袋候选不完整或不合法。"""


@dataclass(frozen=True)
class ScaleTicket:
    store: str
    scale: str
    ticket: str
    revision: int
    bag: str
    line: str
    sku: str
    grams: int
    by: str

    @classmethod
    def from_dict(cls, data: dict) -> "ScaleTicket":
        return cls(
            store=data["store"],
            scale=data["scale"],
            ticket=data["ticket"],
            revision=int(data["revision"]),
            bag=data["bag"],
            line=data["line"],
            sku=data["sku"],
            grams=int(data["grams"]),
            by=data.get("by", ""),
        )

    def key(self) -> tuple:
        return (self.store, self.scale, self.ticket, self.revision)

    def ticket_id(self) -> tuple:
        return (self.store, self.scale, self.ticket)

    def ref(self) -> str:
        return f"{self.store}/{self.scale}/{self.ticket}@{self.revision}"


@dataclass
class Bag:
    """一个装袋候选：钉住每张秤单指向该袋的最高 revision。"""
    bag: str
    tickets: list[ScaleTicket]

    def content(self) -> list[tuple]:
        return sorted((t.line, t.sku, t.grams) for t in self.tickets)


class ScaleBook:
    def __init__(self) -> None:
        self._tickets: dict[tuple, ScaleTicket] = {}
        self._conflicts: list[dict] = []

    def ingest(self, msg: dict) -> tuple[ScaleTicket, str]:
        """回传一张秤单。返回 (生效秤单, accepted|duplicate|conflict)。"""
        ticket = ScaleTicket.from_dict(msg)
        existing = self._tickets.get(ticket.key())
        if existing is not None:
            if existing == ticket:
                return existing, "duplicate"
            self._conflicts.append({
                "key": ticket.key(),
                "kept": existing.ref(),
                "rejected": dict(msg),
            })
            return existing, "conflict"
        self._tickets[ticket.key()] = ticket
        return ticket, "accepted"

    @property
    def conflicts(self) -> list[dict]:
        return list(self._conflicts)

    def is_conflicted(self, key: tuple) -> bool:
        return any(c["key"] == key for c in self._conflicts)

    def bag(self, bag_id: str) -> Bag:
        """解析装袋候选：每张秤单取指向该袋的最高 revision。"""
        by_ticket: dict[tuple, ScaleTicket] = {}
        for ticket in self._tickets.values():
            if ticket.bag != bag_id:
                continue
            current = by_ticket.get(ticket.ticket_id())
            if current is None or ticket.revision > current.revision:
                by_ticket[ticket.ticket_id()] = ticket
        members = sorted(by_ticket.values(), key=lambda t: (t.line, t.ticket_id()))
        if not members:
            raise BagError(f"没有属于装袋 {bag_id} 的秤单")
        conflicted = [t.ref() for t in members if self.is_conflicted(t.key())]
        if conflicted:
            raise ScaleConflict(f"装袋 {bag_id} 含冲突秤单 {conflicted}，需店员更正")
        lines = [t.line for t in members]
        if len(set(lines)) != len(lines):
            raise BagError(f"装袋 {bag_id} 同一行出现多张秤单")
        return Bag(bag_id, members)

    def bags(self) -> list[str]:
        return sorted({t.bag for t in self._tickets.values()})
