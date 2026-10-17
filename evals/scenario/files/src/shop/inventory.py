"""Stock: what needs reordering, and how much."""

import logging

from shop.db import connect

log = logging.getLogger("shop.inventory")
TARGET_WEEKS = 4


def needs_reorder(stock: int, reorder_at: int) -> bool:
    """A product is reordered when its stock is at or below its threshold."""
    return stock <= reorder_at


def reorder_quantity(stock: int, reorder_at: int, weekly_sales: int) -> int:
    """Enough to cover TARGET_WEEKS of sales above the threshold; never negative."""
    wanted = reorder_at + weekly_sales * TARGET_WEEKS - stock
    return max(wanted, 0)


def to_reorder(connection=None) -> list[str]:
    """SKUs of products that need reordering, by SKU."""
    connection = connection or connect()
    rows = connection.execute("SELECT sku, stock, reorder_at FROM products ORDER BY sku").fetchall()
    found = [row["sku"] for row in rows if needs_reorder(row["stock"], row["reorder_at"])]
    log.info("%d products to reorder", len(found))
    return found
