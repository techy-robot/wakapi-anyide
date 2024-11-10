import asyncio
import base64
import difflib
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
from itertools import chain
from itertools import pairwise
from os.path import dirname
from pathlib import Path
from platform import uname
from typing import Dict
from typing import Iterator
from typing import List
from typing import Pattern
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


@dataclass
class UnresolvedChangeEvent:
    filename: str
    file: bytes
    time: float


@dataclass
class ChangeEvent:
    filename: str
    file: bytes
    cursor: Tuple[int, int]
    lines_added: int
    lines_removed: int
    lines: int
    time: float


async def main(env: Environment):
    ev = asyncio.get_event_loop()
    queue: asyncio.Queue[UnresolvedChangeEvent] = asyncio.Queue()
    cache: MutexDict[str, bytes] = MutexDict()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(watcher(env, queue, cache))
        tg.create_task(consumer(env, queue, cache))


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


def process_file_changes(cache: Dict[str, bytes], ignore_binary: bool):
    async def inner(event: UnresolvedChangeEvent) -> ChangeEvent | None:
        new_file = event.file
        old_file = cache[event.filename]

        try:
            new_file = new_file.decode()
            old_file = old_file.decode()

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
                file=event.file,
                cursor=(line, col),
                lines_added=added_lines,
                lines_removed=deleted_lines,
                lines=len(new_file_lines),
                time=event.time
            )
        except UnicodeDecodeError:
            if ignore_binary:
                return

            added_lines = 0
            deleted_lines = 0
            last_index = 0
            for op in difflib.SequenceMatcher(a=old_file, b=new_file, autojunk=False).get_opcodes():
                match op:
                    case ('replace', i1, i2, j1, j2):
                        added_lines += j2 - j1
                        deleted_lines += i2 - i1
                        last_index = max(last_index, j2)
                    case ('delete', i1, i2, _, _):
                        deleted_lines += i2 - i1
                        last_index = max(last_index, i1)
                    case ('insert', _, _, j1, j2):
                        added_lines += j2 - j1
                        last_index = max(last_index, j2)
                    case ('equal', _, _, _, _):
                        pass
                    case _:
                        raise Exception(f"Unknown opcode {op}")

            return ChangeEvent(
                filename=f"{event.filename}#wakapi-anyide-binaryfile",
                file=event.file,
                cursor=(1, last_index),
                lines_added=added_lines,
                lines_removed=deleted_lines,
                lines=len(new_file),
                time=event.time
            )

    return inner


async def consumer(env: Environment, queue: asyncio.Queue[UnresolvedChangeEvent], cache_lock: MutexDict[str, bytes]):
    next_heartbeat_due = time.time() + env.config.settings.heartbeat_rate_limit_seconds
    changed_files: Dict[str, UnresolvedChangeEvent] = dict()

    while True:
        while next_heartbeat_due - time.time() > 0:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=next_heartbeat_due - time.time())

                if env.is_test_only:
                    print(f"Got event for {event.filename}!")

                changed_files[event.filename] = event
            except TimeoutError:
                break

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
            print(f"{event.filename:20} at {event.cursor[0]}:{event.cursor[1]} +{event.lines_added} -{event.lines_removed}")

        host = uname()
        user_agent = f"wakatime/unset ({host.system}-none-none) wakapi-anyide-wakatime/unset"

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

            async with open(path, 'rb') as file:
                cache[normalise(path)] = await file.read()
                print(f"Cached {normalise(path)}")

        await asyncio.gather(*[add_cache(Path('./').absolute() / Path(x)) for x in included_paths.match_tree('./')])

    watch = Watch()
    watch.add_watch("./")

    print("Watching!")

    async for ev_list in watch:
        for event in ev_list:
            resolved_path = normalise(Path(event.target))

            if not included_paths.match_file(resolved_path) or excluded_paths.match_file(resolved_path):
                continue

            match event.kind:
                case WatchEventType.Delete:
                    if cache_lock.read().get(resolved_path) is None:
                        continue

                    await queue.put(UnresolvedChangeEvent(
                        filename=resolved_path,
                        file=b"",
                        time=time.time()
                    ))

                    continue

                case WatchEventType.Create:
                    if cache_lock.read().get(resolved_path) is None:
                        async with cache_lock as cache:
                            cache[resolved_path] = b""

                case WatchEventType.Modify:
                    pass

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
