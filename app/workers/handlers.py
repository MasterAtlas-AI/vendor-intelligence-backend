"""Job handlers, keyed by job type. Add new background work here (one async function per JobType)."""

import asyncio
from typing import Any

from sqlalchemy import func, select

from app.domain.enums import JobType
from app.modules.imports.job import excel_import_handler, pdf_import_handler
from app.workers.runner import Handler, JobContext


async def diagnostics_handler(ctx: JobContext) -> dict[str, Any]:
    """No-op job that exercises claim → progress → result end to end."""
    steps = int(ctx.input.get("steps", 3))
    delay = float(ctx.input.get("step_seconds", 1.0))
    fail_at = ctx.input.get("fail_at_step")
    for step in range(1, steps + 1):
        if fail_at == step:
            raise RuntimeError(f"Diagnostics failure requested at step {step}")
        await asyncio.sleep(delay)
        await ctx.progress(f"step_{step}", int(step * 100 / steps))
    async with ctx.session() as s:
        db_time = await s.scalar(select(func.now()))
    return {"steps": steps, "attempt": ctx.attempt, "db_time": db_time.isoformat() if db_time else None}


HANDLERS: dict[str, Handler] = {
    JobType.DIAGNOSTICS: diagnostics_handler,
    JobType.EXCEL_IMPORT: excel_import_handler,
    JobType.PDF_IMPORT: pdf_import_handler,
}
