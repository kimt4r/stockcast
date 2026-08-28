import unittest

from stockcast.config import Settings
from stockcast.kis import KISClient, OrderRequest


def settings(**changes):
    values = dict(
        app_key="key", app_secret="secret", account_no="12345678",
        environment="paper", allowed_symbols=frozenset({"005930"}),
        max_order_krw=100_000,
    )
    values.update(changes)
    return Settings(**values)


class RiskTests(unittest.TestCase):
    def test_rejects_symbol_outside_allowlist(self):
        with self.assertRaisesRegex(ValueError, "허용되지 않은"):
            KISClient(settings())._validate_order(OrderRequest("000660", "buy", 1, 50_000, "limit"))

    def test_rejects_order_above_limit(self):
        with self.assertRaisesRegex(ValueError, "초과"):
            KISClient(settings())._validate_order(OrderRequest("005930", "buy", 2, 60_000, "limit"))

    def test_live_trading_is_locked(self):
        with self.assertRaisesRegex(ValueError, "잠겨"):
            KISClient(settings(environment="live"))._validate_order(
                OrderRequest("005930", "sell", 1, 50_000, "limit")
            )


if __name__ == "__main__":
    unittest.main()
