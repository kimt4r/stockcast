from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
import threading
from typing import Any

from .kis import KISClient, OrderRequest


KST = timezone(timedelta(hours=9), name="KST")


@dataclass(frozen=True)
class AutoTradeConfig:
    symbols: tuple[str, ...] = ()
    auto_discover: bool = True
    scan_limit: int = 10
    select_count: int = 3
    quantity: int = 1
    interval_seconds: int = 120
    max_positions: int = 3
    stop_loss_pct: float = 3.0
    take_profit_pct: float = 5.0
    short_window: int = 5
    long_window: int = 20

    def validate(self) -> None:
        if not self.auto_discover and not self.symbols:
            raise ValueError("자동매매 후보 종목을 한 개 이상 입력하세요.")
        if any(len(symbol) != 6 or not symbol.isdigit() for symbol in self.symbols):
            raise ValueError("후보 종목은 6자리 국내주식 종목코드여야 합니다.")
        if self.quantity <= 0 or self.interval_seconds < 60 or self.max_positions <= 0:
            raise ValueError("수량과 최대 보유 수는 양수, 실행 주기는 60초 이상이어야 합니다.")
        if not 0 < self.stop_loss_pct <= 20 or not 0 < self.take_profit_pct <= 50:
            raise ValueError("손절은 0~20%, 익절은 0~50% 범위여야 합니다.")
        if not 1 < self.short_window < self.long_window:
            raise ValueError("이동평균은 1 < 단기 < 장기 순서여야 합니다.")
        if not 1 <= self.select_count <= self.scan_limit <= 20:
            raise ValueError("검색 수는 1~20개이며 선택 수는 검색 수 이하여야 합니다.")


class AutoTrader:
    """Paper-only, rule-based trading loop with an explicit kill switch."""

    def __init__(self, client: KISClient):
        self.client = client
        self._config: AutoTradeConfig | None = None
        self._running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._last_run = ""

    def start(self, config: AutoTradeConfig) -> None:
        if self.client.settings.environment != "paper":
            raise ValueError("자동매매 MVP는 모의투자에서만 실행할 수 있습니다.")
        config.validate()
        with self._lock:
            if self._running:
                raise ValueError("자동매매가 이미 실행 중입니다.")
            self._config = config
            self._running = True
            self._stop.clear()
            self._record("system", "자동매매 시작")
            self._thread = threading.Thread(target=self._loop, daemon=True, name="stockcast-auto")
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            was_running = self._running
            self._running = False
            self._stop.set()
            if was_running:
                self._record("system", "자동매매 중지")

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "last_run": self._last_run,
                "config": asdict(self._config) if self._config else None,
                "events": list(reversed(self._events[-50:])),
            }

    def _record(self, kind: str, message: str, **details: Any) -> None:
        self._events.append({
            "time": datetime.now(KST).isoformat(timespec="seconds"),
            "kind": kind,
            "message": message,
            **details,
        })
        del self._events[:-100]

    @staticmethod
    def _market_is_open() -> bool:
        now = datetime.now(KST)
        return now.weekday() < 5 and time(9, 0) <= now.time() <= time(15, 20)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._market_is_open():
                    self.run_once()
                else:
                    with self._lock:
                        self._last_run = datetime.now(KST).isoformat(timespec="seconds")
                        self._record("hold", "장 운영시간이 아니어서 대기합니다.")
            except Exception as exc:  # keep the kill switch/status endpoint alive
                with self._lock:
                    self._record("error", str(exc))
            config = self._config
            if not config or self._stop.wait(config.interval_seconds):
                break

    def run_once(self) -> None:
        config = self._config
        if config is None:
            raise ValueError("자동매매 설정이 없습니다.")
        balance = self.client.balance()
        holdings = {
            row.get("pdno", ""): row for row in balance.get("output1", [])
            if int(row.get("hldg_qty") or 0) > 0
        }
        position_count = len(holdings)
        closes_cache: dict[str, list[int]] = {}
        symbols = list(config.symbols)
        if config.auto_discover:
            symbols, closes_cache = self._discover(config)
            with self._lock:
                self._record("scan", f"자동 검색 결과: {', '.join(symbols) if symbols else '조건 충족 종목 없음'}")
        symbols = list(dict.fromkeys([*symbols, *holdings.keys()]))

        for symbol in symbols:
            closes = closes_cache.get(symbol) or self.client.daily_prices(symbol)
            if len(closes) < config.long_window:
                with self._lock:
                    self._record("hold", f"{symbol}: 가격 이력 부족")
                continue
            current = closes[-1]
            short_sma = sum(closes[-config.short_window:]) / config.short_window
            long_sma = sum(closes[-config.long_window:]) / config.long_window
            row = holdings.get(symbol)

            if row:
                average = float(row.get("pchs_avg_pric") or current)
                return_pct = (current - average) / average * 100 if average else 0
                should_sell = (
                    short_sma < long_sma
                    or return_pct <= -config.stop_loss_pct
                    or return_pct >= config.take_profit_pct
                )
                if should_sell:
                    quantity = min(config.quantity, int(row.get("hldg_qty") or 0))
                    self.client.order(OrderRequest(symbol, "sell", quantity))
                    with self._lock:
                        self._record("sell", f"{symbol} {quantity}주 매도", return_pct=round(return_pct, 2))
                else:
                    with self._lock:
                        self._record("hold", f"{symbol}: 보유 유지", return_pct=round(return_pct, 2))
            elif short_sma > long_sma and closes[-1] > closes[-2] and position_count < config.max_positions:
                self.client.order(OrderRequest(symbol, "buy", config.quantity))
                position_count += 1
                with self._lock:
                    self._record("buy", f"{symbol} {config.quantity}주 매수")
            else:
                with self._lock:
                    self._record("hold", f"{symbol}: 매수 조건 미충족")

        with self._lock:
            self._last_run = datetime.now(KST).isoformat(timespec="seconds")

    def _discover(self, config: AutoTradeConfig) -> tuple[list[str], dict[str, list[int]]]:
        ranked = self.client.volume_rank(limit=config.scan_limit)
        scored: list[tuple[float, str]] = []
        cache: dict[str, list[int]] = {}
        for rank, row in enumerate(ranked):
            symbol = str(row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or "")
            if len(symbol) != 6 or not symbol.isdigit():
                continue
            if self.client.settings.allowed_symbols and symbol not in self.client.settings.allowed_symbols:
                continue
            closes = self.client.daily_prices(symbol)
            cache[symbol] = closes
            if len(closes) < config.long_window or closes[-2] <= 0:
                continue
            short_sma = sum(closes[-config.short_window:]) / config.short_window
            long_sma = sum(closes[-config.long_window:]) / config.long_window
            momentum = (closes[-1] - closes[-2]) / closes[-2] * 100
            trend_gap = (short_sma - long_sma) / long_sma * 100 if long_sma else 0
            if short_sma > long_sma and momentum > 0:
                liquidity_bonus = (config.scan_limit - rank) / config.scan_limit
                scored.append((trend_gap + momentum + liquidity_bonus, symbol))
        scored.sort(reverse=True)
        return [symbol for _, symbol in scored[:config.select_count]], cache
