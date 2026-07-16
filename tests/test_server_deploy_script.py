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
    assert 'MYSQL_PWD="$DB_PASSWORD" mysql' in script


def test_deployer_documents_schema_scoped_least_privilege_grants():
    script = _script()
    upper = script.upper()

    assert "GRANT ALL" not in upper
    assert "WITH GRANT OPTION" not in upper
    assert " ON *.* " not in upper
    assert (
        "SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, "
        "CREATE ROUTINE, ALTER ROUTINE, EXECUTE"
    ) in script
    assert (
        r"SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX ON \`wbd_<tenant>\`.*"
        in script
    )
    assert r"schema: \`$DB_NAME\`" in script
    assert "tenant_config.schema_name" in script
    assert 'echo "$DB_PASSWORD"' not in script
    assert 'printf \'%s\' "$DB_PASSWORD"' not in script
