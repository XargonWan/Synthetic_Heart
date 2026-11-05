"""The /api/database/query SQL filter and its read-only execution path."""

import pytest

from core.webui import _is_read_only_query


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "select id, name from memories where id = 3",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "SELECT pg_size_pretty(pg_database_size('synth'))",
    ],
)
def test_plain_selects_are_allowed(query: str) -> None:
    assert _is_read_only_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * INTO newtbl FROM memories",
        "SELECT pg_read_file('/etc/passwd')",
        "select pg_ls_dir('/')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT set_config('x','y',false)",
        "SELECT pg_sleep(60)",
        "SELECT * FROM dblink('host=x','select 1') AS t(a int)",
        "SELECT 1; DROP TABLE memories",
        "DELETE FROM memories",
        "WITH d AS (DELETE FROM memories RETURNING *) SELECT * FROM d",
        "",
    ],
)
def test_dangerous_queries_are_rejected(query: str) -> None:
    assert not _is_read_only_query(query)
