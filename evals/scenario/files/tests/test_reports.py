import sqlite3
import unittest

from shop import reports


def database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT, joined TEXT);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT, placed TEXT, coupon TEXT);"
        "CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER, quantity INTEGER, unit_price_cents INTEGER);"
        "INSERT INTO customers VALUES (1, 'Noa', 'Haifa', '2025-01-01'), (2, 'Dan', 'Eilat', '2025-02-02');"
        "INSERT INTO orders VALUES (1, 1, 'paid', '2026-03-04', NULL), (2, 1, 'shipped', '2026-03-20', 'VIP20'),"
        " (3, 2, 'cancelled', '2026-03-21', NULL), (4, 2, 'paid', '2026-04-01', NULL), (5, 2, 'pending', '2026-03-02', NULL);"
        "INSERT INTO order_items VALUES (1, 1, 1, 2, 1000), (2, 2, 1, 1, 30000), (3, 3, 1, 1, 5000), (4, 4, 1, 1, 100), (5, 5, 1, 1, 700);"
    )
    return connection


class MonthlyRevenue(unittest.TestCase):
    def test_counts_paid_and_shipped_in_the_month(self):
        expected = (2340 + 2500) + 28080
        self.assertEqual(reports.monthly_revenue(2026, 3, database()), expected)

    def test_other_month(self):
        self.assertEqual(reports.monthly_revenue(2026, 4, database()), 117 + 2500)

    def test_empty_month(self):
        self.assertEqual(reports.monthly_revenue(2026, 5, database()), 0)


class TopCustomers(unittest.TestCase):
    def test_orders_counted(self):
        self.assertEqual(reports.top_customers(5, database()), [("Noa", 2), ("Dan", 1)])
