"""Smoke test — pilnuje, żeby pakiet się importował i CI miało co uruchomić.

Zostanie zastąpiony realnymi testami w kroku 3 (patrz docs/PLAN-faza-1.md).
"""

import emscan


def test_package_imports() -> None:
    assert emscan.__version__ == "0.1.0"
