"""The eval's tasks, in three kinds that recur in real work — questions about the
data, bugs a failing suite points at, and flags kept in three places. Training
tasks are what the reflection round learns from; test tasks are different
instances of the same kinds, run with and without the toolbox it built."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

TESTS = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]


@dataclass
class Task:
    name: str
    kind: str
    prompt: str
    verify: Callable[[Path, str], tuple[bool, str]]
    # (file, old, new): a change committed before the session starts.
    setup: list[tuple[str, str, str]] = field(default_factory=list)


def query(root: Path, sql: str) -> list[tuple]:
    connection = sqlite3.connect(root / "data" / "shop.db")
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def mentions(text: str, *values: str) -> bool:
    flat = text.replace(",", "")
    return all(value.replace(",", "") in flat for value in values)


def tests_pass(root: Path) -> tuple[bool, str]:
    result = subprocess.run(TESTS, cwd=root, capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip().splitlines()[
        -1
    ] if result.stderr.strip() else ""


def tests_untouched(root: Path) -> bool:
    return (
        subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "tests"], cwd=root).returncode == 0
    )


def answer(
    sql: str, render: Callable[[list[tuple]], list[str]]
) -> Callable[[Path, str], tuple[bool, str]]:
    def verify(root: Path, final: str) -> tuple[bool, str]:
        expected = render(query(root, sql))
        return mentions(final, *expected), f"expected {expected}"

    return verify


def revenue(year: int, month: int) -> Callable[[Path, str], tuple[bool, str]]:
    def verify(root: Path, final: str) -> tuple[bool, str]:
        source = root / "src"
        code = f"import sys; sys.path.insert(0, {str(source)!r}); from shop.reports import monthly_revenue; print(monthly_revenue({year}, {month}))"
        cents = int(
            subprocess.run(
                [sys.executable, "-c", code], cwd=root, capture_output=True, text=True
            ).stdout.strip()
        )
        forms = [str(cents), f"{cents / 100:.2f}"]
        return any(mentions(final, form) for form in forms), f"expected {forms}"

    return verify


def fixed(root: Path, final: str) -> tuple[bool, str]:
    passed, summary = tests_pass(root)
    untouched = tests_untouched(root)
    return (
        passed and untouched,
        f"tests {'pass' if passed else 'fail'} ({summary}); tests {'untouched' if untouched else 'CHANGED'}",
    )


def flag(name: str, default: bool, description: str) -> Callable[[Path, str], tuple[bool, str]]:
    def verify(root: Path, final: str) -> tuple[bool, str]:
        config = json.loads((root / "config" / "flags.json").read_text())
        module = (root / "src" / "shop" / "flags.py").read_text()
        docs = (root / "docs" / "FLAGS.md").read_text()
        present = (
            config.get(name) == {"default": default, "description": description}
            and re.search(rf"^{name.upper()} = {default}$", module, re.M) is not None
            and f"| {name} | {'on' if default else 'off'} | {description} |" in docs
        )
        passed, summary = tests_pass(root)
        return present and passed and tests_untouched(
            root
        ), f"flag {'present' if present else 'MISSING or wrong'}; tests {summary}"

    return verify


BUG_PROMPT = "The test suite is failing. Find the bug in src/ and fix it — do not change the tests."


def flag_prompt(name: str, default: bool, description: str) -> str:
    state = "on" if default else "off"
    return f'Add a feature flag `{name}`, {state} by default, described as "{description}".'


TRAINING = [
    Task(
        "pending-orders",
        "data",
        "How many orders are still pending? Answer with the number.",
        answer(
            "SELECT COUNT(*) FROM orders WHERE status = 'pending'", lambda rows: [str(rows[0][0])]
        ),
    ),
    Task(
        "reorder-skus",
        "data",
        "Which products need reordering right now (stock at or below their reorder threshold)? List their SKUs.",
        answer(
            "SELECT sku FROM products WHERE stock <= reorder_at ORDER BY sku",
            lambda rows: [row[0] for row in rows],
        ),
    ),
    Task(
        "june-revenue",
        "data",
        "What was the revenue for June 2026, as reported by the shop's monthly revenue report? Give the amount in shekels.",
        revenue(2026, 6),
    ),
    Task(
        "vat-bug",
        "bug",
        BUG_PROMPT,
        fixed,
        [
            (
                "src/shop/pricing.py",
                "int(amount * (1 + VAT_RATE) + 0.5)",
                "int(amount * (1 + VAT_RATE))",
            )
        ],
    ),
    Task(
        "reorder-bug",
        "bug",
        BUG_PROMPT,
        fixed,
        [("src/shop/inventory.py", "return stock <= reorder_at", "return stock < reorder_at")],
    ),
    Task(
        "gift-wrap-flag",
        "flag",
        flag_prompt("gift_wrap", False, "Offer gift wrapping at checkout"),
        flag("gift_wrap", False, "Offer gift wrapping at checkout"),
    ),
    Task(
        "dark-mode-flag",
        "flag",
        flag_prompt("dark_mode", True, "Dark theme for the storefront"),
        flag("dark_mode", True, "Dark theme for the storefront"),
    ),
]

TEST = [
    Task(
        "vip-shipped",
        "data",
        "How many shipped orders used the VIP20 coupon? Answer with the number.",
        answer(
            "SELECT COUNT(*) FROM orders WHERE status = 'shipped' AND coupon = 'VIP20'",
            lambda rows: [str(rows[0][0])],
        ),
    ),
    Task(
        "refund-leaders",
        "data",
        "Which customers have the most refunded orders, and how many each? Name every customer tied for the most.",
        answer(
            "SELECT customers.name, COUNT(*) AS refunds FROM orders JOIN customers ON customers.id = orders.customer_id"
            " WHERE status = 'refunded' GROUP BY customers.id ORDER BY refunds DESC",
            lambda rows: [name for name, count in rows if count == rows[0][1]] + [str(rows[0][1])],
        ),
    ),
    Task(
        "august-revenue",
        "data",
        "What was the revenue for August 2026, as reported by the shop's monthly revenue report? Give the amount in shekels.",
        revenue(2026, 8),
    ),
    Task(
        "shipping-bug",
        "bug",
        BUG_PROMPT,
        fixed,
        [
            (
                "src/shop/pricing.py",
                "0 if amount >= FREE_SHIPPING_FROM_CENTS",
                "0 if amount > FREE_SHIPPING_FROM_CENTS",
            )
        ],
    ),
    Task(
        "ranking-bug",
        "bug",
        BUG_PROMPT,
        fixed,
        [
            (
                "src/shop/reports.py",
                "ORDER BY orders DESC, customers.id",
                "ORDER BY orders, customers.id",
            )
        ],
    ),
    Task(
        "saved-carts-flag",
        "flag",
        flag_prompt("saved_carts", False, "Keep carts between visits"),
        flag("saved_carts", False, "Keep carts between visits"),
    ),
    Task(
        "order-notes-flag",
        "flag",
        flag_prompt("order_notes", True, "Let customers add a note to an order"),
        flag("order_notes", True, "Let customers add a note to an order"),
    ),
]
