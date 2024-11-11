from enum import IntEnum
from os import PathLike
from typing import List, Self


class WatchEventType(IntEnum):  # undocumented
    """
    A type of watch event.
    """
    
    Create = 0
    Delete = 1
    Modify = 2


class WatchEvent:  # undocumented
    """
    A watch event.
    """
    
    kind: WatchEventType
    target: str


class Watch:  # undocumented
    """
    Watch a directory and its subdirectory for changes.
    """
    
    def __init__(self): ...
    def add_watch(self, to_watch: PathLike | str, is_recursive: bool): ...
    def __aiter__(self) -> Self: ...
    async def __anext__(self) -> List[WatchEvent]: ...