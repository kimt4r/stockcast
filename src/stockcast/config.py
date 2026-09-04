from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def is_domestic_symbol(value: str) -> bool:
    """Return whether value is a six-character KRX short code."""
    return len(value) == 6 and value.isascii() and value.isalnum() and value == value.upper()


def load_dotenv(path: str | Path = ".env") -> None:
    """Load a small, dependency-free subset of dotenv without overwriting env vars."""
    file = Path(path)
    if not file.exists():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_dotenv(path: str | Path) -> dict[str, str]:
    """Read dotenv values without mutating process-wide environment variables."""
    values: dict[str, str] = {}
    file = Path(path)
    if not file.exists():
        return values
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def update_dotenv(path: str | Path, updates: dict[str, str]) -> None:
    """Update selected dotenv keys while preserving credentials and comments."""
    file = Path(path)
    lines = file.read_text(encoding="utf-8").splitlines() if file.exists() else []
    remaining = dict(updates)
    result: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                result.append(f"{key}={remaining.pop(key)}")
                continue
        result.append(raw)
    if remaining and result and result[-1]:
        result.append("")
    result.extend(f"{key}={value}" for key, value in remaining.items())
    file.write_text("\n".join(result) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Settings:
    app_key: str
    app_secret: str
    account_no: str
    product_code: str = "01"
    environment: str = "paper"
    allowed_symbols: frozenset[str] = frozenset()
    max_order_krw: int = 100_000
    allow_live: bool = False
    timeout_seconds: float = 10.0

    @property
    def base_url(self) -> str:
        if self.environment == "paper":
            return "https://openapivts.koreainvestment.com:29443"
        return "https://openapi.koreainvestment.com:9443"

    @classmethod
    def from_env(cls, dotenv_path: str | Path = ".env") -> "Settings":
        values = dict(os.environ)
        values.update(read_dotenv(dotenv_path))

        def value(name: str, default: str = "") -> str:
            return values.get(name) or default

        environment = value("KIS_ENV", "paper").lower()
        if environment not in {"paper", "live"}:
            raise ValueError("KIS_ENV는 paper 또는 live여야 합니다.")
        symbols = frozenset(
            item.strip().upper() for item in value("STOCKCAST_ALLOWED_SYMBOLS").split(",")
            if item.strip()
        )
        settings = cls(
            app_key=value("KIS_APP_KEY"),
            app_secret=value("KIS_APP_SECRET"),
            account_no=value("KIS_ACCOUNT_NO"),
            product_code=value("KIS_ACCOUNT_PRODUCT_CODE", "01"),
            environment=environment,
            allowed_symbols=symbols,
            max_order_krw=int(value("STOCKCAST_MAX_ORDER_KRW", "100000")),
            allow_live=value("STOCKCAST_ALLOW_LIVE", "false").lower() == "true",
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        missing = [name for name, value in {
            "KIS_APP_KEY": self.app_key,
            "KIS_APP_SECRET": self.app_secret,
            "KIS_ACCOUNT_NO": self.account_no,
        }.items() if not value]
        if missing:
            raise ValueError(f"필수 환경변수가 없습니다: {', '.join(missing)}")
        if len(self.account_no) != 8 or not self.account_no.isdigit():
            raise ValueError("KIS_ACCOUNT_NO는 계좌번호 앞 8자리여야 합니다.")
        if len(self.product_code) != 2 or not self.product_code.isdigit():
            raise ValueError("KIS_ACCOUNT_PRODUCT_CODE는 계좌번호 뒤 2자리여야 합니다.")
        if any(not is_domestic_symbol(symbol) for symbol in self.allowed_symbols):
            raise ValueError("STOCKCAST_ALLOWED_SYMBOLS는 6자리 영문·숫자 종목코드여야 합니다.")
        if self.max_order_krw <= 0:
            raise ValueError("STOCKCAST_MAX_ORDER_KRW는 양수여야 합니다.")
