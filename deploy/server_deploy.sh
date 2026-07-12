#!/bin/bash
# ============================================================
# 企微中转 MCP Gateway - 服务器部署脚本
# 目标机：Linux，MySQL 与应用同台
# 用法：在代码根目录执行 bash deploy/server_deploy.sh
# 脚本自动定位代码所在目录，无需手动指定路径
# ============================================================
set -e

# 自动定位：取脚本所在目录的上一层（即代码根目录）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

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
  echo "必填项：DB_PASSWORD / ADMIN_PASSWORD"
  echo "  ADMIN_PASSWORD = 管理后台登录强密码"
  echo "  DB_PASSWORD    = MySQL websysc 账户密码（需与 MySQL 一致）"
  echo "  DB_HOST 已默认 host.docker.internal（同台部署容器访问宿主MySQL）"
  echo "  CREDENTIAL_KEY 留空，下次运行自动生成"
  echo ""
  echo "编辑：vim $APP_DIR/.env"
  exit 0
fi

# DB_HOST 同台部署用 host.docker.internal（compose 已配 extra_hosts）
sed -i 's|^DB_HOST=.*|DB_HOST=host.docker.internal|' .env 2>/dev/null || true

# 生成 CREDENTIAL_KEY 若空
if grep -q "^CREDENTIAL_KEY=$" .env; then
  KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))" 2>/dev/null || openssl rand -base64 48)
  sed -i "s|^CREDENTIAL_KEY=.*|CREDENTIAL_KEY=$KEY|" .env
  echo "✓ 已自动生成 CREDENTIAL_KEY"
fi

chmod 600 .env
echo "✓ .env 已就绪"

echo ""
echo "===== 3.1 MySQL 同台访问检查（关键） ====="
echo "容器访问宿主 MySQL 需要："
echo "  ① MySQL bind-address = 0.0.0.0（非仅 [IP]）"
echo "  ② 授权 websysc 账户可从容器网段登录："
echo "     mysql -uroot -p 执行:"
echo "       ALTER USER 'websysc'@'%' IDENTIFIED BY '你的强密码';"
echo "       GRANT ALL PRIVILEGES ON *.* TO 'websysc'@'%' WITH GRANT OPTION;"
echo "       FLUSH PRIVILEGES;"
echo "  ③ /etc/mysql/my.cnf 或 mariadb.conf 的 bind-address 改 [IP] 后重启 mysql"
echo ""

echo "===== 4. 拉取镜像（GitHub Actions 已构建推送到 GHCR） ====="
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
docker images | grep wbsysc | head -2

echo ""
echo "===== 5. 启动 ====="
docker compose up -d

echo ""
echo "===== 6. 等待健康检查 ====="
sleep 10
docker compose ps

echo ""
echo "===== 7. 验证 ====="
if curl -fs http://127.0.0.1:8001/health 2>/dev/null; then
  echo ""
  echo "✅ 部署成功"
else
  echo "⚠️  健康检查未通过，查日志："
  echo "    docker compose logs --tail=50"
fi

echo ""
echo "===== 完成 ====="
echo "管理后台: http://<服务器IP>:8001/admin/ui/"
echo "MCP地址:  http://<服务器IP>:8001/mcp   (workbuddy 用 Bearer Token 连)"
echo ""
echo "下一步：在容器内接入第一个租户"
echo "  docker compose exec wbsysc python -m app.tenant_init \\"
echo "    --tenant-id tenant1 --corpid wwXXX --secret XXX --token <随机串> \\"
echo "    --contact-secret XXXX --modules report,approval,checkin --display 测试客户1"