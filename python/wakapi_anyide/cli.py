import asyncio
import base64
import difflib
import os
import re
import time
import copy
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
from itertools import chain
from itertools import pairwise
from os.path import dirname
from pathlib import Path
from platform import uname
from re import Pattern
from typing import Dict
from typing import List
from typing import Set
from typing import Tuple

from aiofiles import open
from aiohttp import BasicAuth
from aiohttp import ClientResponse
from aiohttp import request
from pathspec import PathSpec
from wakapi_anyide._watch import Watch
from wakapi_anyide._watch import WatchEventType
from wakapi_anyide.helpers.asynctools import asyncmap
from wakapi_anyide.helpers.mutex import MutexDict
from wakapi_anyide.models.environment import Environment


class ConfigInvalidatedException(BaseException):
    pass


@dataclass
class UnresolvedChangeEvent:
    filename: str
    file: bytes
    time: float


@dataclass
class ChangeEvent:
    filename: str
    checksum: bytes
    lines: int
    time: float


async def main(env: Environment):
    ev = asyncio.get_event_loop()
    queue: asyncio.Queue[UnresolvedChangeEvent] = asyncio.Queue()
    cache: MutexDict[str, bytes] = MutexDict()
    shutdown_flag = asyncio.Event()

    fut = ev.create_task(consumer(env, queue, shutdown_flag, cache))
    try:
        await watcher(env, queue, cache)
    finally:
        print("Shutting down!")
        shutdown_flag.set()
        await fut

def process_file_changes(cache: Dict[str, bytes], ignore_binary: bool):
    async def inner(event: UnresolvedChangeEvent) -> ChangeEvent | None:
        new_checksum = sha256(event.file).digest()
        old_checksum = copy.deepcopy(cache[event.filename])
        
        # Updating the cache
        
        if len(event.file) == 0:# Delete item from cache if the new file is empty, because there is no reason keeping the old
            del cache[event.filename]
            print(f"Deleted {event.filename} from cache")
        else:# Update the cache for the file
            cache[event.filename] = new_checksum

        #Process text files and finding line changes

        # compare file checksums
        if new_checksum != old_checksum:
            print(f"File modified: {event.filename}")
            return

        new_file_lines = new_file.splitlines()

        return ChangeEvent(
            filename=event.filename,
            checksum=new_checksum,
            lines=len(new_file_lines),
            time=event.time
        )

    return inner


async def consumer(env: Environment, queue: asyncio.Queue[UnresolvedChangeEvent], flag: asyncio.Event, cache_lock: MutexDict[str, bytes]):
    next_heartbeat_due = time.time() + env.config.settings.heartbeat_rate_limit_seconds
    changed_files: Dict[str, UnresolvedChangeEvent] = dict()
    fut: asyncio.Future[UnresolvedChangeEvent] | None = None

    while not flag.is_set():
        while next_heartbeat_due - time.time() > 0 and not flag.is_set():
            if fut is None or fut.done():
                fut = asyncio.create_task(queue.get())
            completed, rest = await asyncio.wait([
                fut,
                asyncio.create_task(flag.wait())
            ], return_when=asyncio.FIRST_COMPLETED, timeout=next_heartbeat_due - time.time())  # type: ignore
            
            if fut in completed:
                event = fut.result()

                if env.is_test_only:
                    print(f"Got event for {event.filename}!")

                changed_files[event.filename] = event

        next_heartbeat_due = time.time() + env.config.settings.heartbeat_rate_limit_seconds

        if len(changed_files) == 0:
            print(f"No changes detected.")
            continue

        async with cache_lock as cache:
            changed_events = [x for x in await asyncmap(
                process_file_changes(cache, env.project.files.exclude_binary_files),
                changed_files.values()
            ) if x is not None]
        changed_files.clear()

        print(f"Change summary:")
        for event in changed_events:
            print(f"{event.filename:20} checksum: {event.checksum}, current line count: {event.lines}")

        host = uname()
        user_agent = f"wakatime/unset ({host.system}-none-none) wakapi-anyide-wakatime/unset"

        heartbeats = [{
            "entity": event.filename,
            "type": "file",
            "category": "coding",
            "time": event.time,
            "project": env.project.project.name,
            **({"language": languageProcessor(env, event.filename)} if languageProcessor(env, event.filename) is not None else {}), # dict comprehension, only include language if it's custom defined, otherwise let Wakatime determine
            "lines": event.lines,
            "is_write": True,
            "editor": "wakapi-anyide",
            "machine": env.config.settings.hostname or f"anonymised machine {sha256(host.node.encode()).hexdigest()[:8]}",
            "operating_system": host.system,
            "user_agent": user_agent
        } for event in changed_events]

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

def languageProcessor(env: Environment, filename: str):
    try: 
        languages = env.project.files.language_mapping
    except AttributeError:
        return None
    
    suffix = Path(filename).suffix
    for x, lang in languages.items():
        if suffix == x: # If the suffix matches the defined one in the languages table
            return lang
    return None

def normalise(path: Path):
    return f"./{(path.relative_to(Path('./').absolute()))}"


def directories(path: Path):
    if not path.exists() or not path.is_dir():
        return

    stack = deque()
    stack.append(path)
    while stack:
        current_path = stack.pop()
        yield current_path
        for subpath in current_path.iterdir():
            if subpath.is_dir():
                stack.append(subpath)


async def watcher(env: Environment, queue: asyncio.Queue[UnresolvedChangeEvent], cache_lock: MutexDict[str, bytes]):
    excluded_pathspecs = env.project.files.exclude.copy()

    for file in env.project.files.exclude_files:
        async with open(file, 'r') as file:
            excluded_pathspecs.extend(await file.readlines())

    included_paths = PathSpec.from_lines('gitwildmatch', env.project.files.include)
    excluded_paths = PathSpec.from_lines('gitwildmatch', excluded_pathspecs)

    async with cache_lock as cache:
        async def add_cache(path: Path):
            if excluded_paths.match_file(path):
                return
            # open file and calculate checksum and add to the cache
            async with open(path, 'rb') as file:
                cache[normalise(path)] = sha256(await file.read()).digest()
                print(f"Cached {normalise(path)}")

        await asyncio.gather(*[add_cache(Path('./').absolute() / Path(x)) for x in included_paths.match_tree('./')])

    watch = Watch()
    watch.add_watch("./")

    print("Watching!")

    async for ev_list in watch:
        for event in ev_list:
            resolved_path = normalise(Path(event.target))
            
            if resolved_path == "./wak.toml":
                raise ConfigInvalidatedException("config invalidated")

            if not included_paths.match_file(resolved_path) or excluded_paths.match_file(resolved_path):
                continue
            
            if event.kind == WatchEventType.Delete:
                if cache_lock.read().get(resolved_path) is None:
                    print(f"Not in cache, so deletion for {resolved_path} ignored")
                    continue
                
                await queue.put(UnresolvedChangeEvent(
                    filename=resolved_path,
                    file=b"",
                    time=time.time()
                ))

                continue

            if cache_lock.read().get(resolved_path) is None:
                async with cache_lock as cache:
                    print(f"Cached {resolved_path}")
                    cache[resolved_path] = b""

            try:
                async with open(resolved_path, 'rb') as file:
                    await queue.put(UnresolvedChangeEvent(
                        filename=resolved_path,
                        file=await file.read(),
                        time=time.time()
                    ))
            except OSError as e:
                if Path(resolved_path).is_dir():
                    continue

                print(f"Failed to open a file: {e} (maybe it was deleted very quickly)")
