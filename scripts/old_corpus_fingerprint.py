"""Day 11: prove that the old corpus (the `Corpus Tenant` of Days 9 and 10) has not changed.

All new corpus work goes into another organisation. This script only reads. It makes one
fingerprint over the old tenant's documents and chunks, and compares it with
the one saved in scripts/results/ the first time it ran.

    uv run python scripts/old_corpus_fingerprint.py            compare with the saved one
    uv run python scripts/old_corpus_fingerprint.py --save     save it (only if none is saved)
"""

import asyncio
import json
import sys

from eval_search import CORPUS_TENANT_ID, SCRIPTS
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_service.core.config import get_settings

SAVED = SCRIPTS / "results" / "day11_old_corpus_fingerprint.json"

# The columns that existed when the old corpus was fingerprinted. They are listed by name, so
# that a column added later (by a migration) does not look like a change of the old data.
COLUMNS = {
    "documents": "id, tenant_id, title, source, content, content_hash, metadata, created_at, "
    "updated_at",
    "chunks": "id, tenant_id, document_id, chunk_index, heading, text, text_hash, embedding, "
    "embedding_model, metadata, created_at, updated_at",
}

# md5 of every row (those columns as text), then md5 of those in the order of the row ids
SQL = """
select count(*), coalesce(md5(string_agg(md5(row({columns})::text), '' order by id)), '')
from {table} where tenant_id = :tenant
"""


async def fingerprint() -> dict[str, dict[str, object]]:
    engine = create_async_engine(str(get_settings().DATABASE_URL))
    result: dict[str, dict[str, object]] = {}
    async with engine.connect() as connection:
        for table in COLUMNS:
            rows, digest = (
                await connection.execute(
                    text(SQL.format(table=table, columns=COLUMNS[table])),
                    {"tenant": CORPUS_TENANT_ID},
                )
            ).one()
            result[table] = {"rows": rows, "md5": digest}
    await engine.dispose()
    return result


def main() -> None:
    now = asyncio.run(fingerprint())
    for table, found in now.items():
        print(f"{table:<10} {found['rows']:>5} rows   md5 {found['md5']}")
    if "--save" in sys.argv:
        if SAVED.exists():
            sys.exit(f"a fingerprint is already saved in {SAVED.name}; it is not overwritten")
        SAVED.write_text(json.dumps(now, indent=2), encoding="utf-8")
        print(f"saved: scripts/results/{SAVED.name}")
        return
    saved = json.loads(SAVED.read_text(encoding="utf-8"))
    same = saved == now
    print("THE OLD CORPUS IS UNCHANGED" if same else "THE OLD CORPUS HAS CHANGED")
    sys.exit(0 if same else 1)


if __name__ == "__main__":
    main()
