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
from wakapi_anyide.helpers.filediffer import FileMetadata
from wakapi_anyide.helpers.filediffer import process_file_change
from wakapi_anyide.models.environment import Environment
from wakapi_anyide.watchers.types import Event
from wakapi_anyide.watchers.types import Watcher

logger = logging.getLogger()

def bytes_to_human(size: int):
    if size == 0:
        return "(empty)"
    
    for i, suffix in enumerate(["B", "KiB", "MiB", "GiB", "TiB"]):
        if 1024**i < size < 1024**(i+1):
            return f"{(size / (1024**i)):.2f} {suffix}"
    
    return f"{(size / 1024**6):.2f} PiB"
    

def format_file(file: FileMetadata):
    color = "red" if file.binary else "bright_black"
    extra = "  [yellow]Binary file[/yellow]" if file.binary else ""
    if file.binary:
        return(f"{file.path}  [{color}]{bytes_to_human(file.linecount*100)}[/{color}]{extra}")# *100 is a compensation for the earlier measure to reduce number of psuedo lines
    else:
        return(f"{file.path}  [{color}]{file.linecount}[/{color}]{extra}")


class FileWatcher(Watcher):
    """
    Watches filesystem events for file changes.
    """
    
    current_file: FileMetadata | None = None
    env: Environment
    task: Task
    cache: Dict[str, FileMetadata] = dict()
    
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
    
        # add the Rust watcher
        watch = Watch()
        watch.add_watch("./", True)
        
        logger.info("Watched files:")
        
        for path in included_paths.match_tree('./'):
            resolved_path = self.normalise(Path('./').absolute() / Path(path))
            
            # skip adding the file because its on the naughty list
            if excluded_paths.match_file(resolved_path):
                continue
            
            file = await FileMetadata.read(resolved_path)
            #Determine all the file properties during reading
            # read file and add to the cache
            file = await File.read(resolved_path)
            
            self.cache[resolved_path] = await FileMetadata.read(resolved_path)
            logger.info(f"Test {format_file(self.cache[resolved_path])}")
    
        logger.info("Watching!")
    
        # all the events the Rust watcher returning
        async for ev_list in watch:
            for event in ev_list:
                logger.debug(f"Got event: {event}")
                resolved_path = self.normalise(Path(event.target))
                new_file = None # define the var for the whole loop iteration
                
                # check if we should ignore this file if its on the naughty list 
                if not included_paths.match_file(resolved_path) or excluded_paths.match_file(resolved_path):
                    continue
                
                # If the event is a delete event, overwrite the file with an empty file
                if event.kind == WatchEventType.Delete:
                    new_file = FileMetadata.empty(resolved_path)
                else:
                    # Else, read the current file
                    try:
                        new_file = await FileMetadata.read(resolved_path)
                    except OSError as e:
                        if not Path(resolved_path).is_dir():
                            logger.warning(f"Failed to open a file: {e} (maybe it was deleted very quickly)")
                        
                        continue
                
                # If this event path is not recognized in the cache, its a new file
                if self.cache.get(resolved_path) is None:
                    logger.info(f"New file found: {format_file(new_file)} ")
                    
                    # add the file to the cache only for the diffing, making sure it is empty
                    self.cache[resolved_path] = File.empty(resolved_path)
            
                # Now, add the file to be processed
                self.current_file = new_file
        
    async def setup(self, tg: TaskGroup, emit_event: Queue[Event]):
        self.task = tg.create_task(self._task(emit_event))
    
    async def shutdown(self):
        pass
    
    async def resolve_events(self) -> AsyncGenerator[Event, None]:
        # For every event in this watchers list, process the changes and compare
        if self.current_file is not None:
            event = process_file_change(
                new_file=self.current_file,
                old_file=self.cache[self.current_file.path],
                time=time.time(),
                env=self.env
            )
            
            # Return the event, this is produces a generator, so resolve events can be iteratored
            if event is not None:
                yield event
            
            # Clear the current file and reset the cache to the latest file.
            self.cache[self.current_file.path] = self.current_file
            self.current_file = None
            self.current_file_checksum = None
