"""The eval's project: a small shop — orders, products, customers in SQLite,
pricing and inventory rules, feature flags kept in three places, and a noisy
unittest suite. Deterministic: the same seed builds the same data."""

from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = HERE / "files"


def build(target: Path) -> None:
    for source in sorted(FILES.rglob("*")):
        if source.is_file() and "__pycache__" not in source.parts:
            destination = target / source.relative_to(FILES)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
    seed(target / "data" / "shop.db")


def seed(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    generator = random.Random(7)
    connection = sqlite3.connect(path)
    connection.executescript((FILES / "src" / "shop" / "schema.sql").read_text())
    cities = ["Haifa", "Tel Aviv", "Jerusalem", "Eilat", "Beersheba", "Nazareth"]
    first = ["Noa", "Ari", "Maya", "Dan", "Lior", "Tamar", "Omer", "Yael", "Eitan", "Shira"]
    last = ["Levi", "Cohen", "Mizrahi", "Peretz", "Biton", "Dahan", "Avraham", "Friedman"]
    for number in range(1, 41):
        connection.execute(
            "INSERT INTO customers (id, name, city, joined) VALUES (?, ?, ?, ?)",
            (
                number,
                f"{generator.choice(first)} {generator.choice(last)}",
                generator.choice(cities),
                f"2025-{generator.randint(1, 12):02d}-{generator.randint(1, 28):02d}",
            ),
        )
    categories = ["kitchen", "garden", "office", "toys", "books"]
    for number in range(1, 61):
        connection.execute(
            "INSERT INTO products (id, sku, name, category, price_cents, stock, reorder_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                number,
                f"SKU-{1000 + number}",
                f"{generator.choice(['Small', 'Large', 'Deluxe', 'Basic', 'Eco'])} {generator.choice(['Mug', 'Lamp', 'Chair', 'Shovel', 'Notebook', 'Puzzle', 'Kettle', 'Planter'])} {number}",
                generator.choice(categories),
                generator.randint(5, 400) * 100 - 1,
                generator.randint(0, 80),
                generator.choice([5, 10, 15, 20]),
            ),
        )
    statuses = ["pending", "paid", "shipped", "shipped", "shipped", "cancelled", "refunded"]
    item = 1
    for number in range(1, 601):
        month = generator.randint(1, 9)
        connection.execute(
            "INSERT INTO orders (id, customer_id, status, placed, coupon) VALUES (?, ?, ?, ?, ?)",
            (
                number,
                generator.randint(1, 40),
                generator.choice(statuses),
                f"2026-{month:02d}-{generator.randint(1, 28):02d}",
                generator.choice([None, None, None, "SPRING10", "VIP20"]),
            ),
        )
        for _ in range(generator.randint(1, 4)):
            product = generator.randint(1, 60)
            price = connection.execute(
                "SELECT price_cents FROM products WHERE id = ?", (product,)
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO order_items (id, order_id, product_id, quantity, unit_price_cents) VALUES (?, ?, ?, ?, ?)",
                (item, number, product, generator.randint(1, 5), price),
            )
            item += 1
    connection.commit()
    connection.close()


if __name__ == "__main__":
    build(Path(sys.argv[1]))
