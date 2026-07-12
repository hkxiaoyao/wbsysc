#!/bin/bash
# ============================================================
# 企微中转 MCP Gateway - 服务器部署脚本
# 目标机：Linux，MySQL 与应用同台，部署到 /root/app/websysc
# 用法：在服务器上 bash deploy/server_deploy.sh
# ============================================================
set -e

APP_DIR=/root/app/websysc

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
  yum install -y docker-compose-plugin 2>/dev/null || apt-get install -y docker-compose-plugin
fi

echo ""
echo "===== 2. 代码目录 ====="
mkdir -p /root/app
cd /root/app

# ===== 3. 代码上传 =====
# 方式一（推荐）：从 git 仓库拉
# git clone <你的私有仓库> websysc
#
# 方式二（无git）：从开发机 scp 上传
# 在开发机执行：
#   scp -r D:/app/wbsysc/app D:/app/wbsysc/admin-ui D:/app/wbsysc/sql \
#        D:/app/wbsysc/requirements.txt D:/app/wbsysc/Dockerfile \
#        D:/app/wbsysc/docker-compose.yml D:/app/wbsysc/.dockerignore \
#        D:/app/wbsysc/.env.prod.example D:/app/wbsysc/deploy \
#        root@211.159.172.117:/root/app/websysc/
#
# 下面的步骤假设代码已在 $APP_DIR

if [ ! -f "$APP_DIR/Dockerfile" ]; then
  echo "❌ $APP_DIR 未发现代码，请先按上面方式上传代码"
  exit 1
fi

cd $APP_DIR

echo ""
echo "===== 4. 配置环境 ====="
if [ ! -f .env ]; then
  cp .env.prod.example .env
  echo "已生成 .env，请编辑填入真实值后重新运行本脚本"
  echo "必填项：DB_PASSWORD / ADMIN_PASSWORD / CREDENTIAL_KEY"
  echo "  CREDENTIAL_KEY 生成: python3 -c \"import secrets;print(secrets.token_urlsafe(48))\""
  exit 0
fi

# 生成 CREDENTIAL_KEY 若空
if grep -q "^CREDENTIAL_KEY=$" .env; then
  KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))" 2>/dev/null || openssl rand -base64 48)
  sed -i "s|^CREDENTIAL_KEY=.*|CREDENTIAL_KEY=$KEY|" .env
  echo "已自动生成 CREDENTIAL_KEY"
fi

# DB_HOST 同台部署用 host.docker.internal（compose 已配 extra_hosts 映射到宿主）
# MySQL 需 bind-address=0.0.0.0（不能只监听 127.0.0.1），且授权 wbsysc_app@'%' 或 @'172.%'
sed -i 's|^DB_HOST=.*|DB_HOST=host.docker.internal|' .env 2>/dev/null || true

chmod 600 .env

echo ""
echo "===== 4.1 MySQL 访问检查（同台关键） ====="
echo "需确认宿主 MySQL："
echo "  ① bind-address = 0.0.0.0（不能只 127.0.0.1）"
echo "  ② 授权 wbsysc 账户能从容器网段登录："
echo "     GRANT ... TO 'wbsysc'@'172.%' IDENTIFIED BY '密码';  -- Docker 默认网段"
echo "     或 TO 'wbsysc'@'%';"
echo "如报 1130/2003 连接失败，先在宿主跑：mysql -uwebsysc -p -h 127.0.0.1 验证账户通"

echo ""
echo "===== 5. 构建镜像（首次约 3-6 分钟） ====="
docker compose build

echo ""
echo "===== 6. 启动 ====="
docker compose up -d

echo ""
echo "===== 7. 等待健康检查 ====="
sleep 10
docker compose ps

echo ""
echo "===== 8. 验证 ====="
curl -s http://127.0.0.1:8001/health && echo "" || echo "健康检查未通过，查日志：docker compose logs"

echo ""
echo "===== 完成 ====="
echo "管理后台: http://<服务器IP>:8001/admin/ui/  (HTTP，Nginx+HTTPS 需另配)"
echo "MCP地址:  http://<服务器IP>:8001/mcp         (workbuddy 用 Bearer Token 连)"
echo "注意: 首次需在容器内跑 tenant_init 接入第一个租户"
echo "  docker compose exec wbsysc python -m app.tenant_init --tenant-id tenant1 \\"
echo "    --corpid wwXXX --secret XXX --token <随机串> --contact-secret XXXX \\"
echo "    --modules report,approval,checkin --display 测试客户1"