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
            "007_tenant_auth.sql",
            "008_mcp_service.sql",
            "009_tenant_identity_boundary.sql",
        )
    ]

    assert migration_positions == sorted(migration_positions)
    assert migration_positions[-1] < script.index("docker pull")
    assert migration_positions[-1] < script.index(
        "\ndocker compose up -d --force-recreate\n"
    )
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


def test_deployer_validates_and_unsets_plaintext_token_key():
    script = _script()

    assert "read_env_value MCP_TOKEN_PLAINTEXT_KEY" in script
    assert "is_example_mcp_token_plaintext_key" in script
    assert 'byte_length "$MCP_TOKEN_PLAINTEXT_KEY"' in script
    assert '"$MCP_TOKEN_PLAINTEXT_KEY" = "$CREDENTIAL_KEY"' in script
    assert '"$MCP_TOKEN_PLAINTEXT_KEY" = "$MCP_TOKEN_HMAC_KEY"' in script
    assert "replace_with_plaintext_key" in script
    assert "unset ADMIN_PASSWORD CREDENTIAL_KEY MCP_TOKEN_HMAC_KEY MCP_TOKEN_PLAINTEXT_KEY" in script
    assert "GENERATED_MCP_TOKEN_PLAINTEXT_KEY" not in script


def test_deployer_stages_requested_service_flag_and_rolls_back_safely():
    script = _script()

    assert 'REQUESTED_MCP_SERVICE_ENABLED="$(read_env_value MCP_SERVICE_ENABLED)"' in script
    assert '"true"|"false"' in script
    assert "set_env_value MCP_SERVICE_ENABLED false" in script
    assert "wait_for_health_state false" in script
    assert "set_env_value MCP_SERVICE_ENABLED true" in script
    assert "wait_for_health_state true" in script
    assert "rollback_service_flag" in script
    assert "docker compose up -d --force-recreate" in script
    assert "validate_positive_decimal HEALTH_MAX_ATTEMPTS" in script
    assert "for ((attempt = 1; attempt <= HEALTH_MAX_ATTEMPTS; attempt++))" in script
    assert "sleep 10" not in script

    forced_false = script.index("set_env_value MCP_SERVICE_ENABLED false")
    first_start = script.index("\ndocker compose up -d --force-recreate\n")
    false_health = script.index("wait_for_health_state false", first_start)
    true_write = script.index("set_env_value MCP_SERVICE_ENABLED true", false_health)
    true_health = script.index("wait_for_health_state true", true_write)
    assert forced_false < first_start < false_health < true_write < true_health


def test_rollout_trap_covers_every_post_mutation_failure_and_signal():
    script = _script()

    exit_trap = 'trap \'rollout_exit "$?"\' EXIT'
    signal_traps = (
        "trap 'rollout_signal 130' INT",
        "trap 'rollout_signal 143' TERM",
    )
    disarm = "trap - EXIT INT TERM"
    first_mutation = "# BEGIN ROLLOUT MUTATIONS\nset_env_value MCP_SERVICE_ENABLED false"

    assert exit_trap in script
    assert all(item in script for item in signal_traps)
    assert first_mutation in script
    assert script.index(exit_trap) < script.index(first_mutation)
    assert script.index(signal_traps[0]) < script.index(first_mutation)
    assert script.index(signal_traps[1]) < script.index(first_mutation)
    assert script.count("rollback_service_flag") == 2
    assert 'local original_status="$1"' in script
    assert 'exit "$original_status"' in script
    assert "ROLLOUT_COMPLETE=1" in script

    rollout = script[script.index(first_mutation) :]
    assert rollout.index("sql/004_gateway_hardening.sql") < rollout.index("docker pull")
    assert rollout.index("docker pull") < rollout.index(
        "\ndocker compose up -d --force-recreate\n"
    )
    assert rollout.index("wait_for_health_state false") < rollout.index(
        "set_env_value MCP_SERVICE_ENABLED true"
    )
    assert rollout.index("wait_for_health_state true") < rollout.index(
        "ROLLOUT_COMPLETE=1"
    )
    assert rollout.index("ROLLOUT_COMPLETE=1") < rollout.index(disarm)


def test_health_retry_controls_are_plain_bounded_positive_decimals():
    script = _script()

    assert "validate_positive_decimal" in script
    assert 'validate_positive_decimal HEALTH_MAX_ATTEMPTS "${HEALTH_MAX_ATTEMPTS:-30}" 60' in script
    assert 'validate_positive_decimal HEALTH_RETRY_SECONDS "${HEALTH_RETRY_SECONDS:-2}" 10' in script
    assert "*[!0-9]*" in script
    assert 'if [ "$normalized" = "0" ]' in script
    assert 'if [ "${#normalized}" -gt "${#upper_bound}" ]' in script
    assert '[[ "$normalized" > "$upper_bound" ]]' in script
    assert "10#$" not in script
    assert "eval" not in script


def test_env_reader_trims_again_after_unquoting_for_key_validation_parity():
    script = _script()
    reader = script[script.index("read_env_value() {") : script.index("set_env_value() {")]

    trim_call = 'value="$(trim_env_value "$value")"'
    quote_removal = 'value="${value:1:${#value}-2}"'
    assert reader.count(trim_call) == 2
    assert reader.index(trim_call) < reader.index(quote_removal)
    assert reader.rindex(trim_call) > reader.index(quote_removal)
    assert "replace_with_plaintext_key" in script
    assert "LC_ALL=C" in script

    padded_examples = (
        '"  replace_with_plaintext_key  "',
        "'  replace_with_plaintext_key  '",
    )
    assert all(
        item.strip("\"'").strip() == "replace_with_plaintext_key"
        for item in padded_examples
    )
    credential_key = "c" * 32
    padded_equal_key = f'"  {credential_key}  "'
    assert padded_equal_key.strip('"').strip() == credential_key
    assert '"$MCP_TOKEN_PLAINTEXT_KEY" = "$CREDENTIAL_KEY"' in script
    utf8_boundary = ("界" * 10 + "a", "界" * 10 + "ab")
    assert [len(value.encode("utf-8")) for value in utf8_boundary] == [31, 32]
    assert 'byte_length "$MCP_TOKEN_PLAINTEXT_KEY"' in script


def test_compose_healthcheck_requires_health_shape_and_boolean_flag():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert 'payload.get("status") == "ok"' in compose
    assert 'isinstance(payload.get("mcp_service_enabled"), bool)' in compose
