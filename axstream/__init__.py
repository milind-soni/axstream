"""axstream — fast, verified computer use that compiles.

The surface is small on purpose: a declarative macro format (macrofile),
a verified click ladder and replay engine (replay), the verify-before-trust
gate (gate), and the driver socket (driver). Agents consume it through the
CLI and the MCP server; apps read the same .axstream files.
"""
from .macrofile import MacroFile, discover, load, parse, resolve_name, save

__all__ = ["MacroFile", "discover", "load", "parse", "resolve_name", "save"]
