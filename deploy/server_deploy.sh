#!/bin/bash
# ============================================================
# 企微中转 MCP Gateway - 服务器部署脚本
# 目标机：Linux，MySQL 与应用同台
# 用法：在代码根目录执行 bash deploy/server_deploy.sh
# 脚本自动定位代码所在目录，无需手动指定路径
# ============================================================
set -euo pipefail

# 自动定位：取脚本所在目录的上一层（即代码根目录）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

trim_env_value() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

read_env_value() {
  local key="$1"
  local line value
  line="$(grep -m1 -E "^${key}=" "$APP_DIR/.env" 2>/dev/null || true)"
  if [ -z "$line" ]; then
    printf ''
    return
  fi
  value="${line#*=}"
  value="${value%$'\r'}"
  value="$(trim_env_value "$value")"
  if [[ "$value" == \"*\" && "$value" == *\" ]] ||
     [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  value="$(trim_env_value "$value")"
  printf '%s' "$value"
}

set_env_value() {
  local key="$1"
  local value="$2"
  local temp_file
  if ! temp_file="$(mktemp "$APP_DIR/.env.tmp.XXXXXX")"; then
    return 1
  fi
  if ! awk -v key="$key" -v value="$value" '
    BEGIN { replaced = 0 }
    index($0, key "=") == 1 {
      if (!replaced) {
        print key "=" value
        replaced = 1
      }
      next
    }
    { print }
    END {
      if (!replaced) print key "=" value
    }
  ' "$APP_DIR/.env" > "$temp_file"; then
    rm -f -- "$temp_file"
    return 1
  fi
  if ! chmod 600 "$temp_file"; then
    rm -f -- "$temp_file"
    return 1
  fi
  if ! mv -f -- "$temp_file" "$APP_DIR/.env"; then
    rm -f -- "$temp_file"
    return 1
  fi
}

is_example_password() {
  case "$1" in
    ""|"CHANGE_ME"|"database_password_here"|"migration_password_here"|"admin_password_here"|"<强密码，与开发库不同>"|"<强密码，登录管理后台用>") return 0 ;;
    *) return 1 ;;
  esac
}

is_example_mcp_token_hmac_key() {
  case "$1" in
    ""|"CHANGE_ME"|"<强随机串>"|"<独立强随机串>"|"MCP_TOKEN_HMAC_KEY"|"<MCP_TOKEN_HMAC_KEY>"|"PoC_DEFAULT_KEY_DO_NOT_USE_IN_PRODUCTION_32bytes!") return 0 ;;
    *) return 1 ;;
  esac
}

is_example_mcp_token_plaintext_key() {
  case "$1" in
    ""|"CHANGE_ME"|"<强随机串>"|"<独立强随机串>"|"MCP_TOKEN_PLAINTEXT_KEY"|"<MCP_TOKEN_PLAINTEXT_KEY>"|"PoC_DEFAULT_KEY_DO_NOT_USE_IN_PRODUCTION_32bytes!"|"replace_with_plaintext_key") return 0 ;;
    *) return 1 ;;
  esac
}

byte_length() {
  LC_ALL=C printf '%s' "$1" | wc -c | tr -d '[:space:]'
}

validate_positive_decimal() {
  local target_name="$1"
  local raw_value="$2"
  local upper_bound="$3"
  local normalized="$raw_value"
  case "$normalized" in
    ""|*[!0-9]*)
      echo "❌ $target_name 必须为十进制正整数" >&2
      return 1
      ;;
  esac
  while [ "${normalized#0}" != "$normalized" ]; do
    normalized="${normalized#0}"
  done
  normalized="${normalized:-0}"
  if [ "$normalized" = "0" ]; then
    echo "❌ $target_name 必须大于 0" >&2
    return 1
  fi
  if [ "${#normalized}" -gt "${#upper_bound}" ] ||
    { [ "${#normalized}" -eq "${#upper_bound}" ] && [[ "$normalized" > "$upper_bound" ]]; }; then
    echo "❌ $target_name 超出允许上限 $upper_bound" >&2
    return 1
  fi
  printf -v "$target_name" '%s' "$normalized"
}

echo "代码目录: $APP_DIR"

echo ""
echo "===== 1. 检查 Docker ====="
if ! command -v docker &>/dev/null; then
  echo "Docker 未安装，开始安装..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
else
  echo "Docker 已安装: $(docker --version)"
fi

if ! docker compose version &>/dev/null; then
  echo "docker compose 插件缺失，安装..."
  apt-get install -y docker-compose-plugin 2>/dev/null || yum install -y docker-compose-plugin 2>/dev/null
fi

echo ""
echo "===== 2. 检查代码 ====="
if [ ! -f "$APP_DIR/Dockerfile" ]; then
  echo "❌ 当前目录未发现 Dockerfile"
  echo "   请确认在代码根目录执行：cd wbsysc && bash deploy/server_deploy.sh"
  echo "   当前识别的代码目录: $APP_DIR"
  exit 1
fi
echo "✓ 代码就绪"

cd "$APP_DIR"

echo ""
echo "===== 3. 配置环境 ====="
if [ ! -f .env ]; then
  echo "首次运行：生成 .env 模板，请编辑填入真实值后重新运行本脚本"
  cp .env.prod.example .env
  echo ""
  echo "生产必填：APP_ENV=prod / WECOM_USE_MOCK=false / DB_PASSWORD / ADMIN_PASSWORD / CREDENTIAL_KEY / MCP_TOKEN_HMAC_KEY / MCP_TOKEN_PLAINTEXT_KEY / MCP_SERVICE_ENABLED"
  echo "  ADMIN_PASSWORD = 管理后台登录强密码"
  echo "  DB_PASSWORD    = MySQL websysc 账户密码（需与 MySQL 一致）"
  echo "  DB_HOST 已默认 host.docker.internal（同台部署容器访问宿主MySQL）"
  echo "  CREDENTIAL_KEY 和 MCP_TOKEN_HMAC_KEY 可保留模板占位符，下次运行会分别自动生成 32 字节以上强密钥"
  echo "  MCP_TOKEN_PLAINTEXT_KEY 必须在首次启用前手工配置，部署脚本不会生成或轮换它"
  echo "  三把密钥必须两两独立；弱的自定义值会被拒绝"
  echo ""
  echo "编辑：vim $APP_DIR/.env"
  exit 1
fi

# DB_HOST 同台部署用 host.docker.internal（compose 已配 extra_hosts）
sed -i 's|^DB_HOST=.*|DB_HOST=host.docker.internal|' .env 2>/dev/null || true

# CREDENTIAL_KEY 为空或仍为模板占位符时生成；非空弱值不静默覆盖
CREDENTIAL_KEY="$(read_env_value CREDENTIAL_KEY)"
if [ -z "$CREDENTIAL_KEY" ] || [ "$CREDENTIAL_KEY" = "<强随机串>" ]; then
  if command -v python3 &>/dev/null; then
    GENERATED_KEY="$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")"
  elif command -v openssl &>/dev/null; then
    GENERATED_KEY="$(openssl rand -hex 32)"
  else
    echo "❌ 无法生成 CREDENTIAL_KEY：需要 python3 或 openssl"
    exit 1
  fi
  if grep -q '^CREDENTIAL_KEY=' .env; then
    sed -i "s|^CREDENTIAL_KEY=.*|CREDENTIAL_KEY=$GENERATED_KEY|" .env
  else
    printf '\nCREDENTIAL_KEY=%s\n' "$GENERATED_KEY" >> .env
  fi
  unset GENERATED_KEY
  CREDENTIAL_KEY="$(read_env_value CREDENTIAL_KEY)"
  echo "✓ 已自动生成 CREDENTIAL_KEY"
fi

# MCP Token HMAC 密钥必须与凭证加密密钥独立生成，禁止复用或派生。
MCP_TOKEN_HMAC_KEY="$(read_env_value MCP_TOKEN_HMAC_KEY)"
if is_example_mcp_token_hmac_key "$MCP_TOKEN_HMAC_KEY"; then
  if command -v python3 &>/dev/null; then
    GENERATED_MCP_TOKEN_HMAC_KEY="$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")"
  elif command -v openssl &>/dev/null; then
    GENERATED_MCP_TOKEN_HMAC_KEY="$(openssl rand -hex 32)"
  else
    echo "❌ 无法自动生成 MCP_TOKEN_HMAC_KEY：需要 python3 或 openssl"
    exit 1
  fi
  if grep -q '^MCP_TOKEN_HMAC_KEY=' .env; then
    sed -i "s|^MCP_TOKEN_HMAC_KEY=.*|MCP_TOKEN_HMAC_KEY=$GENERATED_MCP_TOKEN_HMAC_KEY|" .env
  else
    printf '\nMCP_TOKEN_HMAC_KEY=%s\n' "$GENERATED_MCP_TOKEN_HMAC_KEY" >> .env
  fi
  unset GENERATED_MCP_TOKEN_HMAC_KEY
  MCP_TOKEN_HMAC_KEY="$(read_env_value MCP_TOKEN_HMAC_KEY)"
  echo "✓ 已自动生成 MCP_TOKEN_HMAC_KEY"
fi

# Reveal ciphertext depends on this exact key. Never generate or rotate it here.
MCP_TOKEN_PLAINTEXT_KEY="$(read_env_value MCP_TOKEN_PLAINTEXT_KEY)"
REQUESTED_MCP_SERVICE_ENABLED="$(read_env_value MCP_SERVICE_ENABLED)"

chmod 600 .env

CONFIG_INVALID=0
if [ "$(read_env_value APP_ENV)" != "prod" ]; then
  echo "❌ APP_ENV 必须为 prod"
  CONFIG_INVALID=1
fi
if [ "$(read_env_value WECOM_USE_MOCK)" != "false" ]; then
  echo "❌ WECOM_USE_MOCK 必须为 false"
  CONFIG_INVALID=1
fi
DB_PASSWORD="$(read_env_value DB_PASSWORD)"
if is_example_password "$DB_PASSWORD"; then
  echo "❌ DB_PASSWORD 不能为空或使用示例值"
  CONFIG_INVALID=1
fi
ADMIN_PASSWORD="$(read_env_value ADMIN_PASSWORD)"
if is_example_password "$ADMIN_PASSWORD"; then
  echo "❌ ADMIN_PASSWORD 不能为空或使用示例值"
  CONFIG_INVALID=1
fi
if [ "$CREDENTIAL_KEY" = "<强随机串>" ] || [ "$(byte_length "$CREDENTIAL_KEY")" -lt 32 ]; then
  echo "❌ CREDENTIAL_KEY 必须为非示例值且至少 32 UTF-8 字节"
  CONFIG_INVALID=1
fi
DB_USER="$(read_env_value DB_USER)"
DB_USER="${DB_USER:-websysc}"
DB_MIGRATION_USER="${DB_MIGRATION_USER:-}"
DB_MIGRATION_PASSWORD="${DB_MIGRATION_PASSWORD:-}"
if [ -z "$DB_MIGRATION_USER" ] || is_example_password "$DB_MIGRATION_PASSWORD"; then
  echo "❌ DB_MIGRATION_USER/DB_MIGRATION_PASSWORD 必须使用独立迁移账户的真实值"
  CONFIG_INVALID=1
fi
if [ "$DB_MIGRATION_USER" = "$DB_USER" ]; then
  echo "❌ DB_MIGRATION_USER 必须与运行时 DB_USER 不同"
  CONFIG_INVALID=1
fi
if is_example_mcp_token_hmac_key "$MCP_TOKEN_HMAC_KEY" || [ "$(byte_length "$MCP_TOKEN_HMAC_KEY")" -lt 32 ]; then
  echo "❌ MCP_TOKEN_HMAC_KEY 必须为非示例值且至少 32 UTF-8 字节"
  CONFIG_INVALID=1
fi
if [ "$MCP_TOKEN_HMAC_KEY" = "$CREDENTIAL_KEY" ]; then
  echo "❌ MCP_TOKEN_HMAC_KEY 必须与 CREDENTIAL_KEY 保持独立"
  CONFIG_INVALID=1
fi
if is_example_mcp_token_plaintext_key "$MCP_TOKEN_PLAINTEXT_KEY" || [ "$(byte_length "$MCP_TOKEN_PLAINTEXT_KEY")" -lt 32 ]; then
  echo "❌ MCP_TOKEN_PLAINTEXT_KEY 必须为非示例值且至少 32 UTF-8 字节"
  CONFIG_INVALID=1
fi
if [ "$MCP_TOKEN_PLAINTEXT_KEY" = "$CREDENTIAL_KEY" ] || [ "$MCP_TOKEN_PLAINTEXT_KEY" = "$MCP_TOKEN_HMAC_KEY" ]; then
  echo "❌ MCP_TOKEN_PLAINTEXT_KEY 必须与 CREDENTIAL_KEY 和 MCP_TOKEN_HMAC_KEY 两两独立"
  CONFIG_INVALID=1
fi
case "$REQUESTED_MCP_SERVICE_ENABLED" in
  "true"|"false") ;;
  *)
    echo "❌ MCP_SERVICE_ENABLED 必须明确设置为 true 或 false"
    CONFIG_INVALID=1
    ;;
esac
if [ "$CONFIG_INVALID" -ne 0 ]; then
  echo "❌ .env 生产配置校验失败，尚未拉取镜像或启动应用"
  exit 1
fi
unset ADMIN_PASSWORD CREDENTIAL_KEY MCP_TOKEN_HMAC_KEY MCP_TOKEN_PLAINTEXT_KEY
echo "✓ .env 生产配置校验通过"

validate_positive_decimal HEALTH_MAX_ATTEMPTS "${HEALTH_MAX_ATTEMPTS:-30}" 60
validate_positive_decimal HEALTH_RETRY_SECONDS "${HEALTH_RETRY_SECONDS:-2}" 10

wait_for_health_state() {
  local expected="$1"
  local attempt
  local health_body
  for ((attempt = 1; attempt <= HEALTH_MAX_ATTEMPTS; attempt++)); do
    if health_body="$(curl -fsS http://127.0.0.1:8001/health 2>/dev/null)" &&
      printf '%s' "$health_body" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' &&
      printf '%s' "$health_body" | grep -Eq "\"mcp_service_enabled\"[[:space:]]*:[[:space:]]*$expected[[:space:]]*[,}]"; then
      return 0
    fi
    if [ "$attempt" -lt "$HEALTH_MAX_ATTEMPTS" ]; then
      sleep "$HEALTH_RETRY_SECONDS"
    fi
  done
  return 1
}

rollback_service_flag() {
  echo "⚠️  发布未完成，正在恢复并验证 MCP 服务禁用状态"
  if ! set_env_value MCP_SERVICE_ENABLED false; then
    echo "❌ 严重：无法原子写入禁用状态"
    return 1
  fi
  # Always recreate: the pre-deploy process may still be effectively enabled
  # even though the environment file now requests false.
  if ! docker compose up -d --force-recreate; then
    echo "❌ 严重：禁用状态容器重建失败；配置已保持 false"
    return 1
  fi
  if ! wait_for_health_state false; then
    echo "❌ 严重：禁用状态健康恢复失败；配置已保持 false"
    return 1
  fi
  echo "✓ 已恢复 MCP 服务禁用状态；数据库和 008 数据均保留"
}

rollout_exit() {
  local original_status="$1"
  trap - EXIT INT TERM
  if [ "$original_status" -eq 0 ] || [ "${ROLLOUT_COMPLETE:-0}" -eq 1 ]; then
    exit "$original_status"
  fi
  if ! rollback_service_flag; then
    echo "❌ 严重：发布失败，且禁用状态恢复未通过验证"
  fi
  exit "$original_status"
}

rollout_signal() {
  local signal_status="$1"
  trap - INT TERM
  exit "$signal_status"
}

ROLLOUT_COMPLETE=0
trap 'rollout_exit "$?"' EXIT
trap 'rollout_signal 130' INT
trap 'rollout_signal 143' TERM

# A requested true rollout must still start in the disabled state. This atomic
# write happens before any new image is started and retains all schema/data.
# BEGIN ROLLOUT MUTATIONS
set_env_value MCP_SERVICE_ENABLED false

ENV_MIGRATION_HOST="$(read_env_value DB_MIGRATION_HOST)"
MIGRATION_HOST="${DB_MIGRATION_HOST:-${ENV_MIGRATION_HOST:-127.0.0.1}}"
DB_PORT="$(read_env_value DB_PORT)"
DB_PORT="${DB_PORT:-3306}"
DB_NAME="$(read_env_value DB_NAME)"
DB_NAME="${DB_NAME:-websysc}"

echo ""
echo "===== 3.1 MySQL 同台访问检查（关键） ====="
echo "容器访问宿主 MySQL 需要："
echo "  ① MySQL bind-address = 0.0.0.0（非仅 [IP]）"
echo "  ② 授权配置的数据库账户可从容器网段登录："
echo "     mysql -uroot -p 执行:"
echo "       ALTER USER '$DB_USER'@'%' IDENTIFIED BY '你的强密码';"
echo "       GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX ON \`$DB_NAME\`.* TO '$DB_USER'@'%';"
echo "     中心 schema: \`$DB_NAME\`"
echo "     既有租户 schema: 从 $DB_NAME.tenant_config.schema_name 读取后，逐个执行:"
echo "       GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX ON \`wbd_<tenant>\`.* TO '$DB_USER'@'%';"
echo "     新租户 schema 需由 DBA 预先创建并单独授权；不要授予全库权限或转授权限"
echo "     发布迁移使用独立账户 '$DB_MIGRATION_USER'；其 ROUTINE/DDL 权限不得授予运行时账户"
echo "       FLUSH PRIVILEGES;"
echo "  ③ /etc/mysql/my.cnf 或 mariadb.conf 的 bind-address 改 [IP] 后重启 mysql"
echo ""

echo "===== 4. 执行数据库升级 ====="
if ! command -v mysql &>/dev/null; then
  echo "❌ 未找到宿主 mysql 客户端，请安装后重试（迁移脚本包含 DELIMITER，必须使用 mysql CLI）"
  exit 1
fi
MIGRATIONS=(
  "sql/004_gateway_hardening.sql"
  "sql/005_mcp_call_log.sql"
  "sql/006_connection_platform.sql"
  "sql/007_tenant_auth.sql"
  "sql/008_mcp_service.sql"
)
for migration in "${MIGRATIONS[@]}"; do
  if [ ! -f "$APP_DIR/$migration" ]; then
    echo "❌ 未找到 $migration"
    exit 1
  fi
done

echo "使用宿主 mysql CLI 按 004 → 005 → 006 → 007 → 008 执行迁移（迁移主机默认 127.0.0.1，可用 DB_MIGRATION_HOST 覆盖）"
for migration in "${MIGRATIONS[@]}"; do
  echo "执行 $migration"
  if ! MYSQL_PWD="$DB_MIGRATION_PASSWORD" mysql --protocol=TCP \
    --host="$MIGRATION_HOST" --port="$DB_PORT" --user="$DB_MIGRATION_USER" "$DB_NAME" \
    < "$APP_DIR/$migration"; then
    echo "❌ 数据库迁移失败（$migration），尚未拉取镜像或启动新应用"
    exit 1
  fi
done
unset DB_PASSWORD DB_MIGRATION_USER DB_MIGRATION_PASSWORD
echo "✓ 004、005、006、007、008 数据库迁移完成"

echo ""
echo "===== 5. 拉取镜像（GitHub Actions 已构建推送到 GHCR） ====="
# 先试拉（GHCR 公开 package 可匿名拉；私有需 docker login）
if docker pull ghcr.io/hkxiaoyao/wbsysc:latest 2>&1 | tee /tmp/pull.log | tail -3; then
  echo "✓ 镜像拉取成功"
else
  if grep -qE "unauthorized|forbidden|denied|not found" /tmp/pull.log; then
    echo "⚠️  GHCR 镜像不可匿名访问。两种解决："
    echo "  方式A(登录私有package): echo \$GHCR_TOKEN | docker login ghcr.io -u hkxiaoyao --password-stdin"
    echo "    普通 package 改公开免登录: GitHub package 页→Package settings→Change visibility→Public"
    echo "  方式B(本机构建): docker compose build"
    echo "镜像可能还没构建过(需先 push 触发 GitHub Actions)。"
    read -p "是否本机构建? [y/N] " yn
    if [ "$yn" = "y" ] || [ "$yn" = "Y" ]; then
      docker compose build
    else
      exit 1
    fi
  else
    docker compose build   # 其他错误兜底本机构建
  fi
fi
docker images | grep wbsysc | head -2 || true

echo ""
echo "===== 6. 启动 ====="
docker compose up -d --force-recreate

echo ""
echo "===== 7. 验证禁用阶段 ====="
if ! wait_for_health_state false; then
  echo "❌ 禁用阶段健康检查未通过，部署失败（MCP_SERVICE_ENABLED 保持 false）"
  docker compose ps || true
  exit 1
fi
echo "✓ 禁用阶段健康检查通过"

if [ "$REQUESTED_MCP_SERVICE_ENABLED" = "true" ]; then
  echo ""
  echo "===== 8. 启用 MCP 服务并验证 ====="
  set_env_value MCP_SERVICE_ENABLED true
  docker compose up -d --force-recreate
  if ! wait_for_health_state true; then
    echo "❌ MCP 服务启用阶段健康检查未通过"
    exit 1
  fi
  echo "✓ MCP 服务启用阶段健康检查通过"
else
  echo "✓ MCP 服务按请求保持禁用"
fi

docker compose ps
ROLLOUT_COMPLETE=1
trap - EXIT INT TERM

echo ""
echo "===== 完成 ====="
echo "管理后台: http://<服务器IP>:8001/admin/ui/"
echo "MCP地址:  http://<服务器IP>:8001/mcp   (workbuddy 用 Bearer Token 连)"
echo ""
echo "下一步：在容器内接入第一个租户"
echo "  docker compose exec wbsysc python -m app.tenant_init \\"
echo "    --tenant-id tenant1 --corpid wwXXX --secret XXX --token <随机串> \\"
echo "    --contact-secret XXXX --modules report,approval,checkin --display 测试客户1"
