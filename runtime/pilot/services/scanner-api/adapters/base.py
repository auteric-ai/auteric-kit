from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from models import AdapterResult

ProgressCallback = Callable[[int, str], None]


class CommerceAdapter(ABC):
    name = "base"

    @abstractmethod
    async def scan(self, target_url: str, context: dict[str, Any], progress: ProgressCallback) -> AdapterResult:
        raise NotImplementedError
