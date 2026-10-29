"""The `excel_import` job: reads the stored upload and runs every row through the importer.

The whole import is one transaction: if the worker dies or the user cancels, nothing is kept and a re-run starts
clean (the runner's at-least-once contract). Progress is written through separate short sessions, so it shows live
while the import transaction is still open.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.core.exceptions import NotFound
from app.domain.enums import ActorType, AvlStatus, ImportRowOutcome
from app.modules.audit.service import record_audit
from app.modules.imports.importer import ImportOptions, Taxonomy, VendorListImporter, parse_row
from app.modules.imports.models import Upload
from app.modules.imports.service import read_upload
from app.modules.imports.spreadsheet import HEADER_SCAN_ROWS, data_rows
from app.workers.runner import JobContext

PROGRESS_EVERY = 25


async def excel_import_handler(ctx: JobContext) -> dict[str, Any]:
    inp = ctx.input
    max_rows = ctx.settings.import_max_rows
    avl = inp["options"].get("default_avl_status")
    options = ImportOptions(
        default_avl_status=AvlStatus(avl) if avl else None, tags=list(inp["options"].get("tags", []))
    )
    mapping = {k: int(v) for k, v in inp["mapping"].items()}

    await ctx.progress("reading", 2)
    async with ctx.session() as s:
        upload = await s.scalar(
            select(Upload).where(Upload.id == uuid.UUID(inp["upload_id"])).options(undefer(Upload.data))
        )
        if upload is None:
            raise NotFound("The uploaded file no longer exists")
        book = await read_upload(upload, max_rows + HEADER_SCAN_ROWS + 1)
        sheet = book.sheet(inp["sheet"])
        rows, truncated = data_rows(sheet, int(inp["header_row"]), max_rows)

        taxonomy = await Taxonomy.load(s)
        importer = VendorListImporter(
            s,
            taxonomy,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            job_id=ctx.job_id,
            upload_id=upload.id,
            auto_approve=bool(inp.get("auto_approve")),
            options=options,
        )
        counts = {o.value: 0 for o in ImportRowOutcome}
        await ctx.progress("importing", 5)
        for done, (row_number, values) in enumerate(rows, start=1):
            result = await importer.process(parse_row(row_number, values, mapping, taxonomy, options))
            counts[result.outcome.value] += 1
            if done % PROGRESS_EVERY == 0:
                await s.flush()
                await ctx.progress("importing", 5 + int(done * 90 / len(rows)))

        summary = {
            "upload_id": str(upload.id),
            "filename": upload.filename,
            "sheet": sheet.name,
            "rows": len(rows),
            "truncated": truncated,
            "max_rows": max_rows,
            "counts": counts,
        }
        record_audit(
            s,
            "import.completed",
            actor_type=ActorType.USER if ctx.user_id else ActorType.SYSTEM,
            actor_id=ctx.user_id,
            tenant_id=ctx.tenant_id,
            entity_type="job",
            entity_id=ctx.job_id,
            changes=summary,
        )
        await ctx.progress("saving", 97)
        await s.commit()
    return summary
