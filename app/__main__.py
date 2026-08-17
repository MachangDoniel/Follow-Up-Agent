"""Entry point: `uv run python -m app`

Runs the Telegram watcher and the HTTP brain in one process. The Node WhatsApp
watcher is a separate process that talks to the brain over HTTP.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from . import brain, logging_setup, render
from .commands import CommandServer
from .config import ConfigError, Settings
from .controls import Controls
from .decide import Decider
from .gchat_watcher import GoogleChatWatcher
from .llm import LMStudio
from .notify import Notifier
from .store import Store
from .sweep import Sweeper
from .teams_watcher import TeamsBotWatcher
from .telegram_watcher import TelegramWatcher

log = logging.getLogger("app")


async def run(settings: Settings) -> None:
    store = Store(settings.database_path)
    llm = LMStudio(settings)
    controls = Controls(settings, store)
    decider = Decider(settings, store, llm, controls)
    notifier = Notifier(settings)

    runner = await brain.serve(settings, decider, store, notifier)

    if settings.dry_run:
        log.warning("DRY RUN is on - messages are generated and logged, never sent.")
    if notifier.enabled:
        log.info(
            "notifications on: every sent reply goes to Telegram chat %s%s",
            settings.notify_chat_id,
            "" if settings.notify_image else " (text only)",
        )
        if settings.notify_image and not render.AVAILABLE:
            log.warning(
                "NOTIFY_IMAGE is on but Pillow is not installed; notifications will be "
                "text only. Run: uv sync"
            )
    if not settings.your_name:
        log.warning("YOUR_NAME is empty; generated messages will be generic.")
    if settings.allow:
        log.info("allow list active: only %s will get replies", ", ".join(settings.allow))

    tasks: list[asyncio.Task] = []

    # Built before the command server so a sweep can send through it, and
    # started after, so nothing is dispatched at a client that isn't connected.
    telegram = TelegramWatcher(settings, decider, notifier) if settings.telegram_enabled else None
    senders = {"telegram": telegram.send} if telegram else {}
    sweeper = Sweeper(settings, store, decider, senders)

    command_server: CommandServer | None = None
    if notifier.enabled:
        command_server = CommandServer(settings, store, controls, sweeper)
        tasks.append(asyncio.create_task(command_server.run()))

    if telegram is not None:
        tasks.append(asyncio.create_task(telegram.run()))
    else:
        log.info("Telegram watcher disabled (TELEGRAM_ENABLED=false)")

    gchat: GoogleChatWatcher | None = None
    if settings.gchat_enabled:
        gchat = GoogleChatWatcher(settings)
        tasks.append(asyncio.create_task(gchat.run()))
        log.info("Google Chat watcher enabled (Pub/Sub pull)")
    else:
        log.info("Google Chat watcher disabled (GCHAT_ENABLED=false)")

    teams: TeamsBotWatcher | None = None
    if settings.teams_enabled:
        teams = TeamsBotWatcher(settings)
        tasks.append(asyncio.create_task(teams.run()))
        log.info("Teams bot watcher enabled (port %s)", settings.teams_bot_port)
    else:
        log.info("Teams bot watcher disabled (TEAMS_ENABLED=false)")

    try:
        if tasks:
            await asyncio.gather(*tasks)
        else:
            await asyncio.Event().wait()  # brain-only mode, for WhatsApp
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await runner.cleanup()
        await llm.aclose()
        await notifier.aclose()
        if command_server is not None:
            await command_server.aclose()
        await sweeper.aclose()
        if gchat is not None:
            await gchat.aclose()
        if teams is not None:
            await teams.aclose()
        store.close()


def main() -> int:
    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logging_setup.setup(settings.log_level, settings.log_file)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        log.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
