from __future__ import annotations

from typing import Iterable

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from brain.artifact_store import ArtifactStore


def build_artifacts_router(
    *,
    artifacts: ArtifactStore,
    loopback_hosts: Iterable[str],
) -> APIRouter:
    router = APIRouter()
    allowed_hosts = set(loopback_hosts)

    @router.post("/api/artifacts")
    async def upload_artifact(
        request: Request, file: UploadFile = File(...)
    ) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in allowed_hosts:
            raise HTTPException(
                status_code=403, detail="Artifact uploads are restricted to localhost."
            )

        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="Empty artifact payload.")
        if len(data) > artifacts.max_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Artifact exceeds maximum allowed size of "
                    f"{artifacts.max_bytes} bytes."
                ),
            )

        try:
            rec = artifacts.save(
                data,
                content_type=file.content_type or "application/octet-stream",
                filename=file.filename or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return JSONResponse(
            {
                "artifact_id": rec.artifact_id,
                "kind": rec.kind,
                "filename": rec.filename,
                "content_type": rec.content_type,
                "expires_at": rec.expires_at,
                "size_bytes": rec.size_bytes,
            }
        )

    return router
