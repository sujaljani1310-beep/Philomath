import asyncio
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile

from app.routes import files
from app.services.auth_service import AuthenticatedUser


class FileTests(unittest.IsolatedAsyncioTestCase):
    user = AuthenticatedUser(id="test-owner")

    async def test_text_upload_response_and_close(self):
        upload = UploadFile(filename="sample.txt", file=BytesIO(b"hello"))
        result = await files.upload_files([upload], self.user)
        self.assertEqual(result, {
            "success": True, "count": 1,
            "files": [{"name": "sample.txt", "content_type": None, "size": 5, "characters": 5, "text": "hello"}],
            "combined_text": "===== FILE: sample.txt =====\nhello",
        })
        self.assertTrue(upload.file.closed)

    async def test_extraction_does_not_block_event_loop(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_extract(*args):
            started.set()
            await asyncio.wait_for(release.wait(), timeout=2)
            return "extracted"
        upload = UploadFile(filename="sample.pdf", file=BytesIO(b"test"))
        with patch.object(files, "extract_file_text", side_effect=slow_extract):
            task = asyncio.create_task(files.upload_files([upload], self.user))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(started.is_set())
                self.assertFalse(task.done())
            finally:
                release.set()
            self.assertEqual((await task)["files"][0]["text"], "extracted")

    async def test_size_limit_bounds_read_and_preserves_413(self):
        upload = UploadFile(filename="sample.txt", file=BytesIO())
        with patch.object(files, "MAX_FILE_SIZE", 10), patch.object(
            upload, "read", new=AsyncMock(return_value=b"x" * 11)
        ) as read:
            with self.assertRaises(HTTPException) as caught:
                await files.upload_files([upload], self.user)
            self.assertEqual(caught.exception.status_code, 413)
            read.assert_awaited_once_with(11)
        self.assertTrue(upload.file.closed)

    async def test_empty_unsupported_and_parse_errors_are_sanitized_and_close(self):
        for filename, content, status in [("empty.txt", b"", 422), ("bad.exe", b"test", 422), ("bad.pdf", b"test", 422)]:
            with self.subTest(filename=filename):
                upload = UploadFile(filename=filename, file=BytesIO(content))
                with patch.object(files, "extract_file_text", side_effect=ValueError("invalid document")):
                    with self.assertRaises(HTTPException) as caught:
                        await files.upload_files([upload], self.user)
                self.assertEqual(caught.exception.status_code, status)
                self.assertTrue(upload.file.closed)
