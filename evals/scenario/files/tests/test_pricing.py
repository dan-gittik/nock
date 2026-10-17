import unittest

from shop import pricing


class Subtotal(unittest.TestCase):
    def test_sums_quantity_times_price(self):
        for items, expected in [
            ([], 0),
            ([(1, 100)], 100),
            ([(2, 150), (3, 10)], 330),
            ([(10, 999)], 9990),
        ]:
            with self.subTest(items=items):
                self.assertEqual(pricing.subtotal(items), expected)


class Discount(unittest.TestCase):
    def test_no_coupon(self):
        self.assertEqual(pricing.discount(1000, None), 1000)

    def test_known_coupons(self):
        self.assertEqual(pricing.discount(1000, "SPRING10"), 900)
        self.assertEqual(pricing.discount(1000, "VIP20"), 800)

    def test_rounds_down(self):
        self.assertEqual(pricing.discount(999, "SPRING10"), 899)

    def test_unknown_coupon_is_ignored(self):
        self.assertEqual(pricing.discount(1000, "BOGUS"), 1000)


class Vat(unittest.TestCase):
    def test_adds_seventeen_percent(self):
        for amount, expected in [(0, 0), (100, 117), (1000, 1170), (3, 4), (2, 2)]:
            with self.subTest(amount=amount):
                self.assertEqual(pricing.with_vat(amount), expected)


class Shipping(unittest.TestCase):
    def test_charged_below_threshold(self):
        self.assertEqual(pricing.shipping(19999), 2500)

    def test_free_from_threshold(self):
        self.assertEqual(pricing.shipping(20000), 0)
        self.assertEqual(pricing.shipping(50000), 0)


class Total(unittest.TestCase):
    def test_discount_then_vat_then_shipping(self):
        self.assertEqual(pricing.total([(1, 10000)], "SPRING10"), 10530 + 2500)

    def test_free_shipping_is_judged_after_discount(self):
        self.assertEqual(pricing.total([(1, 21000)], "VIP20"), 19656 + 2500)

    def test_large_order_ships_free(self):
        self.assertEqual(pricing.total([(3, 10000)]), 35100)

    def test_legacy_total_still_works(self):
        self.assertEqual(pricing.legacy_total([(1, 100)]), 117 + 2500)
