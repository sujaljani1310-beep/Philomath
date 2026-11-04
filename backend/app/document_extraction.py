"""Cancellation-aware, bounded document extraction without a persistent worker."""

import asyncio
import json
import os
import sys
from pathlib import Path
from threading import BoundedSemaphore

from app.document_worker import MAX_INPUT_BYTES, MAX_OUTPUT_CHARACTERS


MAX_PROCESSING_SECONDS = 10.0
# An astral character is escaped as two six-byte JSON surrogate escapes.
MAX_WIRE_BYTES = MAX_OUTPUT_CHARACTERS * 12 + 1024
_worker_slots = BoundedSemaphore(1)
_WORKER_PATH = Path(__file__).with_name("document_worker.py")


class ExtractionError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def _terminate(process):
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    # A paused StreamReader can keep the subprocess transport's wait future
    # pending even after SIGKILL. Drain remaining pipe bytes without retaining
    # them before reaping, including after an oversized IPC response.
    if process.stdout is not None:
        while await process.stdout.read(64 * 1024):
            pass
    await process.wait()


async def _finish_cleanup(task):
    """Keep ownership through repeated caller cancellation, without respawning.

    Cancelling a shielded await leaves its task running. Merely shielding once
    would therefore release the worker permit while a child was still alive.
    Reawait the same task until completion, then propagate caller cancellation
    after spawn/kill/wait have all finished.
    """
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    return task.result(), interrupted


async def _exchange(process, file_bytes):
    async def write_input():
        try:
            process.stdin.write(file_bytes)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def read_output():
        output = bytearray()
        while chunk := await process.stdout.read(64 * 1024):
            output.extend(chunk)
            if len(output) > MAX_WIRE_BYTES:
                raise ExtractionError(413, "Document resource limits exceeded.")
        return bytes(output)

    writer = asyncio.create_task(write_input())
    try:
        output = await read_output()
        await writer
        await process.wait()
        return output
    finally:
        if not writer.done():
            writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


async def _run_worker(extension: str, file_bytes: bytes) -> str:
    process = None
    creation = None
    try:
        async with asyncio.timeout(MAX_PROCESSING_SECONDS):
            # No inherited environment, credentials, cwd imports, dotenv, or
            # application bootstrapping in the parser interpreter.
            creation = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-I", "-B", str(_WORKER_PATH), extension,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"},
                close_fds=True,
            ))
            process = await asyncio.shield(creation)
            output = await _exchange(process, file_bytes)
            if process.returncode != 0:
                raise ExtractionError(413, "Document resource limits exceeded.")
            try:
                response = json.loads(output)
            except (ValueError, UnicodeError):
                raise ExtractionError(503, "Document processing is temporarily unavailable.") from None
            if not isinstance(response, dict):
                raise ExtractionError(503, "Document processing is temporarily unavailable.")
            if response.get("ok") is True and isinstance(response.get("text"), str):
                text = response["text"]
                if len(text) > MAX_OUTPUT_CHARACTERS:
                    raise ExtractionError(413, "Document text limit exceeded.")
                return text
            kind = response.get("kind")
            if kind == "limit":
                raise ExtractionError(413, "Document resource limits exceeded.")
            if kind == "unavailable":
                raise ExtractionError(503, "Document processing is temporarily unavailable.")
            raise ExtractionError(422, "Document could not be read.")
    except TimeoutError:
        raise ExtractionError(504, "Document processing deadline exceeded.") from None
    except OSError:
        raise ExtractionError(503, "Document processing is temporarily unavailable.") from None
    finally:
        # Shield spawn as well as cleanup: cancellation during process creation
        # must not lose the handle to a newly started child.
        interrupted = False
        if process is None and creation is not None:
            try:
                process, interrupted = await _finish_cleanup(creation)
            except OSError:
                pass
        if process is not None:
            cleanup = asyncio.create_task(_terminate(process))
            _, cleanup_interrupted = await _finish_cleanup(cleanup)
            interrupted = interrupted or cleanup_interrupted
        if interrupted:
            raise asyncio.CancelledError


async def extract_document(filename: str, file_bytes: bytes) -> str:
    if len(file_bytes) > MAX_INPUT_BYTES:
        raise ExtractionError(413, "File size limit exceeded.")
    extension = Path(filename).suffix.lower()
    if extension in {".txt", ".md"}:
        text = file_bytes.decode("utf-8", errors="replace")
        if len(text) > MAX_OUTPUT_CHARACTERS:
            raise ExtractionError(413, "Document text limit exceeded.")
        return text
    if extension not in {".pdf", ".docx"}:
        raise ExtractionError(422, "Unsupported file type. Supported files: PDF, DOCX, TXT, MD.")
    if not _worker_slots.acquire(blocking=False):
        raise ExtractionError(503, "Document processing is busy. Please try again later.")
    try:
        return await _run_worker(extension, file_bytes)
    finally:
        _worker_slots.release()
