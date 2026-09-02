import unittest
from unittest.mock import Mock, patch
import json
from datetime import datetime
from pathlib import Path
import tempfile

from stockcast.autotrade import KST, AutoTradeConfig, AutoTrader
from stockcast.config import Settings


def intraday_bars(prices):
    volumes = [100] * len(prices)
    if len(volumes) >= 2:
        volumes[-2] = 200
    return [
        {"time": f"{index:06d}", "price": price, "volume": volumes[index]}
        for index, price in enumerate(prices)
    ]


class AutoTraderTests(unittest.TestCase):
    def setUp(self):
        self._report_temp = tempfile.TemporaryDirectory()
        self.report_dir = Path(self._report_temp.name)

    def tearDown(self):
        self._report_temp.cleanup()

    def test_buys_uptrend_in_paper_account(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {"output1": [], "output2": [{"tot_evlu_amt": "1000"}]}
        client.intraday_bars.return_value = intraday_bars(list(range(1, 31)))
        client.order.return_value = {"output": {"ODNO": "12345"}}
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(
            ("005930",), auto_discover=False, position_size_pct=6,
        )

        with patch.object(trader, "_entry_is_open", return_value=True):
            trader.run_once()

        order = client.order.call_args.args[0]
        self.assertEqual((order.symbol, order.side, order.quantity), ("005930", "buy", 2))
        self.assertEqual(trader._daily_orders[-1]["order_id"], "12345")

    def test_sells_at_stop_loss(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [{"pdno": "005930", "hldg_qty": "3", "pchs_avg_pric": "200"}],
            "output2": [],
        }
        client.intraday_bars.return_value = intraday_bars([200] * 20 + [100])
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(
            ("005930",), auto_discover=False, position_size_pct=10, stop_loss_pct=3,
        )
        trader._managed_positions.add("005930")

        trader.run_once()

        order = client.order.call_args.args[0]
        self.assertEqual((order.side, order.quantity), ("sell", 3))

    def test_live_account_cannot_start(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="live")
        trader = AutoTrader(client, self.report_dir)

        with self.assertRaisesRegex(ValueError, "모의투자"):
            trader.start(AutoTradeConfig(("005930",)))

    def test_discovers_and_selects_uptrend_from_volume_rank(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [], "output2": [{"tot_evlu_amt": "1000000"}],
        }
        client.daytrade_rank.return_value = [
            {"mksc_shrn_iscd": "111111"},
            {"mksc_shrn_iscd": "222222"},
        ]
        client.intraday_bars.side_effect = lambda symbol: intraday_bars(
            list(range(1, 31)) if symbol == "111111" else list(range(31, 1, -1))
        )
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(auto_discover=True, scan_limit=2, select_count=1)

        with patch.object(trader, "_entry_is_open", return_value=True):
            trader.run_once()

        order = client.order.call_args.args[0]
        self.assertEqual(order.symbol, "111111")
        self.assertTrue(any("111111" in event["message"] for event in trader.status()["events"]))

    def test_force_exit_sells_all_bot_managed_quantity(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [{"pdno": "005930", "hldg_qty": "4", "pchs_avg_pric": "100"}],
            "output2": [],
        }
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(("005930",), auto_discover=False)
        trader._managed_positions.add("005930")

        trader.run_once(force_exit=True)

        order = client.order.call_args.args[0]
        self.assertEqual((order.side, order.quantity), ("sell", 4))
        self.assertNotIn("005930", trader._managed_positions)

    def test_daily_target_blocks_new_buy(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [], "output2": [{"tot_evlu_amt": "1100000"}],
        }
        client.intraday_bars.return_value = intraday_bars(list(range(1, 31)))
        with tempfile.TemporaryDirectory() as directory:
            trader = AutoTrader(client, Path(directory))
            trader._config = AutoTradeConfig(
                ("005930",), auto_discover=False, position_size_pct=10,
            )
            trader._trading_date = datetime.now(KST).date().isoformat()
            trader._starting_equity = 1_000_000

            with patch.object(trader, "_entry_is_open", return_value=True):
                trader.run_once()

            client.order.assert_not_called()
            self.assertTrue(trader.status()["daily_performance"]["target_reached"])

    def test_position_size_uses_equity_and_only_reduces_risk(self):
        client = Mock()
        client.settings = Settings(
            "key", "secret", "12345678", environment="paper", max_order_krw=1_000_000,
        )
        trader = AutoTrader(client, self.report_dir)
        trader._latest_equity = 1_000_000
        config = AutoTradeConfig(position_size_pct=10, daily_target_pct=10)

        self.assertEqual(trader._order_quantity(config, 10_000, None), 10)
        self.assertEqual(trader._order_quantity(config, 10_000, 2), 10)
        self.assertEqual(trader._order_quantity(config, 10_000, -0.1), 5)
        self.assertEqual(trader._order_quantity(config, 10_000, 5), 5)

    def test_writes_daily_report_from_account_equity(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [], "output2": [{"tot_evlu_amt": "1000000"}],
        }
        client.intraday_bars.return_value = intraday_bars([100] * 30)

        with tempfile.TemporaryDirectory() as directory:
            trader = AutoTrader(client, Path(directory))
            trader._config = AutoTradeConfig(("005930",), auto_discover=False)
            trader.run_once()
            report_path = Path(trader.status()["daily_performance"]["report_path"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            journal_path = Path(trader.status()["daily_performance"]["journal_path"])
            journal_text = journal_path.read_text(encoding="utf-8")

        self.assertEqual(report["starting_equity"], 1_000_000)
        self.assertEqual(report["return_pct"], 0.0)
        self.assertEqual(report["target_pct"], 10.0)
        self.assertIn("총평가금액", report["measurement"])
        self.assertIn("journal", report)
        self.assertEqual(report["telemetry"]["run_count"], 1)
        self.assertIn("## 문제점", journal_text)
        self.assertIn("주문이 한 건도 없었습니다", journal_text)

    def test_execution_analysis_matches_strategy_order_number(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        trader = AutoTrader(client, self.report_dir)
        trader._daily_orders = [
            {"order_id": "100", "symbol": "005930", "side": "buy", "quantity": 2},
            {"order_id": "101", "symbol": "005930", "side": "sell", "quantity": 2},
        ]
        trader._executions = [
            {
                "ord_dt": "20260902", "ord_tmd": "100000", "odno": "100",
                "pdno": "005930", "prdt_name": "삼성전자",
                "sll_buy_dvsn_cd_name": "매수", "tot_ccld_qty": "2",
                "avg_prvs": "70000", "rmn_qty": "0",
            },
            {
                "ord_dt": "20260902", "ord_tmd": "110000", "odno": "101",
                "pdno": "005930", "prdt_name": "삼성전자",
                "sll_buy_dvsn_cd_name": "매도", "tot_ccld_qty": "2",
                "avg_prvs": "71000", "rmn_qty": "0",
            },
            {
                "ord_dt": "20260902", "ord_tmd": "120000", "odno": "manual",
                "pdno": "000660", "sll_buy_dvsn_cd_name": "매수",
                "tot_ccld_qty": "1", "avg_prvs": "100000", "rmn_qty": "0",
            },
        ]

        analysis = trader._execution_analysis()

        self.assertEqual(analysis["account_execution_rows"], 3)
        self.assertEqual(analysis["strategy_execution_rows"], 2)
        self.assertEqual(analysis["unconfirmed_order_count"], 0)
        self.assertEqual(analysis["fill_rate_pct"], 100.0)
        self.assertEqual(analysis["gross_realized_pnl_krw"], 2_000)
        self.assertEqual(analysis["wins"], 1)

    def test_no_trade_report_records_precise_decision_blocks(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [], "output2": [{"tot_evlu_amt": "1000000"}],
        }
        client.intraday_bars.return_value = intraday_bars([100] * 30)
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(("005930",), auto_discover=False)

        with patch.object(trader, "_entry_is_open", return_value=False):
            trader.run_once()

        report_path = Path(trader.status()["daily_performance"]["report_path"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertGreater(report["telemetry"]["decision_counts"]["outside_entry_window"], 0)
        self.assertGreater(report["telemetry"]["decision_counts"]["no_buy_signal"], 0)

    def test_reentry_is_blocked_during_cooldown_and_after_daily_limit(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        trader = AutoTrader(client, self.report_dir)
        config = AutoTradeConfig(reentry_cooldown_minutes=30, max_entries_per_symbol=2)
        trader._last_exits["005930"] = datetime.now(KST)

        self.assertFalse(trader._can_reenter("005930", config))
        trader._last_exits.clear()
        trader._entry_counts["005930"] = 2
        self.assertFalse(trader._can_reenter("005930", config))

    def test_profit_lock_activates_after_peak_giveback(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(
            profit_lock_activation_pct=1, profit_giveback_pct=0.5,
        )
        trader._starting_equity = 1_000_000
        trader._latest_equity = 1_006_000
        trader._peak_return_pct = 1.2

        self.assertTrue(trader._profit_lock_active())

    def test_restores_daily_limits_and_open_positions_from_report(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        today = datetime.now(KST).date().isoformat()
        report = {
            "date": today,
            "starting_equity": 1_000_000,
            "latest_equity": 1_001_000,
            "peak_return_pct": 0.2,
            "orders": [
                {"time": f"{today}T10:00:00+09:00", "symbol": "005930", "side": "buy"},
                {"time": f"{today}T10:10:00+09:00", "symbol": "005930", "side": "sell"},
                {"time": f"{today}T11:00:00+09:00", "symbol": "000660", "side": "buy"},
            ],
            "executions": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory)
            (report_dir / f"{today}.json").write_text(
                json.dumps(report), encoding="utf-8",
            )
            trader = AutoTrader(client, report_dir)
            trader._restore_daily_report()

        self.assertEqual(trader._closed_trades, 1)
        self.assertEqual(trader._entry_counts, {"005930": 1, "000660": 1})
        self.assertEqual(trader._managed_positions, {"000660"})

    def test_bar_metrics_calculate_vwap_and_relative_volume(self):
        prices = list(range(100, 130))
        closes, vwap, relative_volume = AutoTrader._bar_metrics(intraday_bars(prices))

        self.assertEqual(closes[-1], 129)
        self.assertLess(vwap, closes[-1])
        self.assertEqual(relative_volume, 2.0)

    def test_trailing_stop_records_exit_reason(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [{"pdno": "005930", "hldg_qty": "3", "pchs_avg_pric": "100"}],
            "output2": [{"tot_evlu_amt": "1000000"}],
        }
        client.intraday_bars.return_value = intraday_bars([100] * 20 + [102])
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(("005930",), auto_discover=False)
        trader._managed_positions.add("005930")
        trader._trading_date = datetime.now(KST).date().isoformat()
        trader._starting_equity = 1_000_000
        trader._position_peaks["005930"] = 103

        trader.run_once()

        order = client.order.call_args.args[0]
        self.assertEqual((order.side, order.quantity), ("sell", 3))
        self.assertEqual(trader._daily_orders[-1]["reason"], "trailing_stop")


if __name__ == "__main__":
    unittest.main()
