import logging
import time
from asyncio import Task
from asyncio.queues import Queue
from asyncio.taskgroups import TaskGroup
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Dict

from aiofiles import open
from pathspec import PathSpec

from wakapi_anyide._rust.watch import Watch
from wakapi_anyide._rust.watch import WatchEventType
from wakapi_anyide.helpers.filediffer import process_file_change
from wakapi_anyide.models.environment import Environment
from wakapi_anyide.watchers.types import Event
from wakapi_anyide.watchers.types import Watcher

logger = logging.getLogger()


class FileWatcher(Watcher):
    """
    Watches filesystem events for file changes.
    """
    
    current_file: str | None = None
    current_file_bytes: bytes | None = None
    env: Environment
    task: Task
    cache: Dict[str, bytes] = dict()
    
    def __init__(self, env: Environment):
        self.env = env
        
    @staticmethod
    def normalise(path: Path):
        return f"./{(path.relative_to(Path('./').absolute()))}"
        
    async def _task(self, queue: Queue[Event]):
        excluded_pathspecs = self.env.project.files.exclude.copy()
    
        for file in self.env.project.files.exclude_files:
            async with open(file, 'r') as file:
                excluded_pathspecs.extend(await file.readlines())
    
        included_paths = PathSpec.from_lines('gitwildmatch', self.env.project.files.include)
        excluded_paths = PathSpec.from_lines('gitwildmatch', excluded_pathspecs)
    
        watch = Watch()
        watch.add_watch("./", True)
        
        logger.info("Watched files:")
        
        for path in included_paths.match_tree('./'):
            resolved_path = self.normalise(Path('./').absolute() / Path(path))
            
            if excluded_paths.match_file(resolved_path):
                return
            
            logger.info(f"- {resolved_path}")
            
            async with open(resolved_path, 'rb') as file:
                self.cache[resolved_path] = sha256(await file.read()).digest()
    
        logger.info("Watching!")
    
        async for ev_list in watch:
            for event in ev_list:
                logger.debug(f"Got event: {event}")
                resolved_path = self.normalise(Path(event.target))
    
                if not included_paths.match_file(resolved_path) or excluded_paths.match_file(resolved_path):
                    continue
                
                if event.kind == WatchEventType.Delete:
                    if self.cache.get(resolved_path) is None:
                        logger.warning(f"Deletion event for {resolved_path} ignored because the file was not tracked")
                        continue
                    
                    event = process_file_change(
                        filename=resolved_path,
                        new_file=b"",
                        old_file=self.cache[resolved_path],
                        time=time.time(),
                        env=self.env
                    )
                    
                    # Deleting the file from cache
                    del self.cache[resolved_path]
                    print(f"Deleted {event.filename} from cache")
                    
                    if event is not None:
                        await queue.put(event)
    
                    continue
    
                if self.cache.get(resolved_path) is None:
                    logger.info(f"Watching {resolved_path}")
                    self.cache[resolved_path] = b""
    
                try:
                    async with open(resolved_path, 'rb') as file:
                        if self.current_file is None:
                            self.current_file = resolved_path
                            self.current_file_checksum = sha256(await file.read()).digest()
                        
                        if self.current_file != resolved_path:
                            event = process_file_change(
                                filename=resolved_path,
                                new_file=sha256(await file.read()).digest(),
                                old_file=self.cache[resolved_path],
                                time=time.time(),
                                env=self.env
                            )
                            
                            if event is not None:
                                await queue.put(event)
                                
                            self.current_file = resolved_path
                            self.current_file_checksum = sha256(await file.read()).digest()
                        
                        # Update the file in the cache
                        self.cache[resolved_path] = sha256(await file.read()).digest()
                
                except OSError as e:
                    if Path(resolved_path).is_dir():
                        continue
    
                    logger.warning(f"Failed to open a file: {e} (maybe it was deleted very quickly)")

        
    async def setup(self, tg: TaskGroup, emit_event: Queue[Event]):
        self.task = tg.create_task(self._task(emit_event))
    
    async def shutdown(self):
        pass
    
    async def resolve_events(self) -> AsyncGenerator[Event, None]:
        if self.current_file is not None:
            assert self.current_file_checksum
            
            event = process_file_change(
                filename=self.current_file,
                new_file=self.current_file_checksum,
                old_file=self.cache[self.current_file],
                time=time.time(),
                env=self.env
            )
            
            if event is not None:
                yield event
            
            self.current_file = None
            self.current_file_checksum = None
