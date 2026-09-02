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
        self._lowest_return_pct = 0.0
        self._sessions: list[dict[str, str]] = []
        self._run_count = 0
        self._scan_count = 0
        self._scan_candidates_seen = 0
        self._scan_rejections: dict[str, int] = {}
        self._decision_counts: dict[str, int] = {}
        self._last_selected_symbols: list[str] = []
        self._latest_managed_positions: list[dict[str, Any]] = []

    def start(self, config: AutoTradeConfig) -> None:
        if self.client.settings.environment != "paper":
            raise ValueError("자동매매 MVP는 모의투자에서만 실행할 수 있습니다.")
        config.validate()
        with self._lock:
            if self._running:
                raise ValueError("자동매매가 이미 실행 중입니다.")
            self._config = config
            self._restore_daily_report()
            self._sessions.append({
                "started_at": datetime.now(KST).isoformat(timespec="seconds"),
                "ended_at": "",
            })
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
            self._events = list(report.get("events") or [])[-100:]
            telemetry = report.get("telemetry") or {}
            self._lowest_return_pct = float(report.get("lowest_return_pct") or 0)
            self._sessions = report.get("sessions") or []
            self._run_count = int(telemetry.get("run_count") or 0)
            self._scan_count = int(telemetry.get("scan_count") or 0)
            self._scan_candidates_seen = int(telemetry.get("scan_candidates_seen") or 0)
            self._scan_rejections = dict(telemetry.get("scan_rejections") or {})
            self._decision_counts = dict(telemetry.get("decision_counts") or {})
            self._last_selected_symbols = list(telemetry.get("last_selected_symbols") or [])
            self._latest_managed_positions = list(report.get("end_positions") or [])
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
            if self._sessions and not self._sessions[-1].get("ended_at"):
                self._sessions[-1]["ended_at"] = datetime.now(KST).isoformat(timespec="seconds")
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
            "lowest_return_pct": round(self._lowest_return_pct, 4),
            "closed_trades": self._closed_trades,
            "profit_lock_active": self._profit_lock_active(),
            "journal_path": str(self._journal_path()) if self._trading_date else "",
        }

    def _daily_return_pct(self) -> float | None:
        if not self._starting_equity or self._latest_equity is None:
            return None
        return round((self._latest_equity - self._starting_equity) / self._starting_equity * 100, 4)

    def _update_equity(self, balance: dict[str, Any]) -> None:
        today = datetime.now(KST).date().isoformat()
        if self._trading_date != today:
            active_sessions = [
                session for session in self._sessions
                if str(session.get("started_at") or "").startswith(today)
                and not session.get("ended_at")
            ]
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
            self._lowest_return_pct = 0.0
            self._sessions = active_sessions
            self._run_count = 0
            self._scan_count = 0
            self._scan_candidates_seen = 0
            self._scan_rejections = {}
            self._decision_counts = {}
            self._last_selected_symbols = []
            self._latest_managed_positions = []
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
            self._lowest_return_pct = min(self._lowest_return_pct, return_pct)

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
        response: dict[str, Any] | None = None,
    ) -> None:
        output = response.get("output") if isinstance(response, dict) else {}
        output = output if isinstance(output, dict) else {}
        order_id = str(output.get("ODNO") or output.get("odno") or "")
        order = {
            "time": datetime.now(KST).isoformat(timespec="seconds"),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "reference_price": reference_price,
            "reason": reason,
        }
        if order_id:
            order["order_id"] = order_id
        self._daily_orders.append(order)

    @staticmethod
    def _number(value: Any) -> int:
        try:
            return int(float(str(value or "0").replace(",", "")))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _execution_side(row: dict[str, Any]) -> str:
        name = str(row.get("sll_buy_dvsn_cd_name") or "").lower()
        if "매수" in name or "buy" in name:
            return "buy"
        if "매도" in name or "sell" in name:
            return "sell"
        return "unknown"

    def _execution_analysis(self) -> dict[str, Any]:
        strategy_order_ids = {
            str(order.get("order_id")) for order in self._daily_orders if order.get("order_id")
        }
        strategy_rows = [
            row for row in self._executions
            if str(row.get("odno") or "") in strategy_order_ids
        ]
        matched_order_ids = {str(row.get("odno") or "") for row in strategy_rows}
        unconfirmed_order_count = sum(
            1 for order in self._daily_orders
            if not order.get("order_id") or str(order.get("order_id")) not in matched_order_ids
        )
        filled_quantity = sum(self._number(row.get("tot_ccld_qty")) for row in strategy_rows)
        unfilled_quantity = sum(self._number(row.get("rmn_qty")) for row in strategy_rows)
        requested_quantity = sum(self._number(order.get("quantity")) for order in self._daily_orders)
        fill_rate_pct = (
            round(filled_quantity / requested_quantity * 100, 2)
            if requested_quantity else None
        )

        positions: dict[str, dict[str, float]] = {}
        trades: list[dict[str, Any]] = []
        rows = sorted(strategy_rows, key=lambda row: (
            str(row.get("ord_dt") or ""), str(row.get("ord_tmd") or ""),
        ))
        for row in rows:
            symbol = str(row.get("pdno") or "")
            quantity = self._number(row.get("tot_ccld_qty"))
            price = self._number(row.get("avg_prvs"))
            if not symbol or quantity <= 0 or price <= 0:
                continue
            if self._execution_side(row) == "buy":
                position = positions.setdefault(symbol, {"quantity": 0.0, "cost": 0.0})
                position["quantity"] += quantity
                position["cost"] += quantity * price
            elif self._execution_side(row) == "sell":
                position = positions.get(symbol, {"quantity": 0.0, "cost": 0.0})
                matched = min(quantity, int(position["quantity"]))
                if matched <= 0:
                    continue
                average_buy = position["cost"] / position["quantity"]
                gross_pnl = round((price - average_buy) * matched)
                trades.append({
                    "symbol": symbol,
                    "name": str(row.get("prdt_name") or ""),
                    "quantity": matched,
                    "average_buy_price": round(average_buy, 2),
                    "average_sell_price": price,
                    "gross_pnl_krw": gross_pnl,
                    "return_pct_before_costs": round((price - average_buy) / average_buy * 100, 2),
                    "sell_time": str(row.get("ord_tmd") or ""),
                })
                remaining = position["quantity"] - matched
                position["cost"] = average_buy * remaining
                position["quantity"] = remaining

        wins = sum(1 for trade in trades if trade["gross_pnl_krw"] > 0)
        losses = sum(1 for trade in trades if trade["gross_pnl_krw"] < 0)
        return {
            "submitted_order_count": len(self._daily_orders),
            "requested_quantity": requested_quantity,
            "account_execution_rows": len(self._executions),
            "strategy_execution_rows": len(strategy_rows),
            "unconfirmed_order_count": unconfirmed_order_count,
            "filled_quantity": filled_quantity,
            "unfilled_quantity": unfilled_quantity,
            "fill_rate_pct": fill_rate_pct,
            "closed_trade_count": len(trades),
            "wins": wins,
            "losses": losses,
            "breakeven": len(trades) - wins - losses,
            "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else None,
            "gross_realized_pnl_krw": sum(trade["gross_pnl_krw"] for trade in trades),
            "trades": trades,
            "note": "체결 손익은 Stockcast 주문번호와 일별 체결을 연결한 세전·수수료 전 추정치입니다.",
        }

    def _journal_path(self) -> Path:
        return self._report_dir / f"{self._trading_date}.md"

    def _review(self, execution: dict[str, Any]) -> dict[str, list[str]]:
        issues: list[str] = []
        positives: list[str] = []
        actions: list[str] = []
        errors = [event for event in self._events if event.get("kind") == "error"]
        return_pct = self._daily_return_pct()
        if not self._daily_orders:
            reason = "진입 조건을 충족한 종목이 없었습니다"
            if not self._scan_count and not self._decision_counts:
                reason = "판단 이력이 저장되지 않아 무거래 원인을 확정할 수 없습니다"
                actions.append("다음 세션부터 검색·판단 통계를 확인해 무거래가 필터 때문인지 운영 문제인지 구분합니다.")
            issues.append(f"주문이 한 건도 없었습니다. {reason}.")
        if errors:
            issues.append(f"운영 오류가 {len(errors)}건 기록됐습니다.")
            actions.append("오류 타임라인을 확인하고 다음 거래 전 API·네트워크 상태를 점검합니다.")
        else:
            positives.append("기록된 API·전략 실행 오류가 없습니다.")
        if execution["unfilled_quantity"]:
            issues.append(f"미체결 수량이 {execution['unfilled_quantity']}주 남았습니다.")
            actions.append("미체결 주문과 실제 잔고를 대조한 뒤 다음 세션을 시작합니다.")
        if execution["unconfirmed_order_count"]:
            issues.append(
                f"제출 주문 {execution['unconfirmed_order_count']}건이 KIS 체결조회와 아직 연결되지 않았습니다."
            )
            actions.append("연결되지 않은 주문번호, 체결 내역과 실제 잔고를 대조합니다.")
        if return_pct is not None and return_pct < 0:
            issues.append(f"계좌 평가금액 기준 손익률이 {return_pct:.2f}%로 마감됐습니다.")
        if self._latest_managed_positions:
            issues.append(f"리포트 시점에 봇 관리 포지션 {len(self._latest_managed_positions)}개가 남아 있습니다.")
            actions.append("보유 잔량과 마감 청산 주문의 체결 여부를 확인합니다.")
        else:
            positives.append("리포트 시점에 기록된 봇 관리 잔량이 없습니다.")
        if self._scan_rejections:
            top_reason = max(self._scan_rejections, key=self._scan_rejections.get)
            reason_names = {
                "invalid_symbol": "종목코드 오류", "not_allowed": "허용목록 제외",
                "insufficient_history": "가격 이력 부족", "below_vwap": "VWAP 이하",
                "low_relative_volume": "상대 거래량 부족", "no_buy_signal": "매수 신호 없음",
            }
            actions.append(
                f"가장 빈번한 검색 탈락 사유({reason_names.get(top_reason, top_reason)})는 "
                "여러 거래일 표본으로 검토한 뒤 기준을 조정합니다."
            )
        if not issues:
            positives.append("손익·체결·리스크 지표에서 즉시 조치할 예외가 발견되지 않았습니다.")
        return {"positives": positives, "issues": issues, "next_actions": actions}

    def _build_journal(self, status: str) -> dict[str, Any]:
        execution = self._execution_analysis()
        net_change = (
            self._latest_equity - self._starting_equity
            if self._starting_equity is not None and self._latest_equity is not None else None
        )
        return {
            "summary": {
                "status": status,
                "net_change_krw": net_change,
                "return_pct": self._daily_return_pct(),
                "peak_return_pct": round(self._peak_return_pct, 4),
                "max_drawdown_from_start_pct": round(self._lowest_return_pct, 4),
            },
            "session": {
                "sessions": list(self._sessions),
                "run_count": self._run_count,
                "scan_count": self._scan_count,
            },
            "selection": {
                "candidate_evaluations": self._scan_candidates_seen,
                "scan_rejections": dict(sorted(self._scan_rejections.items())),
                "decision_blocks": dict(sorted(self._decision_counts.items())),
                "last_selected_symbols": list(self._last_selected_symbols),
            },
            "execution": execution,
            "risk": {
                "target_reached": self._daily_performance()["target_reached"],
                "loss_limit_reached": self._daily_performance()["loss_limit_reached"],
                "profit_lock_active": self._profit_lock_active(),
                "end_positions": list(self._latest_managed_positions),
            },
            "review": self._review(execution),
        }

    @staticmethod
    def _render_markdown(report: dict[str, Any]) -> str:
        journal = report["journal"]
        summary = journal["summary"]
        execution = journal["execution"]
        selection = journal["selection"]
        review = journal["review"]
        money = lambda value: "확인 불가" if value is None else f"{value:+,}원"
        percent = lambda value: "확인 불가" if value is None else f"{value:+.2f}%"
        reason_names = {
            "invalid_symbol": "종목코드 오류", "not_allowed": "허용목록 제외",
            "insufficient_history": "가격 이력 부족", "below_vwap": "VWAP 이하",
            "low_relative_volume": "상대 거래량 부족", "no_buy_signal": "매수 신호 없음",
            "outside_entry_window": "진입 시간 아님", "daily_target_reached": "일일 목표 도달",
            "profit_lock": "수익 보존 발동", "max_round_trips": "왕복 거래 한도",
            "reentry_limit_or_cooldown": "재진입 제한", "max_positions": "보유 종목 한도",
            "insufficient_budget": "주문 예산 부족",
        }
        lines = [
            f"# {report['date']} 데이트레이딩 리포트",
            "",
            "## 장 마감 요약",
            "",
            f"- 상태: {report['status']}",
            f"- 계좌 평가손익: {money(summary['net_change_krw'])} ({percent(summary['return_pct'])})",
            f"- 장중 최고 / 시작 대비 최대 하락: {percent(summary['peak_return_pct'])} / {percent(summary['max_drawdown_from_start_pct'])}",
            f"- 전략 주문 / 전략 체결 / 전체 계좌 체결: {execution['submitted_order_count']}건 / {execution['strategy_execution_rows']}건 / {execution['account_execution_rows']}건",
            f"- 주문 수량 / 체결 수량 / 미체결 수량: {execution['requested_quantity']:,}주 / {execution['filled_quantity']:,}주 / {execution['unfilled_quantity']:,}주 (체결률 {percent(execution['fill_rate_pct'])})",
            f"- 승 / 패 / 승률: {execution['wins']} / {execution['losses']} / {percent(execution['win_rate_pct'])}",
            f"- 실현손익 추정(비용 전): {money(execution['gross_realized_pnl_krw'])}",
            "",
            "## 운영 및 종목 선정",
            "",
            f"- 전략 실행: {journal['session']['run_count']}회",
            f"- 자동 검색: {journal['session']['scan_count']}회, 후보 평가 {selection['candidate_evaluations']}회",
            f"- 마지막 선정 종목: {', '.join(selection['last_selected_symbols']) or '없음'}",
        ]
        sessions = journal["session"]["sessions"]
        if sessions:
            lines.append(
                f"- 세션: {sessions[0].get('started_at') or '확인 불가'} ~ "
                f"{sessions[-1].get('ended_at') or '실행 중'} ({len(sessions)}회 시작)"
            )
        if selection["scan_rejections"]:
            lines.append("- 검색 탈락: " + ", ".join(
                f"{reason_names.get(key, key)} {value}회"
                for key, value in selection["scan_rejections"].items()
            ))
        if selection["decision_blocks"]:
            lines.append("- 진입 차단: " + ", ".join(
                f"{reason_names.get(key, key)} {value}회"
                for key, value in selection["decision_blocks"].items()
            ))
        lines.extend(["", "## 거래 복기", ""])
        if execution["trades"]:
            lines.extend([
                "| 종목 | 수량 | 매수가 | 매도가 | 수익률 | 손익(비용 전) |",
                "|---|---:|---:|---:|---:|---:|",
            ])
            for trade in execution["trades"]:
                label = trade["name"] or trade["symbol"]
                lines.append(
                    f"| {label} ({trade['symbol']}) | {trade['quantity']:,} | "
                    f"{trade['average_buy_price']:,.2f} | {trade['average_sell_price']:,} | "
                    f"{trade['return_pct_before_costs']:+.2f}% | {trade['gross_pnl_krw']:+,}원 |"
                )
        else:
            lines.append("- 주문번호로 연결된 완료 거래가 없습니다.")
        lines.extend([
            "", "## 리스크 및 잔여 포지션", "",
            f"- 일일 목표 도달: {'예' if journal['risk']['target_reached'] else '아니오'}",
            f"- 일일 손실 한도 도달: {'예' if journal['risk']['loss_limit_reached'] else '아니오'}",
            f"- 수익 보존 발동: {'예' if journal['risk']['profit_lock_active'] else '아니오'}",
        ])
        if journal["risk"]["end_positions"]:
            for position in journal["risk"]["end_positions"]:
                quantity = position.get("quantity")
                quantity_text = "잔고 반영 대기" if quantity is None else f"{quantity:,}주"
                lines.append(f"- {position.get('name') or position['symbol']} ({position['symbol']}): {quantity_text}")
        else:
            lines.append("- 봇 관리 잔여 포지션: 없음")
        for title, key in (("잘한 점", "positives"), ("문제점", "issues"), ("다음 거래일 액션", "next_actions")):
            lines.extend(["", f"## {title}", ""])
            lines.extend(f"- {item}" for item in review[key])
            if not review[key]:
                lines.append("- 특이사항 없음")
        lines.extend([
            "", "## 데이터 주의사항", "",
            f"- {report['measurement']}",
            f"- {execution['note']}",
            "- 주문번호가 없는 과거 주문이나 외부 주문은 전략 체결 손익에서 제외됩니다.",
            "",
        ])
        return "\n".join(lines)

    def _write_daily_report(self, status: str) -> None:
        if not self._trading_date or self._starting_equity is None:
            return
        path = self._report_dir / f"{self._trading_date}.json"
        self._latest_report = str(path)
        measurement = "KIS 계좌 총평가금액 스냅샷 기준이며 실제 체결 손익 장부와 다를 수 있습니다."
        report = {
            **self._daily_performance(),
            "status": status,
            "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
            "config": asdict(self._config) if self._config else None,
            "sessions": list(self._sessions),
            "telemetry": {
                "run_count": self._run_count,
                "scan_count": self._scan_count,
                "scan_candidates_seen": self._scan_candidates_seen,
                "scan_rejections": dict(self._scan_rejections),
                "decision_counts": dict(self._decision_counts),
                "last_selected_symbols": list(self._last_selected_symbols),
            },
            "orders": list(self._daily_orders),
            "executions": list(self._executions),
            "end_positions": list(self._latest_managed_positions),
            "events": list(self._events),
            "measurement": measurement,
        }
        report["journal"] = self._build_journal(status)
        self._report_dir.mkdir(parents=True, exist_ok=True)
        json_temp = path.with_suffix(".json.tmp")
        markdown_path = self._journal_path()
        markdown_temp = markdown_path.with_suffix(".md.tmp")
        json_temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown_temp.write_text(self._render_markdown(report), encoding="utf-8")
        json_temp.replace(path)
        markdown_temp.replace(markdown_path)

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

    def _increment(self, target: dict[str, int], key: str) -> None:
        target[key] = target.get(key, 0) + 1

    def _capture_managed_positions(self, holdings: dict[str, dict[str, Any]]) -> None:
        self._latest_managed_positions = []
        for symbol in sorted(self._managed_positions):
            row = holdings.get(symbol)
            if not row:
                self._latest_managed_positions.append({
                    "symbol": symbol,
                    "name": "",
                    "quantity": None,
                    "average_price": None,
                    "current_price": None,
                    "evaluation_pnl_krw": None,
                    "balance_status": "pending_or_missing",
                })
                continue
            self._latest_managed_positions.append({
                "symbol": symbol,
                "name": str(row.get("prdt_name") or ""),
                "quantity": self._number(row.get("hldg_qty")),
                "average_price": self._number(row.get("pchs_avg_pric")),
                "current_price": self._number(row.get("prpr")),
                "evaluation_pnl_krw": self._number(row.get("evlu_pfls_amt")),
                "balance_status": "confirmed",
            })

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
        self._run_count += 1
        daily_return = self._daily_return_pct()
        holdings = {
            row.get("pdno", ""): row for row in balance.get("output1", [])
            if int(row.get("hldg_qty") or 0) > 0
        }
        self._capture_managed_positions(holdings)
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
                    response = self.client.order(OrderRequest(symbol, "sell", quantity))
                    reason = "daily_loss_limit" if daily_return is not None and daily_return <= -config.daily_loss_limit_pct else "market_close"
                    self._record_order(
                        symbol, "sell", quantity, int(float(row.get("prpr") or 0)), reason=reason,
                        response=response,
                    )
                    self._managed_positions.discard(symbol)
                    with self._lock:
                        self._record("sell", f"{symbol} {quantity}주 장 마감 전 전량 매도")
            self._capture_managed_positions(holdings)
            self._sync_executions(force=True)
            with self._lock:
                self._last_run = datetime.now(KST).isoformat(timespec="seconds")
                self._write_daily_report("closed")
            return

        bars_cache: dict[str, list[dict[str, int | str]]] = {}
        symbols = list(config.symbols)
        if config.auto_discover:
            symbols, bars_cache = self._discover(config)
            self._last_selected_symbols = list(symbols)
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
                    response = self.client.order(OrderRequest(symbol, "sell", quantity))
                    self._record_order(
                        symbol, "sell", quantity, current, reason=exit_reason, response=response,
                    )
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
            else:
                blockers: list[str] = []
                if not self._entry_is_open():
                    blockers.append("outside_entry_window")
                if daily_return is not None and daily_return >= config.daily_target_pct:
                    blockers.append("daily_target_reached")
                if self._profit_lock_active():
                    blockers.append("profit_lock")
                if self._closed_trades >= config.max_daily_round_trips:
                    blockers.append("max_round_trips")
                if not self._can_reenter(symbol, config):
                    blockers.append("reentry_limit_or_cooldown")
                if current <= vwap:
                    blockers.append("below_vwap")
                if relative_volume < config.min_relative_volume:
                    blockers.append("low_relative_volume")
                if strategy.evaluate(closes) is not Signal.BUY:
                    blockers.append("no_buy_signal")
                if position_count >= config.max_positions:
                    blockers.append("max_positions")
                if blockers:
                    for blocker in blockers:
                        self._increment(self._decision_counts, blocker)
                    with self._lock:
                        self._record("hold", f"{symbol}: 매수 조건 미충족", blockers=blockers)
                    continue
                quantity = self._order_quantity(config, current, daily_return)
                if quantity <= 0:
                    self._increment(self._decision_counts, "insufficient_budget")
                    with self._lock:
                        self._record("hold", f"{symbol}: 종목당 투자 예산이 1주 가격보다 작음")
                else:
                    response = self.client.order(OrderRequest(symbol, "buy", quantity))
                    self._record_order(
                        symbol, "buy", quantity, current, reason="trend_breakout", response=response,
                    )
                    self._managed_positions.add(symbol)
                    self._entry_counts[symbol] = self._entry_counts.get(symbol, 0) + 1
                    position_count += 1
                    with self._lock:
                        self._record("buy", f"{symbol} {quantity}주 매수", budget=current * quantity)

        self._capture_managed_positions(holdings)
        self._sync_executions()
        with self._lock:
            self._last_run = datetime.now(KST).isoformat(timespec="seconds")
            self._write_daily_report("running")

    def _discover(
        self, config: AutoTradeConfig,
    ) -> tuple[list[str], dict[str, list[dict[str, int | str]]]]:
        ranked = self.client.daytrade_rank(limit=config.scan_limit)
        self._scan_count += 1
        self._scan_candidates_seen += len(ranked)
        scored: list[tuple[float, str]] = []
        cache: dict[str, list[dict[str, int | str]]] = {}
        for rank, row in enumerate(ranked):
            symbol = str(row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or "")
            if len(symbol) != 6 or not symbol.isdigit():
                self._increment(self._scan_rejections, "invalid_symbol")
                continue
            if self.client.settings.allowed_symbols and symbol not in self.client.settings.allowed_symbols:
                self._increment(self._scan_rejections, "not_allowed")
                continue
            bars = self.client.intraday_bars(symbol)
            cache[symbol] = bars
            closes, vwap, relative_volume = self._bar_metrics(bars)
            if len(closes) < config.long_window or closes[-2] <= 0:
                self._increment(self._scan_rejections, "insufficient_history")
                continue
            if closes[-1] <= vwap:
                self._increment(self._scan_rejections, "below_vwap")
                continue
            if relative_volume < config.min_relative_volume:
                self._increment(self._scan_rejections, "low_relative_volume")
                continue
            strategy = TrendBreakoutStrategy(
                config.short_window, config.long_window, config.breakout_window,
                config.min_breakout_pct, config.exit_confirmation_bars,
            )
            if strategy.evaluate(closes) is not Signal.BUY:
                self._increment(self._scan_rejections, "no_buy_signal")
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
