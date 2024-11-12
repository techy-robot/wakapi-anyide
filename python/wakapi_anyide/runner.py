import asyncio
import base64
import logging
import time
from asyncio import CancelledError
from asyncio import Future
from asyncio import Queue
from asyncio import Task
from asyncio import TaskGroup
from collections.abc import Sequence
from hashlib import sha256
from platform import uname
from typing import Dict

from aiohttp import ClientResponse
from aiohttp import request

from wakapi_anyide.models.environment import Environment
from wakapi_anyide.watchers import WATCHERS
from wakapi_anyide.watchers.filewatcher import FileWatcher
from wakapi_anyide.watchers.types import Event
from wakapi_anyide.watchers.types import Watcher

logger = logging.getLogger(__name__)


class ConfigInvalidatedException(Exception):
    pass
    

async def heartbeat_task(env: Environment, queue: Queue[Event], watchers: Sequence[Watcher], should_shutdown: asyncio.Event):
    next_heartbeat_due = time.time() + env.config.settings.heartbeat_rate_limit_seconds
    fut: Future[Event] | None = None

    while not should_shutdown.is_set():
        changed_events: Dict[str, Event] = dict()
        
        # wait until the next heartbeat due, processing events in the meantime
        while (due := next_heartbeat_due - time.time()) > 0 and not should_shutdown.is_set():
            # we don't want to block on getting an event if we're already processing one, so only get a new event if the previous one is done
            logger.debug(f"Next event due in {due}s")
        
        logger.debug("Processing heartbeats")
        
        next_heartbeat_due = time.time() + env.config.settings.heartbeat_rate_limit_seconds
        
        # Adding to the events. Watchers is a list of individual programs
        for watcher in watchers:
            logger.debug(f"Getting events from {watcher}")
            iterable = watcher.resolve_events() # returns a generator
            logger.debug(f"Maybe iterable is {iterable}")
            
            if iterable is not None:           
                async for event in iterable:
                    # everything in the queue, process the event
                    changed_events[event.filename] = event
                    logger.info(f"Got event for {event.filename}")
        
        logger.debug(changed_events)
        
        if len(changed_events) == 0:
            logger.debug(f"No changes detected.")
            continue

        logger.info(f"Change summary:")
        for event in changed_events.values():
            logger.info(f"{event.filename:20} checksum: {event.checksum} +{event.lines_added} -{event.lines_removed}")

        host = uname()
        user_agent = f"wakatime/unset ({host.system}-none-none) wakapi-anyide-wakatime/unset"

        heartbeats = [{
            "entity": event.filename,
            "type": "file",
            "category": "coding",
            "time": event.time,
            "project": env.project.project.name,
            "language": language_processor(env, event.file_extension),
            "line_additions": event.lines_added,
            "line_deletions": event.lines_removed,
            "lines": event.lines,
            "is_write": True,
            "editor": "wakapi-anyide", # Add to a list of editors in the wakapi-anyide settings
            "machine": env.config.settings.hostname or f"anonymised machine {sha256(host.node.encode()).hexdigest()[:8]}",
            "operating_system": host.system,
            "user_agent": user_agent
        } for event in changed_events.values()]

        response: ClientResponse
        last_text: str | None = None

        if env.is_test_only:
            logger.info(f"Would've sent heartbeat, but in testing mode")
            continue

        for i in range(3):
            logger.info(f"Sending heartbeat (attempt {i+1})")
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


def language_processor(env: Environment, file_extension: str) -> str:
    languages = env.project.files.language_mapping

    lang = languages.get(file_extension)  # If the suffix matches a defined one in the languages table
    if lang is None:
        return file_extension.replace(".", "")  # If it didn't find a match, return the suffix only
    return lang

def editor_processor(env: Environment, file_extension: str) -> str:
    """
    Determines the editor name for a given file extension based on the 
    environment's editor mapping configuration.

    Args:
        env (Environment): The environment containing project settings.
        file_extension (str): The file extension to look up.

    Returns:
        str: The name of the editor associated with the file extension,
             or "wakapi-anyide" if no specific editor is found.
    """
    editor = env.project.files.editor_mapping

    edit = editor.get(file_extension)  # If the file suffix matches a defined one in the table, d
    if edit is None:
        return f"wakapi-anyide"  # default to wakapi-anyide
    return edit
    
    
async def run(env: Environment):
    ev = asyncio.get_event_loop()
    runners = [WATCHERS[watcher](env) for watcher in env.project.meta.watchers]
    emit_events: Queue[Event] = Queue()
    should_shutdown = asyncio.Event()
    task: Future
    
    try:
        async with TaskGroup() as tg:
            for runner in runners:
                await runner.setup(tg, emit_events)
            
            async def throw_exception(exc):
                raise exc
            
            task = ev.create_task(heartbeat_task(env, emit_events, runners, should_shutdown))
            
            def done_callback(task: Task):
                try:
                    exc = task.exception()
                except CancelledError:
                    return
                
                if exc is not None and type(exc) not in (CancelledError, KeyboardInterrupt):
                    print(exc)
                    tg.create_task(throw_exception(exc))
        
            task.add_done_callback(done_callback)
    except KeyboardInterrupt:
        pass
    
    should_shutdown.set()
    await task
