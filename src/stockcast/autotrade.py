from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import threading
from typing import Any

from .kis import KISClient, OrderRequest
from .strategy import Signal, TrendBreakoutStrategy


KST = timezone(timedelta(hours=9), name="KST")


@dataclass(frozen=True)
class AutoTradeConfig:
    symbols: tuple[str, ...] = ()
    auto_discover: bool = True
    scan_limit: int = 10
    select_count: int = 3
    position_size_pct: float = 10.0
    interval_seconds: int = 120
    max_positions: int = 3
    stop_loss_pct: float = 3.0
    take_profit_pct: float = 5.0
    short_window: int = 5
    long_window: int = 20
    breakout_window: int = 5
    daily_target_pct: float = 10.0
    daily_loss_limit_pct: float = 3.0
    reentry_cooldown_minutes: int = 30
    max_entries_per_symbol: int = 2
    max_daily_round_trips: int = 10
    min_breakout_pct: float = 0.2
    exit_confirmation_bars: int = 2
    profit_lock_activation_pct: float = 1.0
    profit_giveback_pct: float = 0.5
    min_relative_volume: float = 1.5
    trailing_activation_pct: float = 2.0
    trailing_drawdown_pct: float = 0.7
    breakeven_activation_pct: float = 1.0
    breakeven_floor_pct: float = 0.1

    def validate(self) -> None:
        if not self.auto_discover and not self.symbols:
            raise ValueError("자동매매 후보 종목을 한 개 이상 입력하세요.")
        if any(len(symbol) != 6 or not symbol.isdigit() for symbol in self.symbols):
            raise ValueError("후보 종목은 6자리 국내주식 종목코드여야 합니다.")
        if self.interval_seconds < 60 or self.max_positions <= 0:
            raise ValueError("최대 보유 수는 양수, 실행 주기는 60초 이상이어야 합니다.")
        if not 0 < self.position_size_pct <= 100:
            raise ValueError("종목당 투자 비중은 0~100% 범위여야 합니다.")
        if self.position_size_pct * self.max_positions > 100:
            raise ValueError("종목당 투자 비중과 최대 보유 종목 수의 곱은 100% 이하여야 합니다.")
        if not 0 < self.stop_loss_pct <= 20 or not 0 < self.take_profit_pct <= 50:
            raise ValueError("손절은 0~20%, 익절은 0~50% 범위여야 합니다.")
        if not 1 < self.short_window < self.long_window:
            raise ValueError("이동평균은 1 < 단기 < 장기 순서여야 합니다.")
        if not 2 <= self.breakout_window < self.long_window:
            raise ValueError("돌파 기간은 2개 봉 이상이고 장기 이동평균보다 짧아야 합니다.")
        if not 1 <= self.select_count <= self.scan_limit <= 20:
            raise ValueError("검색 수는 1~20개이며 선택 수는 검색 수 이하여야 합니다.")
        if not 0 < self.daily_target_pct <= 20:
            raise ValueError("일일 목표 수익률은 0~20% 범위여야 합니다.")
        if not 0 < self.daily_loss_limit_pct <= 10:
            raise ValueError("일일 손실 한도는 0~10% 범위여야 합니다.")
        if self.reentry_cooldown_minutes < 1 or self.max_entries_per_symbol < 1:
            raise ValueError("재진입 대기시간과 종목별 진입 횟수는 1 이상이어야 합니다.")
        if self.max_daily_round_trips < 1 or self.exit_confirmation_bars < 1:
            raise ValueError("일일 왕복 거래와 매도 확인 봉 수는 1 이상이어야 합니다.")
        if not 0 <= self.min_breakout_pct <= 5:
            raise ValueError("최소 돌파율은 0~5% 범위여야 합니다.")
        if not 0 < self.profit_lock_activation_pct <= 20 or not 0 < self.profit_giveback_pct <= 10:
            raise ValueError("수익 보존 기준이 허용 범위를 벗어났습니다.")
        if not 0 < self.min_relative_volume <= 10:
            raise ValueError("상대 거래량 기준은 0~10배 범위여야 합니다.")
        if not 0 < self.trailing_activation_pct <= 20 or not 0 < self.trailing_drawdown_pct <= 10:
            raise ValueError("추적 매도 기준이 허용 범위를 벗어났습니다.")
        if not 0 < self.breakeven_activation_pct <= 20 or not 0 <= self.breakeven_floor_pct <= 5:
            raise ValueError("본전 보호 기준이 허용 범위를 벗어났습니다.")


class AutoTrader:
    """Paper-only, rule-based trading loop with an explicit kill switch."""

    def __init__(self, client: KISClient, report_dir: Path | None = None):
        self.client = client
        self._config: AutoTradeConfig | None = None
        self._running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._last_run = ""
        self._managed_positions: set[str] = set()
        self._report_dir = report_dir or Path.cwd() / ".stockcast" / "reports"
        self._trading_date = ""
        self._starting_equity: int | None = None
        self._latest_equity: int | None = None
        self._daily_orders: list[dict[str, Any]] = []
        self._latest_report = ""
        self._entry_counts: dict[str, int] = {}
        self._last_exits: dict[str, datetime] = {}
        self._closed_trades = 0
        self._peak_return_pct = 0.0
        self._executions: list[dict[str, Any]] = []
        self._last_execution_sync: datetime | None = None
        self._position_peaks: dict[str, int] = {}

    def start(self, config: AutoTradeConfig) -> None:
        if self.client.settings.environment != "paper":
            raise ValueError("자동매매 MVP는 모의투자에서만 실행할 수 있습니다.")
        config.validate()
        with self._lock:
            if self._running:
                raise ValueError("자동매매가 이미 실행 중입니다.")
            self._config = config
            self._restore_daily_report()
            self._running = True
            self._stop.clear()
            self._record("system", "자동매매 시작")
            self._thread = threading.Thread(target=self._loop, daemon=True, name="stockcast-auto")
            self._thread.start()

    def _restore_daily_report(self) -> None:
        today = datetime.now(KST).date().isoformat()
        if self._trading_date == today:
            return
        path = self._report_dir / f"{today}.json"
        if not path.exists():
            return
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            if report.get("date") != today:
                return
            orders = report.get("orders") or []
            open_positions: set[str] = set()
            for order in orders:
                symbol = str(order.get("symbol") or "")
                if order.get("side") == "buy" and symbol:
                    self._entry_counts[symbol] = self._entry_counts.get(symbol, 0) + 1
                    open_positions.add(symbol)
                elif order.get("side") == "sell" and symbol:
                    self._closed_trades += 1
                    open_positions.discard(symbol)
                    try:
                        self._last_exits[symbol] = datetime.fromisoformat(str(order.get("time")))
                    except (TypeError, ValueError):
                        pass
            self._trading_date = today
            self._starting_equity = report.get("starting_equity")
            self._latest_equity = report.get("latest_equity")
            self._peak_return_pct = float(report.get("peak_return_pct") or 0)
            self._daily_orders = orders
            self._executions = report.get("executions") or []
            self._managed_positions.update(open_positions)
            self._latest_report = str(path)
            self._record("system", "오늘 거래 리포트에서 과매매 제한 상태를 복원했습니다.")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._record("error", f"오늘 거래 상태 복원 실패: {exc}")

    def stop(self) -> None:
        with self._lock:
            was_running = self._running
            self._running = False
            self._stop.set()
            if was_running:
                self._record("system", "자동매매 중지")
        self._sync_executions(force=True)
        with self._lock:
            self._write_daily_report("stopped")

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "last_run": self._last_run,
                "config": asdict(self._config) if self._config else None,
                "events": list(reversed(self._events[-50:])),
                "daily_performance": self._daily_performance(),
            }

    def _daily_performance(self) -> dict[str, Any]:
        return_pct = self._daily_return_pct()
        target = self._config.daily_target_pct if self._config else 10.0
        loss_limit = self._config.daily_loss_limit_pct if self._config else 3.0
        return {
            "date": self._trading_date,
            "starting_equity": self._starting_equity,
            "latest_equity": self._latest_equity,
            "return_pct": return_pct,
            "target_pct": target,
            "target_reached": return_pct is not None and return_pct >= target,
            "loss_limit_pct": loss_limit,
            "loss_limit_reached": return_pct is not None and return_pct <= -loss_limit,
            "order_count": len(self._daily_orders),
            "report_path": self._latest_report,
            "peak_return_pct": round(self._peak_return_pct, 4),
            "closed_trades": self._closed_trades,
            "profit_lock_active": self._profit_lock_active(),
        }

    def _daily_return_pct(self) -> float | None:
        if not self._starting_equity or self._latest_equity is None:
            return None
        return round((self._latest_equity - self._starting_equity) / self._starting_equity * 100, 4)

    def _update_equity(self, balance: dict[str, Any]) -> None:
        today = datetime.now(KST).date().isoformat()
        if self._trading_date != today:
            self._trading_date = today
            self._starting_equity = None
            self._latest_equity = None
            self._daily_orders = []
            self._latest_report = ""
            self._entry_counts = {}
            self._last_exits = {}
            self._closed_trades = 0
            self._peak_return_pct = 0.0
            self._executions = []
            self._last_execution_sync = None
            self._position_peaks = {}
        summary = (balance.get("output2") or [{}])[0]
        raw_equity = summary.get("tot_evlu_amt") or summary.get("nass_amt")
        if raw_equity in (None, ""):
            return
        equity = int(float(raw_equity))
        if equity <= 0:
            return
        if self._starting_equity is None:
            self._starting_equity = equity
        self._latest_equity = equity
        return_pct = self._daily_return_pct()
        if return_pct is not None:
            self._peak_return_pct = max(self._peak_return_pct, return_pct)

    def _profit_lock_active(self) -> bool:
        if not self._config:
            return False
        current = self._daily_return_pct()
        return (
            current is not None
            and self._peak_return_pct >= self._config.profit_lock_activation_pct
            and self._peak_return_pct - current >= self._config.profit_giveback_pct
        )

    def _can_reenter(self, symbol: str, config: AutoTradeConfig) -> bool:
        if self._entry_counts.get(symbol, 0) >= config.max_entries_per_symbol:
            return False
        last_exit = self._last_exits.get(symbol)
        if last_exit is None:
            return True
        return datetime.now(KST) - last_exit >= timedelta(minutes=config.reentry_cooldown_minutes)

    def _sync_executions(self, *, force: bool = False) -> None:
        now = datetime.now(KST)
        if not self._trading_date:
            return
        if not force and self._last_execution_sync and now - self._last_execution_sync < timedelta(minutes=10):
            return
        try:
            executions = self.client.daily_executions(self._trading_date)
            if isinstance(executions, list):
                self._executions = executions
                self._last_execution_sync = now
        except Exception as exc:
            with self._lock:
                self._record("error", f"체결 내역 동기화 실패: {exc}")

    def _record_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        reference_price: int,
        *,
        reason: str = "",
    ) -> None:
        self._daily_orders.append({
            "time": datetime.now(KST).isoformat(timespec="seconds"),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "reference_price": reference_price,
            "reason": reason,
        })

    def _write_daily_report(self, status: str) -> None:
        if not self._trading_date or self._starting_equity is None:
            return
        path = self._report_dir / f"{self._trading_date}.json"
        self._latest_report = str(path)
        report = {
            **self._daily_performance(),
            "status": status,
            "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
            "orders": list(self._daily_orders),
            "executions": list(self._executions),
            "measurement": "KIS 계좌 총평가금액 스냅샷 기준이며 실제 체결 손익 장부와 다를 수 있습니다.",
        }
        self._report_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def _order_quantity(
        self,
        config: AutoTradeConfig,
        current_price: int,
        return_pct: float | None,
    ) -> int:
        if not self._latest_equity or current_price <= 0:
            return 0
        budget = self._latest_equity * config.position_size_pct / 100
        if return_pct is not None and (return_pct < 0 or return_pct >= config.daily_target_pct / 2):
            budget /= 2
        budget = min(budget, self.client.settings.max_order_krw)
        return int(budget // current_price)

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

    @staticmethod
    def _entry_is_open() -> bool:
        now = datetime.now(KST)
        morning = time(9, 10) <= now.time() < time(11, 20)
        afternoon = time(13, 0) <= now.time() < time(14, 50)
        return now.weekday() < 5 and (morning or afternoon)

    @staticmethod
    def _bar_metrics(bars: list[dict[str, int | str]]) -> tuple[list[int], float, float]:
        closes = [int(bar.get("price") or 0) for bar in bars]
        volumes = [int(bar.get("volume") or 0) for bar in bars]
        total_volume = sum(volumes)
        vwap = (
            sum(price * volume for price, volume in zip(closes, volumes)) / total_volume
            if total_volume else 0
        )
        completed = volumes[:-1]
        baseline = completed[-11:-1]
        average_volume = sum(baseline) / len(baseline) if baseline else 0
        relative_volume = completed[-1] / average_volume if average_volume and completed else 0
        return closes, vwap, relative_volume

    @staticmethod
    def _must_liquidate() -> bool:
        now = datetime.now(KST)
        return now.weekday() < 5 and time(15, 15) <= now.time() <= time(15, 20)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._market_is_open():
                    self.run_once(force_exit=self._must_liquidate())
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

    def run_once(self, *, force_exit: bool = False) -> None:
        config = self._config
        if config is None:
            raise ValueError("자동매매 설정이 없습니다.")
        balance = self.client.balance()
        self._update_equity(balance)
        daily_return = self._daily_return_pct()
        holdings = {
            row.get("pdno", ""): row for row in balance.get("output1", [])
            if int(row.get("hldg_qty") or 0) > 0
        }
        position_count = len(holdings)
        if daily_return is not None and daily_return <= -config.daily_loss_limit_pct:
            force_exit = True
            with self._lock:
                self._record("risk", f"일일 손실 한도 {config.daily_loss_limit_pct:.1f}% 도달: 신규 진입 중단 및 청산")
        if self._profit_lock_active():
            with self._lock:
                self._record("risk", "당일 최고 수익률 대비 반납 한도 도달: 신규 진입 중단")
        if force_exit:
            for symbol in sorted(self._managed_positions):
                row = holdings.get(symbol)
                if not row:
                    self._managed_positions.discard(symbol)
                    self._position_peaks.pop(symbol, None)
                    self._last_exits[symbol] = datetime.now(KST)
                    self._closed_trades += 1
                    continue
                quantity = int(row.get("hldg_qty") or 0)
                if quantity > 0:
                    self.client.order(OrderRequest(symbol, "sell", quantity))
                    reason = "daily_loss_limit" if daily_return is not None and daily_return <= -config.daily_loss_limit_pct else "market_close"
                    self._record_order(
                        symbol, "sell", quantity, int(float(row.get("prpr") or 0)), reason=reason,
                    )
                    self._managed_positions.discard(symbol)
                    with self._lock:
                        self._record("sell", f"{symbol} {quantity}주 장 마감 전 전량 매도")
            self._sync_executions(force=True)
            with self._lock:
                self._last_run = datetime.now(KST).isoformat(timespec="seconds")
                self._write_daily_report("closed")
            return

        bars_cache: dict[str, list[dict[str, int | str]]] = {}
        symbols = list(config.symbols)
        if config.auto_discover:
            symbols, bars_cache = self._discover(config)
            with self._lock:
                self._record("scan", f"자동 검색 결과: {', '.join(symbols) if symbols else '조건 충족 종목 없음'}")
        symbols = list(dict.fromkeys([*symbols, *self._managed_positions]))
        strategy = TrendBreakoutStrategy(
            config.short_window, config.long_window, config.breakout_window,
            config.min_breakout_pct, config.exit_confirmation_bars,
        )

        for symbol in symbols:
            bars = bars_cache.get(symbol) or self.client.intraday_bars(symbol)
            closes, vwap, relative_volume = self._bar_metrics(bars)
            if len(closes) < config.long_window:
                with self._lock:
                    self._record("hold", f"{symbol}: 가격 이력 부족")
                continue
            current = closes[-1]
            row = holdings.get(symbol) if symbol in self._managed_positions else None

            if row:
                average = float(row.get("pchs_avg_pric") or current)
                return_pct = (current - average) / average * 100 if average else 0
                peak = max(self._position_peaks.get(symbol, current), current)
                self._position_peaks[symbol] = peak
                peak_return_pct = (peak - average) / average * 100 if average else 0
                peak_drawdown_pct = (peak - current) / peak * 100 if peak else 0
                trailing_exit = (
                    peak_return_pct >= config.trailing_activation_pct
                    and peak_drawdown_pct >= config.trailing_drawdown_pct
                )
                breakeven_exit = (
                    peak_return_pct >= config.breakeven_activation_pct
                    and return_pct <= config.breakeven_floor_pct
                )
                should_sell = (
                    strategy.evaluate(closes, holding=True) is Signal.SELL
                    or return_pct <= -config.stop_loss_pct
                    or return_pct >= config.take_profit_pct
                    or trailing_exit
                    or breakeven_exit
                )
                if should_sell:
                    if return_pct <= -config.stop_loss_pct:
                        exit_reason = "stop_loss"
                    elif return_pct >= config.take_profit_pct:
                        exit_reason = "take_profit"
                    elif trailing_exit:
                        exit_reason = "trailing_stop"
                    elif breakeven_exit:
                        exit_reason = "breakeven_protection"
                    else:
                        exit_reason = "trend_exit_confirmed"
                    quantity = int(row.get("hldg_qty") or 0)
                    self.client.order(OrderRequest(symbol, "sell", quantity))
                    self._record_order(symbol, "sell", quantity, current, reason=exit_reason)
                    self._managed_positions.discard(symbol)
                    self._position_peaks.pop(symbol, None)
                    self._last_exits[symbol] = datetime.now(KST)
                    self._closed_trades += 1
                    with self._lock:
                        self._record("sell", f"{symbol} {quantity}주 매도", return_pct=round(return_pct, 2))
                else:
                    with self._lock:
                        self._record("hold", f"{symbol}: 보유 유지", return_pct=round(return_pct, 2))
            elif symbol in self._managed_positions:
                with self._lock:
                    self._record("hold", f"{symbol}: 주문 후 잔고 반영 대기")
            elif (
                self._entry_is_open()
                and
                (daily_return is None or daily_return < config.daily_target_pct)
                and
                not self._profit_lock_active()
                and self._closed_trades < config.max_daily_round_trips
                and self._can_reenter(symbol, config)
                and
                current > vwap
                and relative_volume >= config.min_relative_volume
                and
                strategy.evaluate(closes) is Signal.BUY
                and position_count < config.max_positions
            ):
                quantity = self._order_quantity(config, current, daily_return)
                if quantity <= 0:
                    with self._lock:
                        self._record("hold", f"{symbol}: 종목당 투자 예산이 1주 가격보다 작음")
                else:
                    self.client.order(OrderRequest(symbol, "buy", quantity))
                    self._record_order(symbol, "buy", quantity, current, reason="trend_breakout")
                    self._managed_positions.add(symbol)
                    self._entry_counts[symbol] = self._entry_counts.get(symbol, 0) + 1
                    position_count += 1
                    with self._lock:
                        self._record("buy", f"{symbol} {quantity}주 매수", budget=current * quantity)
            else:
                with self._lock:
                    self._record("hold", f"{symbol}: 매수 조건 미충족")

        self._sync_executions()
        with self._lock:
            self._last_run = datetime.now(KST).isoformat(timespec="seconds")
            self._write_daily_report("running")

    def _discover(
        self, config: AutoTradeConfig,
    ) -> tuple[list[str], dict[str, list[dict[str, int | str]]]]:
        ranked = self.client.daytrade_rank(limit=config.scan_limit)
        scored: list[tuple[float, str]] = []
        cache: dict[str, list[dict[str, int | str]]] = {}
        for rank, row in enumerate(ranked):
            symbol = str(row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or "")
            if len(symbol) != 6 or not symbol.isdigit():
                continue
            if self.client.settings.allowed_symbols and symbol not in self.client.settings.allowed_symbols:
                continue
            bars = self.client.intraday_bars(symbol)
            cache[symbol] = bars
            closes, vwap, relative_volume = self._bar_metrics(bars)
            if len(closes) < config.long_window or closes[-2] <= 0:
                continue
            if closes[-1] <= vwap or relative_volume < config.min_relative_volume:
                continue
            strategy = TrendBreakoutStrategy(
                config.short_window, config.long_window, config.breakout_window,
                config.min_breakout_pct, config.exit_confirmation_bars,
            )
            if strategy.evaluate(closes) is not Signal.BUY:
                continue
            short_sma = sum(closes[-config.short_window:]) / config.short_window
            long_sma = sum(closes[-config.long_window:]) / config.long_window
            trend_gap = (short_sma - long_sma) / long_sma * 100 if long_sma else 0
            previous_high = max(closes[-config.breakout_window - 1:-1])
            breakout = (closes[-1] - previous_high) / previous_high * 100
            liquidity_bonus = (config.scan_limit - rank) / config.scan_limit
            scored.append((trend_gap + breakout + liquidity_bonus, symbol))
        scored.sort(reverse=True)
        return [symbol for _, symbol in scored[:config.select_count]], cache
