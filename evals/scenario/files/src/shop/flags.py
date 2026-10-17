"""Feature flags. Mirrors config/flags.json and docs/FLAGS.md — keep all three in step."""

EXPRESS_CHECKOUT = True
LOYALTY_POINTS = False
NEW_SEARCH = False

ALL = {
    "express_checkout": EXPRESS_CHECKOUT,
    "loyalty_points": LOYALTY_POINTS,
    "new_search": NEW_SEARCH,
}
