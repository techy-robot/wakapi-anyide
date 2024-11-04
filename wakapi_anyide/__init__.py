import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Dict, Iterator, List, Pattern, Set, Tuple
from asyncinotify import Inotify, Mask
from pathlib import Path
from os.path import dirname
from itertools import pairwise, chain
from aiohttp import BasicAuth, request, ClientResponse
import difflib
import wakapi_anyide.backport.glob as glob
from wakapi_anyide.helpers.mutex import MutexDict
from wakapi_anyide.helpers.asynctools import asyncmap
from aiofiles import open
from functools import partial
import base64
import re
import time

from wakapi_anyide.models.environment import Environment

USER_AGENT = "wakatime/unset (none-none-none) wakapi-anyide-wakatime/unset"


@dataclass
class UnresolvedChangeEvent:
    filename: str
    file: str
    time: float


@dataclass
class ChangeEvent:
    filename: str
    file: str
    cursor: Tuple[int, int]
    lines_added: int
    lines_removed: int
    lines: int
    time: float


async def main(env: Environment):
    ev = asyncio.get_event_loop()
    queue: asyncio.Queue[UnresolvedChangeEvent] = asyncio.Queue()
    cache: MutexDict[str, str] = MutexDict()
    
    ev.create_task(watcher(env, queue, cache))
    await consumer(env, queue, cache)
    

def process_file_changes(cache: Dict[str, str]):
    async def inner(event: UnresolvedChangeEvent):
        new_file = event.file
        old_file = cache[event.filename]
        
        last_index = 0
        for op in difflib.SequenceMatcher(a=old_file, b=new_file, autojunk=False).get_opcodes():
            match op:
                case ('replace', _, _, _, j2):
                    last_index = max(last_index, j2)
                case ('delete', i1, _, _, _):
                    last_index = max(last_index, i1)
                case ('insert', i1, _, j1, j2):
                    last_index = max(last_index, j2)
                case ('equal', _, _, _, _):
                    pass
                case _:
                    raise Exception(f"Unknown opcode {op}")
        
        new_file_lines = new_file.splitlines()
        added_lines = 0
        deleted_lines = 0
        for op in difflib.SequenceMatcher(a=old_file.splitlines(), b=new_file_lines, autojunk=False).get_opcodes():
            match op:
                case ('replace', i1, i2, j1, j2):
                    added_lines += j2 - j1
                    deleted_lines += i2 - i1
                case ('delete', i1, i2, _, _):
                    deleted_lines += i2 - i1
                case ('insert', _, _, j1, j2):
                    added_lines += j2 - j1
                case ('equal', _, _, _, _):
                    pass
                case _:
                    raise Exception(f"Unknown opcode {op}")
        
        line, col = index_to_linecol(new_file, last_index)
        
        return ChangeEvent(
            filename=event.filename,
            file=new_file,
            cursor=(line, col),
            lines_added=added_lines,
            lines_removed=deleted_lines,
            lines=len(new_file_lines),
            time=event.time
        )
    
    return inner


async def consumer(env: Environment, queue: asyncio.Queue[UnresolvedChangeEvent], cache_lock: MutexDict[str, str]):
    next_heartbeat_due = time.time() + env.config.settings.heartbeat_rate_limit_seconds
    changed_files: Dict[str, UnresolvedChangeEvent] = dict()
    
    while True:
        while next_heartbeat_due - time.time() > 0:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=next_heartbeat_due - time.time())
                changed_files[event.filename] = event
            except TimeoutError:
                break
                
        next_heartbeat_due = time.time() + env.config.settings.heartbeat_rate_limit_seconds
                
        if len(changed_files) == 0:
            print(f"No changes detected.")
            continue
        
        async with cache_lock as cache:
            changed_events = await asyncmap(process_file_changes(cache), changed_files.values())
        changed_files.clear()
        
        print(f"Change summary:")
        for event in changed_events:
            print(f"{event.filename:20} at {event.cursor[0]}:{event.cursor[1]} +{event.lines_added} -{event.lines_removed}")
            
        heartbeats = [{
            "entity": event.filename,
            "type": "file",
            "category": "coding",
            "time": event.time,
            "project": "wakatime-anyide",
            "language": Path(event.filename).suffix.replace('.', '') or Path(event.filename).name,
            "lines": event.lines,
            "line_additions": event.lines_added,
            "line_deletions": event.lines_removed,
            "lineno": event.cursor[0],
            "cursorpos": event.cursor[1],
            "is_write": True,
            "editor": "wakapi-anyide",
            "operating_system": "wakapi-anyide",
            "user_agent": USER_AGENT
        } for event in changed_events]
        
        response: ClientResponse
        last_text: str | None = None
        
        if env.is_test_only:
            print(f"Would've sent heartbeat, but in testing mode")
            continue
            
        for i in range(3):
            print(f"Sending heartbeat (attempt {i+1})")
            async with request("POST", f"{env.config.settings.api_url}/users/current/heartbeats.bulk", json=heartbeats, headers={
                "User-Agent": USER_AGENT,
                "Authorization": f"Basic {base64.b64encode(env.config.settings.api_key.encode()).decode()}"
            }) as response:
                if response.status == 201:
                    break
                else:
                    last_text = await response.text()
        else:
            raise Exception(f"Failed to send heartbeat: {response.status} {last_text}")


def normalise(path):
    return f"./{(path.relative_to(Path('./').absolute()))}"


def index_to_linecol(file: str, index: int):
    line = 0
    col = 0
    
    for character in file[:index]:
        if character == '\n':
            line += 1
            col = 0
        else:
            col += 1
    
    return line + 1, col + 1


def directories(path):
    assert path.is_dir()
    
    stack = deque()
    stack.append(path)
    while stack:
        current_path = stack.pop()
        yield current_path
        for subpath in current_path.iterdir():
            if subpath.is_dir():
                stack.append(subpath)


def is_included(path: str, included: List[Pattern], excluded: List[Pattern]):
    return any(pattern.match(path) for pattern in included) and not any(pattern.match(path) for pattern in excluded)


async def watcher(env: Environment, queue: asyncio.Queue[UnresolvedChangeEvent], cache_lock: MutexDict[str, str]):
    included_paths_globs = env.project.files.include
    excluded_paths_globs = env.project.files.exclude
    
    included_paths_re = [re.compile(glob.translate(path_glob, recursive=True)) for path_glob in included_paths_globs]
    excluded_paths_re = [re.compile(glob.translate(path_glob, recursive=True)) for path_glob in excluded_paths_globs]
    
    included_paths = set(chain.from_iterable((Path(x).absolute() for x in glob.glob(path_glob, recursive=True)) for path_glob in included_paths_globs))
    excluded_paths = set(chain.from_iterable((Path(x).absolute() for x in glob.glob(path_glob, recursive=True)) for path_glob in excluded_paths_globs))
    
    included_directories = set(dirname(x) for x in included_paths) - set(dirname(x) for x in excluded_paths)
    
    async with cache_lock as cache: 
        async def add_cache(path: Path):
            if path in excluded_paths:
                return
            
            async with open(path, 'r+') as file:
                cache[normalise(path)] = await file.read()
                print(f"Cached {normalise(path)}")
        
        await asyncio.gather(*[add_cache(x) for x in included_paths])
    
    mask = Mask.CLOSE_WRITE | Mask.CREATE | Mask.DELETE | Mask.MOVE
    with Inotify() as inotify:
        for directory in directories(Path("./")):
            inotify.add_watch(directory, mask)
        
        async for event in inotify:
            if event.path is None:
                continue
                
            resolved_path = f"./{event.path}"
            
            if Mask.ISDIR in event.mask:
                if Mask.CREATE in event.mask or Mask.MOVED_TO in event.mask:
                    # When a directory is created or moved here:
                    print(f"Directory moved or created here: {resolved_path}")
                    for directory in directories(event.path):
                        inotify.add_watch(directory, mask)
                if Mask.MOVED_FROM in event.mask:
                    # When a directory is moved out of here:
                    print(f"Directory moved out of here: {resolved_path}")
                    watches = [watch for watch in inotify._watches.values() if watch.path.is_relative_to(event.path)]
                    for watch in watches:
                        inotify.rm_watch(watch)
                
                continue
            else:
                if Mask.MOVED_FROM in event.mask or Mask.DELETE in event.mask:
                    # When a file is moved out or deleted:
                    if is_included(resolved_path, included_paths_re, excluded_paths_re):   
                        print(f"File moved out or deleted: {resolved_path}")
                        await queue.put(UnresolvedChangeEvent(
                            filename=resolved_path,
                            file="",
                            time=time.time()
                        ))
                    continue
                
            if is_included(resolved_path, included_paths_re, excluded_paths_re):  
                async with open(event.path, 'r') as file:
                    file_contents = await file.read()
                    
                if Mask.CREATE in event.mask or Mask.MOVED_TO in event.mask:
                    # When a file is created or moved here:
                    print(f"File moved or created here: {resolved_path}")
                    async with cache_lock as cache:
                        cache[resolved_path] = file_contents
                
                await queue.put(UnresolvedChangeEvent(
                    filename=resolved_path,
                    file=file_contents,
                    time=time.time()
                ))