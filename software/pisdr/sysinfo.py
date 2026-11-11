"""Small system facts for the Settings info panel and the network-mode page."""
from __future__ import annotations

import importlib.util
import shutil
import socket


def lan_address() -> str:
    """Best guess at the Pi's LAN IP (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def hostname() -> str:
    return socket.gethostname()


def have_tool(name: str) -> bool:
    return shutil.which(name) is not None


def have_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False
