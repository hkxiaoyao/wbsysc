from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _script() -> str:
    return (ROOT / "deploy" / "server_deploy.sh").read_text(encoding="utf-8")


def test_deployer_runs_all_migrations_in_order_before_starting_new_image():
    script = _script()

    migration_positions = [
        script.index(f"sql/{name}")
        for name in (
            "004_gateway_hardening.sql",
            "005_mcp_call_log.sql",
            "006_connection_platform.sql",
        )
    ]

    assert migration_positions == sorted(migration_positions)
    assert migration_positions[-1] < script.index("docker pull")
    assert migration_positions[-1] < script.index("docker compose up -d")
    assert 'MYSQL_PWD="$DB_MIGRATION_PASSWORD" mysql' in script
    assert '--user="$DB_MIGRATION_USER"' in script


def test_deployer_documents_schema_scoped_least_privilege_grants():
    script = _script()
    upper = script.upper()

    assert "GRANT ALL" not in upper
    assert "WITH GRANT OPTION" not in upper
    assert " ON *.* " not in upper
    runtime_grant = (
        "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX ON "
        r"\`$DB_NAME\`.* TO '$DB_USER'@'%'"
    )
    assert runtime_grant in script
    assert "ROUTINE, ALTER ROUTINE" not in runtime_grant
    assert (
        r"SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX ON \`wbd_<tenant>\`.*"
        in script
    )
    assert r"schema: \`$DB_NAME\`" in script
    assert "tenant_config.schema_name" in script
    assert 'DB_MIGRATION_USER="${DB_MIGRATION_USER:-}"' in script
    assert 'DB_MIGRATION_PASSWORD="${DB_MIGRATION_PASSWORD:-}"' in script
    assert "read_env_value DB_MIGRATION_PASSWORD" not in script
    default_user = 'DB_USER="${DB_USER:-websysc}"'
    distinct_check = 'if [ "$DB_MIGRATION_USER" = "$DB_USER" ]'
    assert default_user in script
    assert distinct_check in script
    assert script.index(default_user) < script.index(distinct_check)
    assert 'echo "$DB_PASSWORD"' not in script
    assert 'printf \'%s\' "$DB_PASSWORD"' not in script
