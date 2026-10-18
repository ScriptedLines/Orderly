import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import List
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from config import ALLOWED_EXTENSIONS, MAX_FILE_SIZE_BYTES, logger
from dependencies import AuthenticatedUser, require_elevated_role
from schemas import IngestResponse
from services import _store_chunks

router = APIRouter(tags=["Ingestion"])

@router.post("/ingest", response_model=IngestResponse, status_code=status.HTTP_200_OK)
async def ingest_documents(
    files: List[UploadFile] = File(...),
    current_user: AuthenticatedUser = Depends(require_elevated_role),
):
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No files uploaded. Please attach at least one document.",
        )

    tmp_dir = tempfile.mkdtemp(prefix="rag_ingest_")
    saved_paths: List[str] = []
    processed_names: List[str] = []

    try:
        for upload in files:
            if not upload.filename:
                continue

            ext = Path(upload.filename).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unsupported file type '{upload.filename}'. Allowed types: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
                )

            dest = Path(tmp_dir) / upload.filename
            content = await upload.read()

            if len(content) > MAX_FILE_SIZE_BYTES:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"File '{upload.filename}' exceeds maximum allowed size of {MAX_FILE_SIZE_BYTES // (1024*1024)}MB.",
                )

            dest.write_bytes(content)
            saved_paths.append(str(dest))
            processed_names.append(upload.filename)

        def _process_ingestion():
            try:
                return _store_chunks(saved_paths)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        ingest_task = asyncio.create_task(asyncio.to_thread(_process_ingestion))
        try:
            total_chunks = await asyncio.shield(ingest_task)
            return IngestResponse(
                message="Ingestion complete.",
                total_chunks=total_chunks,
                files_processed=processed_names,
            )
        except asyncio.CancelledError:
            logger.info(f"Client disconnected during ingestion of {processed_names}. Ingestion task continues in background.")
            raise
    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.exception(f"Unexpected error in ingestion endpoint: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ingestion failed: {str(e)}",
        )
