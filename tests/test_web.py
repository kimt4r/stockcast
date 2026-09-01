import unittest
from unittest.mock import Mock
from unittest.mock import patch
from pathlib import Path
import tempfile

from stockcast.config import Settings
from stockcast.web import UserContext, create_app


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(testing=True)
        self.client = self.app.test_client()

    def test_login_page_is_default(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("API 연결".encode(), response.data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_dashboard_api_requires_connection(self):
        response = self.client.get("/api/balance")
        self.assertEqual(response.status_code, 401)
        self.assertIn("API 연결", response.get_json()["error"])

    def test_invalid_environment_is_rejected_before_network(self):
        response = self.client.post("/connect", data={"environment": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("투자 환경", response.get_data(as_text=True))

    def test_connect_loads_credentials_for_selected_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.paper").write_text(
                "KIS_APP_KEY=key\nKIS_APP_SECRET=secret\n"
                "KIS_ACCOUNT_NO=12345678\nKIS_ACCOUNT_PRODUCT_CODE=01\n"
                "KIS_ENV=paper\n",
                encoding="utf-8",
            )
            with (
                patch("stockcast.web.Path.cwd", return_value=root),
                patch("stockcast.web.KISClient.authenticate", return_value="token"),
            ):
                response = self.client.post("/connect", data={"environment": "paper"})

        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as browser_session:
            context = self.app.extensions["stockcast_vault"].get(browser_session["sid"])
        self.assertEqual(context.settings.environment, "paper")
        self.assertEqual(context.settings.account_no, "12345678")

    def test_quote_reuses_daily_prices_for_signal(self):
        kis = Mock()
        kis.current_price.return_value = 71_200
        kis.daily_prices.return_value = list(range(1, 31))
        context = UserContext(Settings("key", "secret", "12345678"), kis, "csrf")
        self.app.extensions["stockcast_vault"].put("test-session", context)
        with self.client.session_transaction() as browser_session:
            browser_session["sid"] = "test-session"

        response = self.client.get("/api/quote/005930")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["price"], 71_200)
        self.assertIn(response.get_json()["signal"], {"BUY", "SELL", "HOLD"})
        kis.daily_prices.assert_called_once_with("005930")

    def test_dashboard_contains_expandable_strategy_rules(self):
        kis = Mock()
        context = UserContext(Settings("key", "secret", "12345678"), kis, "csrf")
        self.app.extensions["stockcast_vault"].put("test-session", context)
        with self.client.session_transaction() as browser_session:
            browser_session["sid"] = "test-session"

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("현재 매매기준 보기".encode(), response.data)
        self.assertIn(b'<details class="strategy-details">', response.data)

    def test_discovery_returns_name_symbol_and_price(self):
        kis = Mock()
        kis.daytrade_rank.return_value = [{
            "hts_kor_isnm": "삼성전자",
            "mksc_shrn_iscd": "005930",
            "stck_prpr": "71200",
            "prdy_ctrt": "1.25",
        }]
        context = UserContext(Settings("key", "secret", "12345678"), kis, "csrf")
        self.app.extensions["stockcast_vault"].put("test-session", context)
        with self.client.session_transaction() as browser_session:
            browser_session["sid"] = "test-session"

        response = self.client.get("/api/autotrade/discover?limit=7")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"][0], {
            "name": "삼성전자", "symbol": "005930", "price": 71200, "change_rate": 1.25,
        })
        kis.daytrade_rank.assert_called_once_with(limit=7)

    def test_logout_clears_connection(self):
        kis = Mock()
        context = UserContext(Settings("key", "secret", "12345678"), kis, "csrf")
        self.app.extensions["stockcast_vault"].put("test-session", context)
        with self.client.session_transaction() as browser_session:
            browser_session["sid"] = "test-session"

        response = self.client.post("/logout", headers={"X-CSRF-Token": "csrf"})

        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("sid", browser_session)

    def test_safety_settings_update_context_and_environment_file(self):
        kis = Mock()
        context = UserContext(Settings("key", "secret", "12345678"), kis, "csrf")
        self.app.extensions["stockcast_vault"].put("test-session", context)
        with self.client.session_transaction() as browser_session:
            browser_session["sid"] = "test-session"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment_file = root / ".env.paper"
            environment_file.write_text("KIS_APP_KEY=keep-secret\n", encoding="utf-8")
            with patch("stockcast.web.Path.cwd", return_value=root):
                response = self.client.post(
                    "/api/settings",
                    json={"allowed_symbols": "005930,000660", "max_order_krw": 250000},
                    headers={"X-CSRF-Token": "csrf"},
                )
            saved = environment_file.read_text(encoding="utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(context.settings.max_order_krw, 250_000)
        self.assertEqual(context.settings.allowed_symbols, frozenset({"005930", "000660"}))
        self.assertIn("KIS_APP_KEY=keep-secret", saved)
        self.assertIn("STOCKCAST_MAX_ORDER_KRW=250000", saved)


if __name__ == "__main__":
    unittest.main()
