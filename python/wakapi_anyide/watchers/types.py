import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from typing import Tuple

from wakapi_anyide.models.environment import Environment


@dataclass
class Event:
    filename: str
    file_extension: str
    cursor: Tuple[int, int]
    lines_added: int
    lines_removed: int
    lines: int
    time: float


class Watcher(Protocol):
    """
    A watcher implements this protocol.
    A watcher can either choose to emit events in realtime with the emit_event queue, or use resolve_events as a generator.
    """
    
    def __init__(self, env: Environment):
        raise NotImplementedError
    
    async def setup(self, tg: asyncio.TaskGroup, emit_event: asyncio.Queue[Event]):
        """
        Create long-running tasks with the tg TaskGroup.
        """
        return
    
    async def shutdown(self):
        """
        Teardown any long-running tasks here.
        """
        return
    
    def resolve_events(self) -> AsyncIterator[Event] | None:
        return
