"""Prices: an order's total with coupons, VAT and shipping."""

import logging
import warnings

log = logging.getLogger("shop.pricing")

VAT_RATE = 0.17
FREE_SHIPPING_FROM_CENTS = 20000
SHIPPING_CENTS = 2500
COUPONS = {"SPRING10": 0.10, "VIP20": 0.20}


def subtotal(items: list[tuple[int, int]]) -> int:
    """Sum of quantity × unit price, in cents."""
    return sum(quantity * unit_price for quantity, unit_price in items)


def discount(amount: int, coupon: str | None) -> int:
    """The amount after the coupon's percentage off, rounded down to a cent."""
    if coupon is None:
        return amount
    if coupon not in COUPONS:
        log.warning("unknown coupon %s ignored", coupon)
        return amount
    return int(amount * (1 - COUPONS[coupon]))


def with_vat(amount: int) -> int:
    """The amount with VAT, rounded half up to a cent."""
    return int(amount * (1 + VAT_RATE) + 0.5)


def shipping(amount: int) -> int:
    """Shipping is free from FREE_SHIPPING_FROM_CENTS (after discount, before VAT)."""
    return 0 if amount >= FREE_SHIPPING_FROM_CENTS else SHIPPING_CENTS


def total(items: list[tuple[int, int]], coupon: str | None = None) -> int:
    """What the customer pays: discount first, then VAT, then shipping."""
    discounted = discount(subtotal(items), coupon)
    return with_vat(discounted) + shipping(discounted)


def legacy_total(items: list[tuple[int, int]]) -> int:
    warnings.warn("legacy_total is deprecated; use total", DeprecationWarning, stacklevel=2)
    return total(items)
