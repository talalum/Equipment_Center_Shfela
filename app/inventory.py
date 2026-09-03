"""
Stock status calculation.

    net issued = Σ(applied issuance lines) − Σ(adjustments.delta)
    remaining  = standard − net issued
    shortage   = max(0, net issued)

The system assumes the warehouse starts full at the standard quantity, and that
every issuance creates a shortage until stock is replenished. `remaining` may
rise above the standard after a large delivery — that is allowed, and then
`shortage` is 0.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.db import connect
from app.repo import Item


@dataclass(frozen=True)
class ItemStatus:
    item: Item
    issued: int  # total issued according to the emails taken in
    adjusted: int  # total of the manual movements (positive = stock added)

    @property
    def issued_net(self) -> int:
        return self.issued - self.adjusted

    @property
    def remaining(self) -> int:
        return self.item.standard_qty - self.issued_net

    @property
    def shortage(self) -> int:
        return max(0, self.issued_net)

    @property
    def in_shortage(self) -> bool:
        return self.shortage > 0


def _issued_totals() -> dict[int, int]:
    rows = connect().execute(
        """
        SELECT l.item_id AS item_id, SUM(l.qty) AS total
        FROM issuance_lines l
        JOIN issuances i ON i.id = l.issuance_id
        WHERE i.status = 'applied' AND l.item_id IS NOT NULL
        GROUP BY l.item_id
        """
    )
    return {r["item_id"]: int(r["total"] or 0) for r in rows}


def _adjustment_totals() -> dict[int, int]:
    rows = connect().execute(
        "SELECT item_id, SUM(delta) AS total FROM adjustments GROUP BY item_id"
    )
    return {r["item_id"]: int(r["total"] or 0) for r in rows}


def status_for_all(items: list[Item]) -> list[ItemStatus]:
    """Status of every item. Two aggregate queries only, not one query per item."""
    issued = _issued_totals()
    adjusted = _adjustment_totals()
    return [ItemStatus(item=i, issued=issued.get(i.id, 0), adjusted=adjusted.get(i.id, 0)) for i in items]


def status_for_item(item: Item) -> ItemStatus:
    issued = connect().execute(
        """
        SELECT COALESCE(SUM(l.qty), 0) AS total
        FROM issuance_lines l JOIN issuances i ON i.id = l.issuance_id
        WHERE i.status = 'applied' AND l.item_id = ?
        """,
        (item.id,),
    ).fetchone()["total"]
    adjusted = connect().execute(
        "SELECT COALESCE(SUM(delta), 0) AS total FROM adjustments WHERE item_id = ?", (item.id,)
    ).fetchone()["total"]
    return ItemStatus(item=item, issued=int(issued), adjusted=int(adjusted))


_SORT_KEYS = {
    "sku": lambda s: s.item.sku,
    "name": lambda s: s.item.name,
    "standard": lambda s: s.item.standard_qty,
    "issued": lambda s: s.issued,
    "remaining": lambda s: s.remaining,
    "shortage": lambda s: s.shortage,
}


def sort_statuses(statuses: list[ItemStatus], sort: str = "shortage", direction: str = "desc") -> list[ItemStatus]:
    """
    The default is shortage descending — whatever needs action is at the top, always.
    A stable tie-break by SKU keeps the order from jumping between refreshes.
    """
    key = _SORT_KEYS.get(sort, _SORT_KEYS["shortage"])
    ordered = sorted(statuses, key=lambda s: s.item.sku)
    return sorted(ordered, key=key, reverse=(direction != "asc"))
