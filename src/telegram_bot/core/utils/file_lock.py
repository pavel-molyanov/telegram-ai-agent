"""Cross-process file locking via fcntl.flock.

Provides sync FileLock (for scripts) and AsyncFileLock (for async bot code).
Lock file is {path}.lock — separate from the target file to avoid
conflicts with os.replace() during atomic writes.

Linux/macOS only (production and development are on Linux).
"""

from __future__ import annotations

import asyncio
import fcntl
from concurrent.futures import ThreadPoolExecutor
from io import TextIOWrapper
from pathlib import Path
from types import TracebackType


class FileLock:
    """Sync cross-process file lock via fcntl.flock.

    Usage::

        with FileLock("/path/to/data.json"):
            data = json.loads(Path("/path/to/data.json").read_text())
            data["key"] = "value"
            Path("/path/to/data.json").write_text(json.dumps(data))
    """

    def __init__(self, path: str | Path) -> None:
        self._lock_path = Path(path).with_suffix(Path(path).suffix + ".lock")
        self._fd: TextIOWrapper | None = None

    def __enter__(self) -> FileLock:
        self._fd = open(self._lock_path, "w")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None
            self._lock_path.unlink(missing_ok=True)


class AsyncFileLock:
    """Async cross-process file lock — flock via run_in_executor.

    Uses a dedicated ThreadPoolExecutor (not the default) to avoid
    blocking the executor pool during long lock waits.

    Usage::

        async with AsyncFileLock("/path/to/data.json"):
            # read-modify-write under lock
            ...
    """

    _shared_executor: ThreadPoolExecutor | None = None

    def __init__(self, path: str | Path, executor: ThreadPoolExecutor | None = None) -> None:
        self._lock_path = Path(path).with_suffix(Path(path).suffix + ".lock")
        self._executor = executor
        self._fd: TextIOWrapper | None = None

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is not None:
            return self._executor
        if AsyncFileLock._shared_executor is None:
            AsyncFileLock._shared_executor = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="flock"
            )
        return AsyncFileLock._shared_executor

    def _acquire(self) -> None:
        self._fd = open(self._lock_path, "w")  # noqa: SIM115
        fcntl.flock(self._fd, fcntl.LOCK_EX)

    def _release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None
            self._lock_path.unlink(missing_ok=True)

    async def __aenter__(self) -> AsyncFileLock:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._get_executor(), self._acquire)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._get_executor(), self._release)
