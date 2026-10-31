"""Rich terminal progress for long-running CLI tasks."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from .events import RouterEvent


@contextmanager
def tts_progress(
    console: Console, *, enabled: bool = True
) -> Iterator[Callable[[RouterEvent], None]]:
    """Yield an event callback that renders deterministic file-level TTS progress.

    The percentage advances only when a synthesis chunk completes and during MP3
    encoding; routing, quota waits and retries update the label without inventing
    time-based progress.
    """
    progress = Progress(
        SpinnerColumn("dots"),
        TextColumn("{task.description}", markup=False),
        BarColumn(bar_width=28),
        TaskProgressColumn(),
        console=console,
        transient=False,
        refresh_per_second=10,
        disable=not (enabled and console.is_terminal),
    )
    with progress:
        task = progress.add_task("Preparing audio", total=100)
        completed = 0.0

        def report(event: RouterEvent) -> None:
            nonlocal completed
            if event.pct is not None:
                completed = max(completed, min(100.0, float(event.pct)))
            description = event.message()
            progress.update(task, completed=completed, description=description)

        yield report
