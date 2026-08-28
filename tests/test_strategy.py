import unittest

from stockcast.strategy import Signal, SmaCrossStrategy


class SmaCrossStrategyTests(unittest.TestCase):
    def test_buy_on_upward_cross(self):
        strategy = SmaCrossStrategy(short_window=2, long_window=3)
        self.assertEqual(strategy.evaluate([5, 5, 5, 4, 7]), Signal.BUY)

    def test_sell_on_downward_cross(self):
        strategy = SmaCrossStrategy(short_window=2, long_window=3)
        self.assertEqual(strategy.evaluate([5, 5, 5, 6, 3]), Signal.SELL)

    def test_hold_when_history_is_short(self):
        self.assertEqual(SmaCrossStrategy().evaluate([1, 2, 3]), Signal.HOLD)


if __name__ == "__main__":
    unittest.main()
