"""Konfiguracja — pydantic-settings, wartości z `.env` lub zmiennych środowiskowych.

Zero kluczy w kodzie i w historii gita (SPEC §Jakość kodu). Wszystkie zmienne mają
prefiks `EMSCAN_`, wzorzec w `.env.example`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Ustawienia runtime. Puste klucze oznaczają źródło wyłączone, nie błąd."""

    model_config = SettingsConfigDict(
        env_prefix="EMSCAN_",
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- klucze źródeł ---
    finnhub_api_key: str | None = None
    tradier_api_key: str | None = None
    """Główne źródło łańcucha opcji i historii OHLC — patrz docs/PROBE-2026-08-13.md."""
    polygon_api_key: str | None = None

    tradier_sandbox: bool = True
    """Sandbox czy produkcja Tradiera. Sandbox ma darmowy klucz i opóźnione dane."""

    # --- ścieżki ---
    db_path: Path = Path("data/emscan.db")
    raw_dir: Path = Path("data/raw")

    # --- HTTP ---
    http_timeout: float = 20.0
    http_max_retries: int = 3
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 emscan/0.1"
    )

    # --- filtry uniwersum, SPEC §1.5 ---
    min_price: float = 5.0
    min_volume_20d: int = 500_000
    min_oi_atm: int = 100
    min_em_pct: float = Field(default=0.06, description="Minimalny EM w ułamku, nie w procentach")

    def resolved_db_path(self) -> Path:
        """Ścieżka bazy sprowadzona do bezwzględnej względem katalogu repo."""
        return self.db_path if self.db_path.is_absolute() else REPO_ROOT / self.db_path

    def resolved_raw_dir(self) -> Path:
        """Katalog surowych odpowiedzi sprowadzony do bezwzględnego."""
        return self.raw_dir if self.raw_dir.is_absolute() else REPO_ROOT / self.raw_dir


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Ustawienia jako singleton — czytane raz na proces."""
    return Settings()
