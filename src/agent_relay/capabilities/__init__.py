"""Typed local capabilities exposed by the outbound Relay agent."""

from .base import InvokeMessage, LocalCapability
from .browser import BrowserCapability
from .system import SystemCapability
from .terminal import TerminalCapability

__all__ = ["BrowserCapability", "InvokeMessage", "LocalCapability", "SystemCapability", "TerminalCapability"]
