import sqlite3
import unittest

from shop import inventory


class NeedsReorder(unittest.TestCase):
    def test_threshold(self):
        for stock, reorder_at, expected in [
            (0, 5, True),
            (5, 5, True),
            (6, 5, False),
            (100, 20, False),
            (19, 20, True),
        ]:
            with self.subTest(stock=stock, reorder_at=reorder_at):
                self.assertEqual(inventory.needs_reorder(stock, reorder_at), expected)


class ReorderQuantity(unittest.TestCase):
    def test_covers_target_weeks(self):
        self.assertEqual(inventory.reorder_quantity(stock=5, reorder_at=10, weekly_sales=3), 17)

    def test_never_negative(self):
        self.assertEqual(inventory.reorder_quantity(stock=500, reorder_at=10, weekly_sales=3), 0)

    def test_no_sales(self):
        self.assertEqual(inventory.reorder_quantity(stock=2, reorder_at=10, weekly_sales=0), 8)


class ToReorder(unittest.TestCase):
    def test_lists_skus_at_or_below_threshold(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE products (sku TEXT, stock INTEGER, reorder_at INTEGER)")
        connection.executemany(
            "INSERT INTO products VALUES (?, ?, ?)", [("B", 5, 5), ("A", 1, 10), ("C", 50, 10)]
        )
        self.assertEqual(inventory.to_reorder(connection), ["A", "B"])
