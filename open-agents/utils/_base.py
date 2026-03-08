"""Base types for open-agents tools and inputs."""
from dataclasses import dataclass, asdict, fields
from typing import Any


@dataclass
class ToolResult:
    """Base class for all tool and input return values.

    Subclasses define their fields as dataclass fields.
    The graph runner converts instances to dicts via to_dict().
    """

    def to_dict(self) -> dict:
        result = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if isinstance(val, ToolResult):
                result[f.name] = val.to_dict()
            else:
                result[f.name] = val
        return result
