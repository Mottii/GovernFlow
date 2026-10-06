from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    cache_write_hour: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
