from complaint_warehouse.config import Settings


def test_defaults_point_at_local_compose_db(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.database_url.startswith("postgresql://")
    assert settings.db_pool_max_size >= 1


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@example:6543/x")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "3")
    settings = Settings(_env_file=None)
    assert settings.database_url == "postgresql://u:p@example:6543/x"
    assert settings.db_pool_max_size == 3
