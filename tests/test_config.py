"""
Fail-fast startup configuration (Phase 0, Module 0.2).

validate_config() splits into (warnings, critical_errors): warnings are the
pre-existing "a call will fail later" checks; critical_errors are new —
a public JWT secret or an unrecognized ENVIRONMENT value outside local
development, which src/main.py's lifespan handler now refuses to boot on
in production. These are pure-Python checks; no live Postgres/AGE needed.
"""
import pytest

import src.config as config
import src.main as main_module


# ── validate_config(): warnings vs. critical ─────────────────────────────────

def test_default_jwt_secret_is_fine_in_development(monkeypatch):
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "JWT_SECRET_KEY", "your-secret-key-for-dev")

    _, critical = config.validate_config()

    assert not any("JWT_SECRET_KEY" in c for c in critical)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_default_jwt_secret_is_critical_outside_development(monkeypatch, environment):
    monkeypatch.setattr(config, "ENVIRONMENT", environment)
    monkeypatch.setattr(config, "JWT_SECRET_KEY", "your-secret-key-for-dev")

    _, critical = config.validate_config()

    assert any("JWT_SECRET_KEY" in c for c in critical)


def test_real_jwt_secret_is_never_flagged(monkeypatch):
    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    monkeypatch.setattr(config, "JWT_SECRET_KEY", "a-real-randomly-generated-secret")

    _, critical = config.validate_config()

    assert not any("JWT_SECRET_KEY" in c for c in critical)


@pytest.mark.parametrize("value", ["Production", "prod", "", "developement", "dev"])
def test_invalid_environment_value_is_critical(monkeypatch, value):
    monkeypatch.setattr(config, "ENVIRONMENT", value)
    # Isolate the enum check from the JWT-secret check above.
    monkeypatch.setattr(config, "JWT_SECRET_KEY", "a-real-randomly-generated-secret")

    _, critical = config.validate_config()

    assert any("ENVIRONMENT" in c for c in critical)


@pytest.mark.parametrize("value", ["development", "staging", "production"])
def test_valid_environment_values_are_never_flagged(monkeypatch, value):
    monkeypatch.setattr(config, "ENVIRONMENT", value)
    monkeypatch.setattr(config, "JWT_SECRET_KEY", "a-real-randomly-generated-secret")

    _, critical = config.validate_config()

    assert not any("ENVIRONMENT" in c for c in critical)


def test_missing_database_url_is_a_warning(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")

    warnings, _ = config.validate_config()

    assert any("DATABASE_URL" in w for w in warnings)


def test_air_gap_mode_without_local_llm_is_a_warning(monkeypatch):
    monkeypatch.setattr(config, "AIR_GAP_MODE", True)
    monkeypatch.setattr(config, "LOCAL_LLM_URL", "")

    warnings, _ = config.validate_config()

    assert any("AIR_GAP_MODE" in w for w in warnings)


def test_air_gap_mode_with_local_llm_configured_is_not_flagged(monkeypatch):
    monkeypatch.setattr(config, "AIR_GAP_MODE", True)
    monkeypatch.setattr(config, "LOCAL_LLM_URL", "http://localhost:8001")

    warnings, _ = config.validate_config()

    assert not any("AIR_GAP_MODE" in w for w in warnings)


def test_missing_mcp_database_url_is_a_warning_when_postgres_is_configured(monkeypatch):
    """Falling back to the superuser DATABASE_URL for the MCP SQL route
    (migrations/009_mcp_readonly_role.sql) must be visible at startup, not silent."""
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql+asyncpg://postgres:dev@localhost:5432/muhafiz")
    monkeypatch.setattr(config, "MCP_DATABASE_URL", "")

    warnings, _ = config.validate_config()

    assert any("MCP_DATABASE_URL" in w for w in warnings)


def test_mcp_database_url_configured_is_not_flagged(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql+asyncpg://postgres:dev@localhost:5432/muhafiz")
    monkeypatch.setattr(config, "MCP_DATABASE_URL", "postgresql://muhafiz_mcp_readonly:pw@localhost:5432/muhafiz")

    warnings, _ = config.validate_config()

    assert not any("MCP_DATABASE_URL" in w for w in warnings)


def test_missing_mcp_database_url_not_double_flagged_without_database_url(monkeypatch):
    """No point warning about the MCP fallback when there's no DATABASE_URL
    to fall back to either — that's already its own, separate warning."""
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "MCP_DATABASE_URL", "")

    warnings, _ = config.validate_config()

    assert not any("MCP_DATABASE_URL" in w for w in warnings)


# ── main.py lifespan: Postgres requirement gate ──────────────────────────────

def _quiet_startup_side_effects(monkeypatch, tmp_path):
    """Neutralize lifespan side effects unrelated to what these tests check."""
    monkeypatch.setattr(main_module, "ensure_directories", lambda: None)
    monkeypatch.setattr("src.observability.errors.install", lambda: None)
    # DB_PATH's "archive the old SQLite file" step touches real disk under
    # is_postgres_configured()==True — redirect it into tmp_path so tests
    # never touch this repo's actual data/pipeline_logs.db.
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pipeline_logs.db")


async def test_lifespan_refuses_to_start_without_postgres_by_default(monkeypatch, tmp_path):
    """
    REQUIRE_POSTGRES defaults to True: the legacy SQLite schema predates the
    entire case/auth/RBAC model, so silently falling back to it (the old
    behavior) is the bug this module fixes, not a feature to preserve.
    """
    _quiet_startup_side_effects(monkeypatch, tmp_path)
    monkeypatch.setattr("src.database.postgres.is_postgres_configured", lambda: False)
    monkeypatch.setattr(config, "REQUIRE_POSTGRES", True)

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_allows_legacy_sqlite_when_explicitly_opted_out(monkeypatch, tmp_path):
    _quiet_startup_side_effects(monkeypatch, tmp_path)
    monkeypatch.setattr("src.database.postgres.is_postgres_configured", lambda: False)
    monkeypatch.setattr(config, "REQUIRE_POSTGRES", False)
    monkeypatch.setattr("src.database.db.init_db", lambda: None)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "JWT_SECRET_KEY", "your-secret-key-for-dev")

    async with main_module.lifespan(main_module.app):
        pass  # must not raise — this is the explicit, documented opt-out


async def test_lifespan_raises_on_critical_config_errors_in_production(monkeypatch, tmp_path):
    _quiet_startup_side_effects(monkeypatch, tmp_path)
    monkeypatch.setattr("src.database.postgres.is_postgres_configured", lambda: True)

    async def fake_init_postgres():
        return None

    monkeypatch.setattr("src.database.postgres.init_postgres", fake_init_postgres)
    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    monkeypatch.setattr(config, "JWT_SECRET_KEY", "your-secret-key-for-dev")  # still the public default

    with pytest.raises(RuntimeError, match="critical configuration"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_lifespan_warns_but_does_not_raise_on_critical_errors_outside_production(monkeypatch, tmp_path):
    """Local onboarding must not break: only 'production' is refused at startup."""
    _quiet_startup_side_effects(monkeypatch, tmp_path)
    monkeypatch.setattr("src.database.postgres.is_postgres_configured", lambda: True)

    async def fake_init_postgres():
        return None

    monkeypatch.setattr("src.database.postgres.init_postgres", fake_init_postgres)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "JWT_SECRET_KEY", "your-secret-key-for-dev")

    async with main_module.lifespan(main_module.app):
        pass  # must not raise
