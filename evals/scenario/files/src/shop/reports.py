"""Reports over orders."""

import logging

from shop.db import connect
from shop.pricing import total

log = logging.getLogger("shop.reports")
COUNTED = ("paid", "shipped")


def order_items(connection, order_id: int) -> list[tuple[int, int]]:
    rows = connection.execute(
        "SELECT quantity, unit_price_cents FROM order_items WHERE order_id = ?", (order_id,)
    ).fetchall()
    return [(row["quantity"], row["unit_price_cents"]) for row in rows]


def monthly_revenue(year: int, month: int, connection=None) -> int:
    """Revenue in cents from paid and shipped orders placed in the month, as customers paid it."""
    connection = connection or connect()
    prefix = f"{year:04d}-{month:02d}-"
    orders = connection.execute(
        "SELECT id, coupon, status FROM orders WHERE placed LIKE ?", (prefix + "%",)
    ).fetchall()
    revenue = 0
    for order in orders:
        if order["status"] not in COUNTED:
            continue
        revenue += total(order_items(connection, order["id"]), order["coupon"])
    log.info("revenue %s: %d", prefix, revenue)
    return revenue


def top_customers(limit: int = 5, connection=None) -> list[tuple[str, int]]:
    """Customers by number of counted orders, most first."""
    connection = connection or connect()
    rows = connection.execute(
        "SELECT customers.name AS name, COUNT(*) AS orders FROM orders JOIN customers"
        " ON customers.id = orders.customer_id WHERE orders.status IN ('paid', 'shipped')"
        " GROUP BY customers.id ORDER BY orders DESC, customers.id LIMIT ?",
        (limit,),
    ).fetchall()
    return [(row["name"], row["orders"]) for row in rows]
