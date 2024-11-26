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
from wakapi_anyide.watchers.types import Event
from wakapi_anyide.watchers.types import Watcher

logger = logging.getLogger(__name__)


class ConfigInvalidatedException(Exception):
    pass


async def heartbeat_task(
    env: Environment,
    queue: Queue[Event],
    watchers: Sequence[Watcher],
    should_shutdown: asyncio.Event,
):
    next_heartbeat_due = time.time() + env.config.settings.heartbeat_rate_limit_seconds
    fut: Future[Event] | None = None

    while not should_shutdown.is_set():
        changed_events: Dict[str, Event] = dict()

        while (
            due := next_heartbeat_due - time.time()
        ) > 0 and not should_shutdown.is_set():
            if fut is None or fut.done():
                fut = asyncio.create_task(queue.get())

            logger.debug(f"Next event due in {due}s")

            completed, rest = await asyncio.wait(
                [fut, asyncio.create_task(should_shutdown.wait())],
                return_when=asyncio.FIRST_COMPLETED,
                timeout=due,
            )  # type: ignore

            if fut in completed:
                event = fut.result()

                logger.debug(f"Got event for {event.filename}!")

                changed_events[event.filename] = event

        logger.debug("Processing heartbeats")

        next_heartbeat_due = (
            time.time() + env.config.settings.heartbeat_rate_limit_seconds
        )

        for watcher in watchers:
            logger.debug(f"Getting events from {watcher}")
            iterable = watcher.resolve_events()
            logger.debug(f"Maybe iterable is {iterable}")

            if iterable is not None:
                async for event in iterable:
                    changed_events[event.filename] = event
                    logger.debug(f"Got event for {event.filename}")

        logger.debug(changed_events)

        if len(changed_events) == 0:
            logger.debug("No changes detected.")
            continue

        logger.info("Change summary:")
        for event in changed_events.values():
            #:20 strips the hashtag from the filename for better viewing. It shows up on the dashboard still
            logger.info(
                f"{event.filename:20} at {event.cursor[0]}:{event.cursor[1]} +{event.lines_added} -{event.lines_removed} total: {event.lines}"
            )

        host = uname()
        user_agent = (
            f"wakatime/unset ({host.system}-none-none) wakapi-anyide-wakatime/unset"
        )

        heartbeats = [
            {
                "entity": event.filename,
                "type": "file",
                "category": "coding",
                "time": event.time,
                "project": env.project.project.name,
                "language": env.project.files.language_mapping.get(
                    event.file_extension, event.file_extension.replace(".", "")
                ),
                "lines": event.lines,
                "line_additions": event.lines_added,
                "line_deletions": event.lines_removed,
                "lineno": event.cursor[0],
                "cursorpos": event.cursor[1],
                "is_write": True,
                "editor": env.project.files.editor_mapping.get(
                    event.file_extension, "wakapi-anyide"
                ),
                "machine": env.config.settings.hostname
                or f"anonymised machine {sha256(host.node.encode()).hexdigest()[:8]}",
                "operating_system": host.system,
                "user_agent": user_agent,
            }
            for event in changed_events.values()
        ]

        response: ClientResponse
        last_text: str | None = None

        if env.is_test_only:
            logger.info("Would've sent heartbeat, but in testing mode")
            continue

        for i in range(3):
            logger.info(f"Sending heartbeat (attempt {i+1})")
            async with request(
                "POST",
                f"{env.config.settings.api_url}/users/current/heartbeats.bulk",
                json=heartbeats,
                headers={
                    "User-Agent": user_agent,
                    "Authorization": f"Basic {base64.b64encode(env.config.settings.api_key.encode()).decode()}",
                },
            ) as response:
                if response.status == 201:
                    break
                else:
                    last_text = await response.text()
        else:
            raise Exception(f"Failed to send heartbeat: {response.status} {last_text}")


async def run(env: Environment):
    ev = asyncio.get_event_loop()
    runners = [WATCHERS[watcher](env) for watcher in env.project.meta.watchers]
    emit_events: Queue[Event] = Queue()
    should_shutdown = asyncio.Event()
    task: Future | None = None

    try:
        async with TaskGroup() as tg:
            for runner in runners:
                await runner.setup(tg, emit_events)

            async def throw_exception(exc):
                raise exc

            task = ev.create_task(
                heartbeat_task(env, emit_events, runners, should_shutdown)
            )

            def done_callback(task: Task):
                logger.debug(task)
                try:
                    exc = task.exception()
                except CancelledError:
                    return

                if exc is not None and type(exc) not in (
                    CancelledError,
                    KeyboardInterrupt,
                ):
                    print(exc)
                    tg.create_task(throw_exception(exc))

            task.add_done_callback(done_callback)
    except KeyboardInterrupt:
        pass
    finally:
        should_shutdown.set()
        if task is not None:
            await task
