"""
tools.py — Wraps OpenMontage BaseTool subclasses into Claude API tool definitions.

Discovery flow:
  1. sys.path-inject OPEN_MONTAGE_PATH so OpenMontage imports work
  2. Call ToolRegistry.discover() to auto-find all BaseTool subclasses
  3. Convert each tool's input_schema + docstring into a Claude tools[] entry
  4. execute(name, params) routes to the right tool's .execute()
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

# ── Inject OpenMontage path ──────────────────────────────────────────
OPEN_MONTAGE_PATH = os.environ.get(
    "OPEN_MONTAGE_PATH",
    str(Path(__file__).resolve().parents[2] / "OpenMontage"),  # default: sibling dir
)
if OPEN_MONTAGE_PATH not in sys.path:
    sys.path.insert(0, OPEN_MONTAGE_PATH)

# ── Import OpenMontage registry (lazy, so import errors surface clearly)
try:
    from tools.tool_registry import ToolRegistry as _OMRegistry  # noqa: E402
    from tools.base_tool import ToolResult                        # noqa: E402
    _OM_AVAILABLE = True
except ImportError as e:
    print(f"[tools] WARNING: Cannot import OpenMontage ({e}). Using stub mode.", flush=True)
    _OM_AVAILABLE = False


# ── Tools that are too slow/expensive for WhatsApp context ──────────
# These are excluded from the tool list to prevent the agent from calling
# them without explicit user confirmation (handled by approval gate instead).
_EXCLUDE_TOOLS: set[str] = set()

# ── Max description length for Claude tool definitions ───────────────
_MAX_DESC = 400


class ToolRegistry:
    """
    Wraps OpenMontage's tool registry for use by the Claude API agent.
    """

    def __init__(self) -> None:
        self._om_registry = None
        self._tools: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not _OM_AVAILABLE:
            print("[tools] OpenMontage not available — no tools loaded.", flush=True)
            return

        registry = _OMRegistry()
        registry.discover()
        self._om_registry = registry

        for tool in registry.get_available():
            name = tool.name
            if name in _EXCLUDE_TOOLS:
                continue
            self._tools[name] = tool

        print(f"[tools] loaded {len(self._tools)} OpenMontage tools", flush=True)

    # ── Claude API tool definitions ──────────────────────────────────
    def as_claude_tools(self) -> list[dict]:
        definitions = []
        for name, tool in self._tools.items():
            # Build description from docstring + provider info
            raw_doc = (tool.__class__.__doc__ or "").strip().split("\n")[0]
            provider = getattr(tool, "provider", "")
            desc = f"[{provider}] {raw_doc}" if provider else raw_doc
            desc = desc[:_MAX_DESC]

            # Use the tool's declared input_schema if present
            input_schema = getattr(tool, "input_schema", None) or {
                "type": "object",
                "properties": {},
            }

            definitions.append({
                "name": name,
                "description": desc,
                "input_schema": input_schema,
            })
        return definitions

    # ── Execute a named tool ─────────────────────────────────────────
    def execute(self, name: str, params: dict) -> dict:
        if name not in self._tools:
            raise ValueError(f"Tool '{name}' not found in registry")

        tool = self._tools[name]
        result: ToolResult = tool.execute(params)

        if not result.success:
            raise RuntimeError(result.error or f"Tool {name} failed")

        # Flatten ToolResult into a plain dict for JSON serialisation
        out: dict = {}
        if result.data:
            out.update(result.data)
        if result.artifacts:
            # artifacts is a list of file paths; expose as output_path (last one)
            out["output_path"] = result.artifacts[-1]
            out["artifacts"] = result.artifacts
        if result.cost_usd is not None:
            out["cost_usd"] = result.cost_usd
        return out

    # ── Convenience ─────────────────────────────────────────────────
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())
