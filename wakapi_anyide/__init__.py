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
from os import uname
from hashlib import sha256
import base64
import re
import time

from wakapi_anyide.models.environment import Environment


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
        
        # Simplified process, we don't actually need to track individual changes and compare all the lines, all we need to know is if it changed.
        # At some point I will add a mechanism for calculating md5 hash of the file and compare it to the past 10 file hashes over a long time range to verify someone
        # isn't just spamming the same file over and over.
        
        new_file_lines = 0
        new_file_lines = new_file.splitlines()
        old_file_lines = 0
        old_file_lines = old_file.splitlines()
        
        changed_lines = 0
        changed_lines = abs(len(new_file_lines) - len(old_file_lines)) 
        
        if new_file == old_file: # This should never happen
            print ("file did not change")
            
        if new_file != old_file:
            print ("file changed")
            
        # There is a bug with the cache. It correctly knows when the file has changed, but it doesn't update the cache to remove the changed lines.
        # It knows when a file has changed and when it hasn't, but the cache file contents aren't updated and its always comparing to that. Security measure perhaps?
        
        # Need to include a mechanism to drop the change event when the file hasn't changed at all
        if changed_lines == 0 and new_file != old_file: 
            changed_lines = 7 # random placeholder value, we don't actually care how many lines changed from the diff   
        
        return ChangeEvent(
            filename=event.filename,
            file=new_file,
            cursor=(0, 0),
            lines_added=changed_lines,
            lines_removed=changed_lines,
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
        
        host = uname()
        user_agent = f"wakatime/unset ({host.sysname}-none-none) wakapi-anyide-wakatime/unset"
        
        heartbeats = [{
            "entity": event.filename,
            "type": "file",
            "category": "coding",
            "time": event.time,
            "project": env.project.project.name,
            "language": Path(event.filename).suffix.replace('.', '') or Path(event.filename).name,
            "lines": event.lines,
            "line_additions": event.lines_added,
            "line_deletions": event.lines_removed,
            "lineno": event.cursor[0],
            "cursorpos": event.cursor[1],
            "is_write": True,
            "editor": "wakapi-anyide",
            "machine": env.config.settings.hostname or f"anonymised machine {sha256(host.nodename.encode()).hexdigest()[:8]}",
            "operating_system": host.sysname,
            "user_agent": user_agent
        } for event in changed_events]
        # print("heartbeats", f"{heartbeats}")
        
        response: ClientResponse
        last_text: str | None = None
        
        if env.is_test_only:
            print(f"Would've sent heartbeat, but in testing mode")
            continue
            
        for i in range(3):
            print(f"Sending heartbeat (attempt {i+1})")
            async with request("POST", f"{env.config.settings.api_url}/users/current/heartbeats.bulk", json=heartbeats, headers={
                "User-Agent": user_agent,
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