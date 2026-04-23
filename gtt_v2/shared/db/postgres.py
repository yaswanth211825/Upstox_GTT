import asyncpg
import pathlib
import structlog
from typing import Optional

log = structlog.get_logger()

_pool: Optional[asyncpg.Pool] = None


async def create_pool(dsn: str) -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(
        dsn,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    log.info("db_pool_created", dsn=dsn.split("@")[-1])
    return _pool


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — call create_pool() first")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def run_migrations(pool: asyncpg.Pool) -> None:
    migrations_dir = pathlib.Path(__file__).parent / "migrations"
    sql_files = sorted(migrations_dir.glob("*.sql"))
    async with pool.acquire() as conn:
        # Serialize migrations across services so concurrent startups do not
        # race on schema_migrations bookkeeping.
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", 884211)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            for sql_file in sql_files:
                version = sql_file.stem
                applied = await conn.fetchval(
                    "SELECT 1 FROM schema_migrations WHERE version = $1", version
                )
                if applied:
                    continue
                sql = sql_file.read_text()
                await conn.execute(sql)
                await conn.execute(
                    """
                    INSERT INTO schema_migrations (version)
                    VALUES ($1)
                    ON CONFLICT (version) DO NOTHING
                    """,
                    version,
                )
                log.info("migration_applied", version=version)
