from asyncio import Lock
from typing import Dict, Mapping

class MutexDict[K, V]:
    """
    Asynchronous mutex primitive based on asyncio.Lock
    
    Example:
        ```
        mutex = MutexDict()
        
        async with mutex as inner:
            inner["hello"] = "world"
        ```
    """
    
    _inner: Dict[K, V]
    _lock: Lock
    
    def __init__(self):
        self._inner = dict()
        self._lock = Lock()
    
    def read(self) -> Mapping[K, V]:
        """
        Get the inner value. By using this method, you promise not to mutate the inner value.
        """
        
        return self._inner
    
    async def __aenter__(self) -> Dict[K, V]:
        await self._lock.acquire()
        return self._inner
    
    async def __aexit__(self, *args):
        self._lock.release()