"""Typed local capabilities exposed by the outbound Relay agent."""

from .base import InvokeMessage, LocalCapability
from .computer import ComputerCapability, get_cua_driver_path
from .system import SystemCapability
from .terminal import TerminalCapability

__all__ = [
    "ComputerCapability",
    "InvokeMessage",
    "LocalCapability",
    "SystemCapability",
    "TerminalCapability",
    "get_cua_driver_path",
]
