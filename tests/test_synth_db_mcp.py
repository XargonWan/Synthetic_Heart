import mcp_servers.synth_db as synth_db


def test_configured_targets_prefer_repo_env(monkeypatch):
    monkeypatch.setattr(
        synth_db,
        "_REPO_ENV",
        {
            "SYNTH_PRIMARY_DB": "soul",
            "DB_TYPE": "mariadb",
            "DB_HOST": "192.168.1.13",
            "DB_PORT": "3306",
            "DB_USER": "raineadmin",
            "DB_PASS": "secret",
            "DB_NAME": "synth",
            "DATABASE_URL": "postgresql://legacy:legacy@ignored:5432/ignored",
            "SOURCE_DB_HOST": "192.168.1.13",
            "SOURCE_DB_PORT": "3306",
            "SOURCE_DB_USER": "raineadmin",
            "SOURCE_DB_PASSWORD": "secret",
            "SOURCE_DB_NAME": "synth",
            "SOUL_POSTGRES_DSN": "postgresql://soul:soul@192.168.1.13:5432/soul",
        },
    )
    monkeypatch.setenv("DB_TYPE", "mariadb")
    monkeypatch.setenv("DB_HOST", "legacy-host")

    targets = synth_db._configured_targets()

    assert targets["runtime"].db_type == "postgres"
    assert targets["runtime"].database == "soul"
    assert targets["runtime"].dsn == "postgresql://soul:soul@192.168.1.13:5432/soul"
    assert targets["source"].db_type == "mariadb"
    assert targets["source"].database == "synth"
    assert targets["soul"].db_type == "postgres"


def test_runtime_target_can_be_forced_to_memory(monkeypatch):
    monkeypatch.setattr(
        synth_db,
        "_REPO_ENV",
        {
            "SYNTH_PRIMARY_DB": "memory",
            "DB_TYPE": "mariadb",
            "DB_HOST": "192.168.1.13",
            "DB_PORT": "3306",
            "DB_USER": "raineadmin",
            "DB_PASS": "secret",
            "DB_NAME": "synth",
            "DATABASE_URL": "postgresql://legacy:legacy@ignored:5432/ignored",
            "SOUL_POSTGRES_DSN": "postgresql://soul:soul@192.168.1.13:5432/soul",
        },
    )

    targets = synth_db._configured_targets()

    assert targets["runtime"].db_type == "mariadb"
    assert targets["runtime"].database == "synth"
    assert targets["runtime"].dsn is None


def test_process_env_target_uses_soul_settings_when_selected(monkeypatch):
    monkeypatch.setenv("SYNTH_PRIMARY_DB", "soul")
    monkeypatch.setenv(
        "SOUL_POSTGRES_DSN",
        "postgresql://soul:soul@soul-host:5544/soul_runtime",
    )
    monkeypatch.setenv("SOUL_PG_USER", "soul_user")
    monkeypatch.setenv("SOUL_PG_PASSWORD", "soul_pass")
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_PORT", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("DB_PASS", raising=False)
    monkeypatch.delenv("DB_NAME", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SOUL_PG_HOST", raising=False)
    monkeypatch.delenv("SOUL_PG_PORT", raising=False)
    monkeypatch.delenv("SOUL_PG_DB", raising=False)
    monkeypatch.delenv("SOUL_PG_PASS", raising=False)

    target = synth_db._build_process_env_target()

    assert target is not None
    assert target.db_type == "postgres"
    assert target.host == "soul-host"
    assert target.port == 5544
    assert target.user == "soul_user"
    assert target.password == "soul_pass"
    assert target.database == "soul_runtime"


def test_get_db_targets_reports_available_targets(monkeypatch):
    monkeypatch.setattr(
        synth_db,
        "_REPO_ENV",
        {
            "DB_TYPE": "postgres",
            "DB_HOST": "192.168.1.13",
            "DB_PORT": "5432",
            "DB_USER": "soul",
            "DB_PASS": "soul",
            "DB_NAME": "soul",
            "SOURCE_DB_HOST": "192.168.1.13",
            "SOURCE_DB_PORT": "3306",
            "SOURCE_DB_USER": "raineadmin",
            "SOURCE_DB_PASSWORD": "secret",
            "SOURCE_DB_NAME": "synth",
            "SOUL_POSTGRES_DSN": "postgresql://soul:soul@192.168.1.13:5432/soul",
        },
    )

    output = synth_db.get_db_targets()

    assert "runtime: postgres" in output
    assert "source: mariadb" in output
    assert "soul: postgres" in output


def test_get_recent_diary_uses_timestamp_column(monkeypatch):
    class DummyCursor:
        def __init__(self):
            self.executed = []

        def execute(self, query, params=None):
            self.executed.append((query, params))

        def fetchall(self):
            return [
                {
                    "id": 1,
                    "timestamp": "2026-05-04T22:58:10+00:00",
                    "content": "hello",
                    "personal_thought": "thinking",
                }
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyConn:
        def __init__(self, cursor):
            self._cursor = cursor

        def cursor(self):
            return self._cursor

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    cursor = DummyCursor()
    connection = DummyConn(cursor)
    runtime_target = synth_db.DbTarget(
        name="runtime",
        db_type="postgres",
        host="192.168.1.13",
        port=5432,
        user="soul",
        password="soul",
        database="soul",
        dsn="postgresql://soul:soul@192.168.1.13:5432/soul",
    )

    monkeypatch.setattr(synth_db, "_resolve_target", lambda target=None: runtime_target)
    monkeypatch.setattr(synth_db, "_connect", lambda target=None: connection)
    monkeypatch.setattr(
        synth_db,
        "_select_recent_diary_columns",
        lambda cur, target=None: ["id", "timestamp", "content", "personal_thought"],
    )

    output = synth_db.get_recent_diary(limit=1)

    executed_query = cursor.executed[-1][0]
    assert "timestamp" in executed_query
    assert "created_at" not in executed_query
    assert "personal_thought" in output
