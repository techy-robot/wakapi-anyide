import asyncio 
from typing import Awaitable
from typing import Callable
from typing import Collection
from typing import List


async def asyncmap[A, B](callable: Callable[[A], Awaitable[B]], iterable: Collection[A]) -> List[B]:
    """
    Asynchronously map callable over iterable and returns the result as a list.
    No ordering guarantees.
    """
    
    target: List[None | B] = [None] * len(iterable)
    
    async with asyncio.TaskGroup() as tg:
        async def _inner(i, value):
            target[i] = await callable(value)
        
        for i, value in enumerate(iterable):
            tg.create_task(_inner(i, value))
    
    return target  # type: ignore
