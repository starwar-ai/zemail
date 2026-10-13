#!/usr/bin/env bash
# zemail 自动部署脚本（Ubuntu 22.04）
#
# 两种用法：
#   1. 项目已上传到服务器（git clone 或 scp），在项目根目录执行：
#        sudo ./deploy.sh
#   2. 让脚本直接从 git 仓库拉取：
#        sudo ./deploy.sh --repo https://github.com/you/zemail.git
#
# 重复执行即「更新部署」：拉取/同步最新代码、更新依赖并重启服务。
#
# 可选参数：
#   --repo <git-url>    从 git 仓库部署（默认：使用当前目录的代码）
#   --branch <name>     git 分支（默认 main）
#   --dir <path>        部署目录（默认 /opt/zemail）
#   --port <port>       监听端口（默认 8000）
#   --user <name>       服务运行用户（默认当前 sudo 调用者）
#   --skip-apt          跳过 apt 系统依赖安装（离线环境用）
set -euo pipefail

# ---------- 参数解析 ----------
GIT_REPO=""
GIT_BRANCH="main"
APP_DIR="/opt/zemail"
APP_PORT="8000"
APP_USER=""
SKIP_APT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)     GIT_REPO="$2"; shift 2 ;;
    --branch)   GIT_BRANCH="$2"; shift 2 ;;
    --dir)      APP_DIR="$2"; shift 2 ;;
    --port)     APP_PORT="$2"; shift 2 ;;
    --user)     APP_USER="$2"; shift 2 ;;
    --skip-apt) SKIP_APT=1; shift ;;
    -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 sudo 运行：sudo $0" >&2
  exit 1
fi

# 默认以 sudo 调用者身份运行服务，避免日志/缓存目录归 root 所有
if [ -z "$APP_USER" ]; then
  APP_USER="${SUDO_USER:-root}"
fi

SERVICE_NAME="zemail"
VENV_DIR="$APP_DIR/.venv"

echo "==> 部署目录: $APP_DIR"
echo "==> 运行用户: $APP_USER"
echo "==> 监听端口: $APP_PORT"

# ---------- 1. 系统依赖 ----------
if [ "$SKIP_APT" -eq 0 ]; then
  echo "==> 安装系统依赖..."
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip git
fi

# ---------- 2. 同步代码 ----------
if [ -n "$GIT_REPO" ]; then
  echo "==> 从 $GIT_REPO ($GIT_BRANCH) 拉取代码..."
  if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch origin "$GIT_BRANCH"
    git -C "$APP_DIR" checkout "$GIT_BRANCH"
    git -C "$APP_DIR" reset --hard "origin/$GIT_BRANCH"
  else
    rm -rf "$APP_DIR"
    git clone --depth 1 --branch "$GIT_BRANCH" "$GIT_REPO" "$APP_DIR"
  fi
else
  # 从当前目录同步（.env 与 data/ 运行数据永不覆盖服务器端）
  SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ "$SRC_DIR" != "$APP_DIR" ]; then
    echo "==> 从 $SRC_DIR 同步代码到 $APP_DIR..."
    mkdir -p "$APP_DIR"
    if command -v rsync >/dev/null 2>&1; then
      rsync -a --delete \
        --exclude '.git' --exclude '__pycache__' --exclude '.venv' \
        --exclude 'data' --exclude '.env' \
        "$SRC_DIR/" "$APP_DIR/"
    else
      mkdir -p "$APP_DIR"
      tar -C "$SRC_DIR" -c \
        --exclude=./.git --exclude=./.venv --exclude=./data --exclude=./.env \
        . | tar -C "$APP_DIR" -x
      find "$APP_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    fi
  fi
fi

# ---------- 3. 配置文件 ----------
if [ ! -f "$APP_DIR/.env" ]; then
  echo "==> 生成 .env 模板（请编辑后再启动服务）"
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  ENV_FRESH=1
else
  ENV_FRESH=0
fi

# ---------- 4. Python 虚拟环境 ----------
echo "==> 创建/更新虚拟环境..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$APP_DIR/requirements.txt" waitress

# ---------- 5. systemd 服务 ----------
echo "==> 写入 systemd 服务 $SERVICE_NAME ..."
cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=zemail 邮件分析助手
After=network.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PORT=$APP_PORT
EnvironmentFile=-$APP_DIR/.env
ExecStart=$VENV_DIR/bin/waitress-serve --listen=0.0.0.0:$APP_PORT --max-request-body-size=21474836480 --call web:create_app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# 目录归属（缓存、日志需要可写）
mkdir -p "$APP_DIR/data/emails"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" --quiet

if [ "$ENV_FRESH" -eq 1 ]; then
  echo ""
  echo "=============================================="
  echo " 首次部署：请先编辑配置再启动服务"
  echo "   nano $APP_DIR/.env"
  echo " 然后执行："
  echo "   systemctl start $SERVICE_NAME"
  echo "=============================================="
else
  echo "==> 重启服务..."
  systemctl restart "$SERVICE_NAME"
  sleep 2
  systemctl --no-pager --full status "$SERVICE_NAME" | head -n 12 || true
  echo ""
  echo "部署完成，服务地址: http://<服务器IP>:$APP_PORT"
  echo "常用命令："
  echo "  查看日志:  journalctl -u $SERVICE_NAME -f"
  echo "  重启服务:  systemctl restart $SERVICE_NAME"
fi
