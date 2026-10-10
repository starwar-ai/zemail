#!/usr/bin/env bash
# zemail 远程一键部署：把本机当前代码部署到 172.16.52.11:8020
#
# 用法（在本机项目根目录执行，Git Bash / Linux / macOS 均可，无需 rsync）：
#   ./deploy-remote.sh                # 首次部署 + 重复更新部署
#   ./deploy-remote.sh --push-env     # 强制用本机 .env 覆盖服务器上的 .env
#
# 做了什么：
#   1. tar 打包当前代码（排除 .git/venv/缓存/日志/运行数据），经 SSH 上传到服务器
#   2. 在服务器上执行 deploy.sh：装依赖、建 venv、注册 systemd 服务 zemail
#   3. 首次部署时若本机有 .env 则自动上传（仅当服务器还没有 .env），随后启动服务
#   4. 健康检查 http://<服务器>:<端口>/
# 服务器上的运行数据（data/ 目录）与已有 .env 永远不会被本机内容覆盖。
#
# 可选参数：
#   --server <ip>        服务器地址（默认 172.16.52.11）
#   --port <port>        监听端口（默认 8020）
#   --ssh-user <name>    SSH 登录用户（默认 root；非 root 需可 sudo）
#   --app-dir <path>     服务器部署目录（默认 /opt/zemail）
#   --service-user <n>   服务运行用户（默认 zemail，不存在则自动创建）
#   --push-env           用本机 .env 覆盖服务器 .env（覆盖后自动重启）
#   --no-env             首次部署也不上传本机 .env（自行上服务器编辑）
#   --skip-apt           传给 deploy.sh：跳过 apt 系统依赖安装（离线环境用）
set -euo pipefail

SERVER="172.16.52.11"
APP_PORT="8020"
SSH_USER="root"
APP_DIR="/opt/zemail"
SERVICE_USER="zemail"
PUSH_ENV=0
NO_ENV=0
SKIP_APT_FLAG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --server)       SERVER="$2"; shift 2 ;;
    --port)         APP_PORT="$2"; shift 2 ;;
    --ssh-user)     SSH_USER="$2"; shift 2 ;;
    --app-dir)      APP_DIR="$2"; shift 2 ;;
    --service-user) SERVICE_USER="$2"; shift 2 ;;
    --push-env)     PUSH_ENV=1; shift ;;
    --no-env)       NO_ENV=1; shift ;;
    --skip-apt)     SKIP_APT_FLAG="--skip-apt"; shift ;;
    -h|--help)      sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "未知参数: $1（--help 查看用法）" >&2; exit 1 ;;
  esac
done

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_DEST="$SSH_USER@$SERVER"
STAGE_DIR="/tmp/zemail-deploy-src"
SERVICE_NAME="zemail"

echo "==> 目标: http://$SERVER:$APP_PORT  ($SSH_DEST:$APP_DIR)"

# ---------- 1. 连通性 / sudo 检测 ----------
echo "==> 检查 SSH 连接..."
if [ "$(ssh -o ConnectTimeout=8 "$SSH_DEST" 'id -u')" = "0" ]; then
  SUDO=""
else
  SUDO="sudo"
  echo "    SSH 用户非 root，远程命令将使用 sudo（需免密 sudo 或允许交互输入密码）"
fi

# ---------- 2. 上传代码 ----------
echo "==> 上传代码到 $SSH_DEST:$STAGE_DIR ..."
tar -C "$SRC_DIR" -czf - \
    --exclude=./.git --exclude=./.venv --exclude=./venv \
    --exclude=./data --exclude=./.env \
    --exclude=./.agents --exclude=./.claude --exclude=./.zcode \
    --exclude=./wecom-auth-qr.png \
    --exclude=__pycache__ --exclude='*.pyc' --exclude='*.log' \
    . | ssh "$SSH_DEST" "rm -rf '$STAGE_DIR' && mkdir -p '$STAGE_DIR' && tar -xzf - -C '$STAGE_DIR'"
# 保险处理：万一 .sh 文件带了 Windows 行尾（CRLF），服务器上的 bash 会无法执行
ssh "$SSH_DEST" "find '$STAGE_DIR' -name '*.sh' -exec sed -i 's/\r\$//' {} +"

# ---------- 3. 服务运行用户 ----------
if [ "$SERVICE_USER" != "root" ]; then
  echo "==> 确保服务用户 $SERVICE_USER 存在..."
  ssh "$SSH_DEST" "id -u '$SERVICE_USER' >/dev/null 2>&1 || $SUDO useradd --system --home-dir '$APP_DIR' --create-home --shell /usr/sbin/nologin '$SERVICE_USER'"
fi

# ---------- 4. 首次部署标记（以服务器上是否已有 .env 判断） ----------
FIRST_DEPLOY=0
ssh "$SSH_DEST" "test -f '$APP_DIR/.env'" || FIRST_DEPLOY=1

# ---------- 5. 服务器端执行 deploy.sh ----------
echo "==> 在服务器上执行 deploy.sh（端口 $APP_PORT）..."
ssh "$SSH_DEST" "cd '$STAGE_DIR' && $SUDO bash deploy.sh --dir '$APP_DIR' --port '$APP_PORT' --user '$SERVICE_USER' $SKIP_APT_FLAG"

# ---------- 6. .env 处理与启动 ----------
NEED_START=0
if [ "$FIRST_DEPLOY" = "1" ]; then
  NEED_START=1   # 首次部署 deploy.sh 只生成模板不启动服务，由本脚本启动
  if [ "$NO_ENV" != "1" ] && [ -f "$SRC_DIR/.env" ]; then
    echo "==> 服务器无 .env，上传本机配置..."
    scp -q "$SRC_DIR/.env" "$SSH_DEST:/tmp/zemail.env"
    ssh "$SSH_DEST" "$SUDO mv /tmp/zemail.env '$APP_DIR/.env' && $SUDO chown '$SERVICE_USER':'$SERVICE_USER' '$APP_DIR/.env' && $SUDO chmod 600 '$APP_DIR/.env'"
  else
    NEED_START=0
    echo ""
    echo "=============================================="
    echo " 首次部署且未上传 .env：请先上服务器编辑配置"
    echo "   $SUDO nano $APP_DIR/.env"
    echo " 然后启动："
    echo "   $SUDO systemctl start $SERVICE_NAME"
    echo "=============================================="
  fi
elif [ "$PUSH_ENV" = "1" ] && [ -f "$SRC_DIR/.env" ]; then
  echo "==> --push-env：用本机 .env 覆盖服务器配置并重启..."
  scp -q "$SRC_DIR/.env" "$SSH_DEST:/tmp/zemail.env"
  ssh "$SSH_DEST" "$SUDO mv /tmp/zemail.env '$APP_DIR/.env' && $SUDO chown '$SERVICE_USER':'$SERVICE_USER' '$APP_DIR/.env' && $SUDO chmod 600 '$APP_DIR/.env' && $SUDO systemctl restart $SERVICE_NAME"
fi

if [ "$NEED_START" = "1" ]; then
  echo "==> 启动服务..."
  ssh "$SSH_DEST" "$SUDO systemctl start $SERVICE_NAME"
fi

# ---------- 7. 清理暂存目录 + 健康检查 ----------
ssh "$SSH_DEST" "rm -rf '$STAGE_DIR'" || true

echo "==> 健康检查 http://$SERVER:$APP_PORT/ ..."
CODE=""
for _ in $(seq 1 15); do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 "http://$SERVER:$APP_PORT/" || true)"
  [ "$CODE" = "200" ] && break
  sleep 2
done

echo ""
if [ "$CODE" = "200" ]; then
  echo "部署成功: http://$SERVER:$APP_PORT"
  echo "默认账号 admin / admin123，登录后请立即修改密码"
  echo "查看日志: ssh $SSH_DEST \"$SUDO journalctl -u $SERVICE_NAME -f\""
else
  echo "健康检查未通过（HTTP $CODE），请排查："
  echo "  ssh $SSH_DEST \"$SUDO systemctl status $SERVICE_NAME --no-pager\""
  echo "  ssh $SSH_DEST \"$SUDO journalctl -u $SERVICE_NAME -n 50 --no-pager\""
  exit 1
fi
