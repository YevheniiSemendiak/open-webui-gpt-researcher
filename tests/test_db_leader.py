from __future__ import annotations

import pytest

from open_webui_gpt_researcher.db import AdvisoryLockLease, Database


class FakeConnection:
    invalidated = False

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0

    async def scalar(self, statement: object, params: object = None) -> int | bool:
        del params
        sql = str(statement)
        self.statements.append(sql)
        if "pg_backend_pid" in sql:
            return 1234
        return True

    async def commit(self) -> None:
        self.commits += 1


async def test_advisory_lock_lease_checks_session_and_unlocks() -> None:
    connection = FakeConnection()
    lease = AdvisoryLockLease(
        connection=connection,  # type: ignore[arg-type]
        lock_id=42,
        backend_pid=1234,
        acquired=True,
    )
    assert await lease.is_valid() is True
    await lease.release()
    assert lease.acquired is False
    assert any("pg_advisory_unlock" in statement for statement in connection.statements)
    assert connection.commits == 3


async def test_database_election_rejects_non_postgresql() -> None:
    database = Database("sqlite+aiosqlite://")
    try:
        with pytest.raises(RuntimeError, match="requires PostgreSQL"):
            async with database.advisory_lock(42):
                pass
    finally:
        await database.close()
