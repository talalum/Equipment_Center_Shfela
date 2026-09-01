"""
חישוב מצב המלאי.

    הונפק נטו = Σ(שורות הנפקה applied) − Σ(adjustments.delta)
    נשאר      = תקן − הונפק נטו
    חסר       = max(0, הונפק נטו)

המערכת מניחה שהמחסן מתחיל מלא לפי התקן, וכל הנפקה יוצרת חוסר עד שהמלאי מחודש.
`נשאר` יכול לעלות מעל התקן אם התקבלה אספקה גדולה — זה מותר, ואז `חסר` הוא 0.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.db import connect
from app.repo import Item


@dataclass(frozen=True)
class ItemStatus:
    item: Item
    issued: int  # סה"כ שהונפק לפי מיילים שנקלטו
    adjusted: int  # סה"כ תנועות ידניות (חיובי = מלאי שנוסף)

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
    """מצב כל הפריטים. שתי שאילתות צבירה בלבד, לא שאילתה לכל פריט."""
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
    ברירת המחדל היא חוסר יורד — מה שדורש פעולה נמצא למעלה, תמיד.
    שובר שוויון קבוע לפי מק"ט כדי שהסדר לא יקפוץ בין רענונים.
    """
    key = _SORT_KEYS.get(sort, _SORT_KEYS["shortage"])
    ordered = sorted(statuses, key=lambda s: s.item.sku)
    return sorted(ordered, key=key, reverse=(direction != "asc"))
