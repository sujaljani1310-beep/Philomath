from pathlib import Path
from time import perf_counter
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.document_extraction import ExtractionError, extract_document
from app.observability import record_timing
from app.services.auth_service import AuthenticatedUser
from app.services.startup_service import get_startup_user


router = APIRouter()

MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_FILE_COUNT = 5
MAX_AGGREGATE_BYTES = 20 * 1024 * 1024
MAX_AGGREGATE_CHARACTERS = 500_000
MAX_FILENAME_CHARACTERS = 255
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}


async def extract_file_text(filename: str, file_bytes: bytes) -> str:
    started = perf_counter()
    outcome = "ok"
    try:
        return await extract_document(filename, file_bytes)
    except BaseException:
        outcome = "error"
        raise
    finally:
        record_timing("file.extract", started, outcome=outcome)


@router.post("/api/files/upload")
async def upload_files(
    files: List[UploadFile] = File(...),
    user: AuthenticatedUser = Depends(get_startup_user),
):
    results = []
    aggregate_bytes = 0
    aggregate_characters = 0
    try:
        if not files:
            raise HTTPException(422, detail="No files were uploaded.")
        if len(files) > MAX_FILE_COUNT:
            raise HTTPException(413, detail="File count limit exceeded.")

        for uploaded_file in files:
            filename = uploaded_file.filename or "unnamed"
            if len(filename) > MAX_FILENAME_CHARACTERS:
                raise HTTPException(422, detail="File name is too long.")
            extension = Path(filename).suffix.lower()
            if extension not in ALLOWED_EXTENSIONS:
                raise HTTPException(422, detail="Unsupported file type. Supported files: PDF, DOCX, TXT, MD.")

            # Ingress middleware applies these limits before multipart parsing;
            # retain route checks for defense in depth and direct callers.
            file_bytes = await uploaded_file.read(MAX_FILE_SIZE + 1)
            if not file_bytes:
                raise HTTPException(422, detail="Uploaded files must not be empty.")
            if len(file_bytes) > MAX_FILE_SIZE:
                raise HTTPException(413, detail="File size limit exceeded. Maximum file size is 10 MB.")
            aggregate_bytes += len(file_bytes)
            if aggregate_bytes > MAX_AGGREGATE_BYTES:
                raise HTTPException(413, detail="Aggregate file size limit exceeded.")

            try:
                text = await extract_file_text(filename, file_bytes)
            except ExtractionError as error:
                raise HTTPException(error.status_code, detail=error.detail) from None
            except Exception:
                raise HTTPException(422, detail="Document could not be read.") from None
            aggregate_characters += len(text)
            if aggregate_characters > MAX_AGGREGATE_CHARACTERS:
                raise HTTPException(413, detail="Aggregate extracted text limit exceeded.")
            results.append({
                "name": filename,
                "content_type": uploaded_file.content_type,
                "size": len(file_bytes),
                "characters": len(text),
                "text": text,
            })
    finally:
        # Also close files that were never processed after an earlier rejection.
        for uploaded_file in files:
            try:
                await uploaded_file.close()
            except Exception:
                pass

    combined_text = "\n\n".join(
        f"===== FILE: {file_data['name']} =====\n{file_data['text']}"
        for file_data in results
    )
    return {
        "success": True,
        "count": len(results),
        "files": results,
        "combined_text": combined_text,
    }
