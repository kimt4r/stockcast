from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import os
import secrets
import threading
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
import requests

from .config import Settings, is_domestic_symbol, update_dotenv
from .autotrade import AutoTradeConfig, AutoTrader
from .kis import KISAPIError, KISClient, OrderRequest
from .strategy import SmaCrossStrategy


@dataclass
class UserContext:
    settings: Settings
    client: KISClient
    csrf_token: str
    trader: AutoTrader | None = None


class MemoryVault:
    """Process-local credential vault. Credentials never enter cookies or disk."""

    def __init__(self) -> None:
        self._items: dict[str, UserContext] = {}
        self._lock = threading.Lock()

    def put(self, key: str, value: UserContext) -> None:
        with self._lock:
            self._items[key] = value

    def get(self, key: str | None) -> UserContext | None:
        if not key:
            return None
        with self._lock:
            return self._items.get(key)

    def pop(self, key: str | None) -> None:
        if not key:
            return
        with self._lock:
            self._items.pop(key, None)


def create_app(*, testing: bool = False) -> Flask:
    template_dir = Path(__file__).with_name("templates")
    static_dir = Path(__file__).with_name("static")
    app = Flask(__name__, template_folder=str(template_dir), static_folder=str(static_dir))
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=False,  # localhost HTTP
        MAX_CONTENT_LENGTH=16 * 1024,
        TESTING=testing,
    )
    vault = MemoryVault()
    app.extensions["stockcast_vault"] = vault

    def context() -> UserContext | None:
        return vault.get(session.get("sid"))

    def require_context() -> UserContext:
        current = context()
        if current is None:
            raise PermissionError("API 연결이 필요합니다.")
        return current

    def require_csrf(current: UserContext) -> None:
        if request.headers.get("X-CSRF-Token") != current.csrf_token:
            raise PermissionError("요청 검증에 실패했습니다. 화면을 새로고침하세요.")

    def establish_connection(settings: Settings) -> None:
        settings.validate()
        client = KISClient(settings)
        client.authenticate()
        vault.pop(session.get("sid"))
        sid = secrets.token_urlsafe(32)
        session.clear()
        session["sid"] = sid
        vault.put(sid, UserContext(settings, client, secrets.token_urlsafe(32), AutoTrader(client)))

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/")
    def index():
        current = context()
        if current is None:
            return render_template("login.html")
        return render_template(
            "dashboard.html",
            environment=current.settings.environment,
            account_mask=f"{current.settings.account_no[:4]}••••-{current.settings.product_code}",
            allowed_symbols=sorted(current.settings.allowed_symbols),
            max_order_krw=current.settings.max_order_krw,
            allow_live=current.settings.allow_live,
            csrf_token=current.csrf_token,
        )

    @app.post("/connect")
    def connect():
        environment = request.form.get("environment", "paper")
        if environment not in {"paper", "live"}:
            return render_template("login.html", error="투자 환경을 확인해주세요."), 400
        try:
            credentials_file = Path.cwd() / f".env.{environment}"
            if not credentials_file.exists():
                raise ValueError(f"{credentials_file.name} 파일이 없습니다.")
            settings = Settings.from_env(credentials_file)
            if settings.environment != environment:
                raise ValueError(
                    f"{credentials_file.name}의 KIS_ENV 값은 {environment}여야 합니다."
                )
            establish_connection(settings)
            return redirect(url_for("index"))
        except (ValueError, KISAPIError, requests.RequestException) as exc:
            return render_template("login.html", error=str(exc)), 400

    @app.post("/logout")
    def logout():
        current = require_context()
        require_csrf(current)
        report_version = None
        if current.trader:
            report_version = current.trader.disconnect()
        vault.pop(session.get("sid"))
        session.clear()
        return jsonify({"ok": True, "report_version": report_version})

    @app.get("/api/autotrade")
    def autotrade_status():
        current = require_context()
        if current.trader is None:
            raise ValueError("자동매매 엔진을 사용할 수 없습니다.")
        return jsonify(current.trader.status())

    @app.get("/api/autotrade/discover")
    def autotrade_discover():
        current = require_context()
        if current.settings.environment != "paper":
            raise ValueError("종목 자동검색은 모의투자에서만 사용할 수 있습니다.")
        limit = min(20, max(1, request.args.get("limit", 10, type=int)))
        rows = current.client.daytrade_rank(limit=limit)
        results = []
        for row in rows:
            symbol = str(row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or "")
            if not is_domestic_symbol(symbol):
                continue
            results.append({
                "name": str(row.get("hts_kor_isnm") or "종목명 없음"),
                "symbol": symbol,
                "price": int(row.get("stck_prpr") or 0),
                "change_rate": float(row.get("prdy_ctrt") or 0),
            })
        return jsonify({"items": results})

    @app.post("/api/autotrade/start")
    def autotrade_start():
        current = require_context()
        require_csrf(current)
        if current.settings.environment != "paper":
            raise ValueError("자동매매는 모의투자 계좌에서만 시작할 수 있습니다.")
        data: dict[str, Any] = request.get_json(silent=True) or {}
        symbols = tuple(dict.fromkeys(
            item.strip().upper() for item in str(data.get("symbols", "")).split(",") if item.strip()
        ))
        if current.settings.allowed_symbols and any(
            symbol not in current.settings.allowed_symbols for symbol in symbols
        ):
            raise ValueError("자동매매 후보는 주문 허용 종목에 포함되어야 합니다.")
        config = AutoTradeConfig(
            symbols=symbols,
            auto_discover=data.get("auto_discover") is True,
            scan_limit=int(data.get("scan_limit", 10)),
            select_count=int(data.get("select_count", 3)),
            position_size_pct=float(data.get("position_size_pct", 10)),
            interval_seconds=int(data.get("interval_seconds", 60)),
            max_positions=int(data.get("max_positions", 3)),
            signal_bar_minutes=int(data.get("signal_bar_minutes", 3)),
            swing_lookback=int(data.get("swing_lookback", 2)),
            ema_period=int(data.get("ema_period", 7)),
            atr_period=int(data.get("atr_period", 5)),
            chop_lookback=int(data.get("chop_lookback", 4)),
            min_range_atr=float(data.get("min_range_atr", 2)),
            use_ema_filter=data.get("use_ema_filter") is not False,
            use_chop_filter=data.get("use_chop_filter") is not False,
            min_structure_age_bars=int(data.get("min_structure_age_bars", 1)),
            max_structure_age_bars=int(data.get("max_structure_age_bars", 5)),
            min_anchor_distance_atr=float(data.get("min_anchor_distance_atr", 0.2)),
            max_anchor_distance_atr=float(data.get("max_anchor_distance_atr", 1.5)),
            min_ema_slope_atr=float(data.get("min_ema_slope_atr", 0)),
            catastrophe_atr_multiple=float(data.get("catastrophe_atr_multiple", 4)),
            daily_loss_limit_pct=float(data.get("daily_loss_limit_pct", 3)),
            reentry_cooldown_minutes=int(data.get("reentry_cooldown_minutes", 30)),
            max_entries_per_symbol=int(data.get("max_entries_per_symbol", 2)),
            max_daily_round_trips=int(data.get("max_daily_round_trips", 20)),
            profit_lock_activation_pct=float(data.get("profit_lock_activation_pct", 1)),
            profit_giveback_pct=float(data.get("profit_giveback_pct", 0.5)),
            profit_protection_activation_pct=float(
                data.get("profit_protection_activation_pct", 1)
            ),
            profit_protection_activation_atr=float(
                data.get("profit_protection_activation_atr", 2)
            ),
            profit_trailing_atr=float(data.get("profit_trailing_atr", 1.5)),
            profit_trailing_min_pct=float(data.get("profit_trailing_min_pct", 0.5)),
            profit_floor_pct=float(data.get("profit_floor_pct", 0.2)),
            profit_exit_confirmation_bars=int(
                data.get("profit_exit_confirmation_bars", 2)
            ),
        )
        if current.trader is None:
            current.trader = AutoTrader(current.client)
        current.trader.start(config)
        return jsonify(current.trader.status())

    @app.post("/api/autotrade/stop")
    def autotrade_stop():
        current = require_context()
        require_csrf(current)
        if current.trader:
            current.trader.stop()
            return jsonify(current.trader.status())
        return jsonify({"running": False, "events": []})

    @app.post("/api/settings")
    def update_settings():
        current = require_context()
        require_csrf(current)
        data: dict[str, Any] = request.get_json(silent=True) or {}
        symbols = frozenset(
            item.strip().upper() for item in str(data.get("allowed_symbols", "")).split(",")
            if item.strip()
        )
        if any(not is_domestic_symbol(item) for item in symbols):
            raise ValueError("허용 종목은 쉼표로 구분한 6자리 영문·숫자 종목코드여야 합니다.")
        updated = replace(
            current.settings,
            allowed_symbols=symbols,
            max_order_krw=int(data.get("max_order_krw", 0)),
            allow_live=(
                data.get("allow_live") is True
                if current.settings.environment == "live"
                else False
            ),
        )
        updated.validate()
        current.settings = updated
        current.client.settings = updated
        update_dotenv(
            Path.cwd() / f".env.{updated.environment}",
            {
                "STOCKCAST_ALLOWED_SYMBOLS": ",".join(sorted(symbols)),
                "STOCKCAST_MAX_ORDER_KRW": str(updated.max_order_krw),
                "STOCKCAST_ALLOW_LIVE": str(updated.allow_live).lower(),
            },
        )
        return jsonify({"ok": True})

    @app.get("/api/quote/<symbol>")
    def quote(symbol: str):
        current = require_context()
        price = current.client.current_price(symbol)
        closes = current.client.daily_prices(symbol)
        signal = SmaCrossStrategy(5, 20).evaluate(closes)
        return jsonify({
            "symbol": symbol,
            "price": price,
            "closes": closes[-30:],
            "signal": signal.value,
            "short": 5,
            "long": 20,
        })

    @app.get("/api/balance")
    def balance():
        return jsonify(require_context().client.balance())

    @app.get("/api/signal/<symbol>")
    def signal(symbol: str):
        current = require_context()
        short = min(20, max(2, request.args.get("short", 5, type=int)))
        long = min(60, max(short + 1, request.args.get("long", 20, type=int)))
        closes = current.client.daily_prices(symbol)
        result = SmaCrossStrategy(short, long).evaluate(closes)
        return jsonify({"symbol": symbol, "signal": result.value, "short": short, "long": long})

    @app.post("/api/order")
    def order():
        current = require_context()
        require_csrf(current)
        data: dict[str, Any] = request.get_json(silent=True) or {}
        execute = data.get("execute") is True
        order_request = OrderRequest(
            symbol=str(data.get("symbol", "")),
            side=str(data.get("side", "")),
            quantity=int(data.get("quantity", 0)),
            price=int(data.get("price", 0)),
            order_type="limit" if int(data.get("price", 0)) else "market",
        )
        current.client._validate_order(order_request)
        if not execute:
            return jsonify({"dry_run": True, "message": "주문 조건 검증 완료"})
        if current.settings.environment == "live" and data.get("live_confirm") != "LIVE":
            raise ValueError("실전 주문 확인 문구가 올바르지 않습니다.")
        return jsonify(current.client.order(order_request))

    @app.errorhandler(PermissionError)
    def permission_error(exc):
        return jsonify({"error": str(exc)}), 401

    @app.errorhandler(ValueError)
    @app.errorhandler(KISAPIError)
    @app.errorhandler(requests.RequestException)
    def request_error(exc):
        return jsonify({"error": str(exc)}), 400

    return app


def main() -> None:
    # Stockcast loads .env itself; suppress Flask's optional python-dotenv tip.
    os.environ.setdefault("FLASK_SKIP_DOTENV", "1")
    create_app().run(host="127.0.0.1", port=8787, debug=False)


if __name__ == "__main__":
    main()
