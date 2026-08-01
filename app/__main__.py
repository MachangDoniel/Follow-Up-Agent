"""Entry point: `uv run python -m app`

Runs the Telegram watcher and the HTTP brain in one process. The Node WhatsApp
watcher is a separate process that talks to the brain over HTTP.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from . import brain, logging_setup
from .config import ConfigError, Settings
from .decide import Decider
from .llm import LMStudio
from .store import Store
from .telegram_watcher import TelegramWatcher

log = logging.getLogger("app")


async def run(settings: Settings) -> None:
    store = Store(settings.database_path)
    llm = LMStudio(settings)
    decider = Decider(settings, store, llm)

    runner = await brain.serve(settings, decider, store)

    if settings.dry_run:
        log.warning("DRY RUN is on - messages are generated and logged, never sent.")
    if not settings.your_name:
        log.warning("YOUR_NAME is empty; generated messages will be generic.")
    if settings.allow:
        log.info("allow list active: only %s will get replies", ", ".join(settings.allow))

    tasks: list[asyncio.Task] = []
    if settings.telegram_enabled:
        tasks.append(asyncio.create_task(TelegramWatcher(settings, decider).run()))
    else:
        log.info("Telegram watcher disabled (TELEGRAM_ENABLED=false)")

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
