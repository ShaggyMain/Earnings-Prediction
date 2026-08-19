"""Bezpiecznik sieciowy z conftest.py — testuje sam siebie.

Bez tego pliku bezpiecznik byłby kodem, w którego działanie trzeba wierzyć. A jego zadaniem
jest łapanie sytuacji, w której ktoś (ja) zostawia w testach wywołanie komendy chodzącej po
prawdziwym API.
"""

from __future__ import annotations

import socket

import pytest


def test_outbound_connection_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="sieć w testach jest zabroniona"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("cdn.cboe.com", 443))


def test_the_message_says_what_to_do_instead() -> None:
    with pytest.raises(RuntimeError, match="respx albo atrapy"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("api.nasdaq.com", 443))


def test_localhost_stays_available() -> None:
    """Blokada nie może wywrócić niczego, co legalnie łączy się lokalnie."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect(("127.0.0.1", server.getsockname()[1]))
        client.close()
    finally:
        server.close()
