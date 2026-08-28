import unittest
from unittest.mock import Mock

from stockcast.autotrade import AutoTradeConfig, AutoTrader
from stockcast.config import Settings


class AutoTraderTests(unittest.TestCase):
    def test_buys_uptrend_in_paper_account(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {"output1": [], "output2": []}
        client.daily_prices.return_value = list(range(1, 31))
        trader = AutoTrader(client)
        trader._config = AutoTradeConfig(("005930",), auto_discover=False, quantity=2)

        trader.run_once()

        order = client.order.call_args.args[0]
        self.assertEqual((order.symbol, order.side, order.quantity), ("005930", "buy", 2))

    def test_sells_at_stop_loss(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [{"pdno": "005930", "hldg_qty": "3", "pchs_avg_pric": "200"}],
            "output2": [],
        }
        client.daily_prices.return_value = [100] * 30
        trader = AutoTrader(client)
        trader._config = AutoTradeConfig(("005930",), auto_discover=False, quantity=2, stop_loss_pct=3)

        trader.run_once()

        order = client.order.call_args.args[0]
        self.assertEqual((order.side, order.quantity), ("sell", 2))

    def test_live_account_cannot_start(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="live")
        trader = AutoTrader(client)

        with self.assertRaisesRegex(ValueError, "모의투자"):
            trader.start(AutoTradeConfig(("005930",)))

    def test_discovers_and_selects_uptrend_from_volume_rank(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {"output1": [], "output2": []}
        client.volume_rank.return_value = [
            {"mksc_shrn_iscd": "111111"},
            {"mksc_shrn_iscd": "222222"},
        ]
        client.daily_prices.side_effect = lambda symbol: (
            list(range(1, 31)) if symbol == "111111" else list(range(30, 0, -1))
        )
        trader = AutoTrader(client)
        trader._config = AutoTradeConfig(auto_discover=True, scan_limit=2, select_count=1)

        trader.run_once()

        order = client.order.call_args.args[0]
        self.assertEqual(order.symbol, "111111")
        self.assertTrue(any("111111" in event["message"] for event in trader.status()["events"]))


if __name__ == "__main__":
    unittest.main()
