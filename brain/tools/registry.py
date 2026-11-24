from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from brain.tools.base import Tool


class ToolRegistry:
    """Deterministic tool registry keeping OpenAI tool schemas sorted by name for KV Prefix-Cache stability."""

    def __init__(self, tools: Optional[Iterable[Tool]] = None) -> None:
        self._tools: Dict[str, Tool] = {}
        if tools is not None:
            for t in tools:
                self.register(t)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> Optional[Tool]:
        return self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self) -> List[Tool]:
        return [self._tools[name] for name in sorted(self._tools.keys())]

    def to_openai_tools(self) -> List[Dict[str, object]]:
        """Export OpenAI/DeepSeek function tool schemas deterministically sorted by name."""
        return [self._tools[name].to_openai_schema() for name in sorted(self._tools.keys())]

    def __len__(self) -> int:
        return len(self._tools)
