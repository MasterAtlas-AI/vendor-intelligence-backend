from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import ContainerDep

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(c: ContainerDep) -> JSONResponse:
    checks: dict[str, Any] = {}
    try:
        async with c.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
    healthy = checks["database"] == "ok"
    # Informational: "disabled" is normal when jobs run in a separate `app.cli run-worker` process.
    checks["job_runner"] = "running" if c.job_runner.running else "disabled"
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks}, status_code=200 if healthy else 503
    )
