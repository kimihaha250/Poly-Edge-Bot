#!/usr/bin/env bash
# ============================================================
# Polymarket Bot 服务器一键部署脚本
# 适用：Ubuntu 20.04 / 22.04 / 24.04 (腾讯云新加坡)
# 用法：bash deploy/install.sh
# ============================================================
set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_MIN="3.10"
SERVICE_NAME="poly-edga"
SERVICE_USER="$(whoami)"

echo "========================================="
echo "  Polymarket Bot 部署脚本"
echo "  目录: $REPO_DIR"
echo "========================================="

# ── 1. 系统依赖 ───────────────────────────────────────────
echo ""
echo "[1/5] 安装系统依赖..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv curl git screen 2>/dev/null

# ── 2. 检查 Python 版本 ───────────────────────────────────
PYTHON=$(which python3)
PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "    Python 版本: $PY_VER"

# ── 3. 创建虚拟环境 & 安装依赖 ────────────────────────────
echo ""
echo "[2/5] 创建虚拟环境并安装依赖..."
cd "$REPO_DIR"

if [ ! -d ".venv" ]; then
    $PYTHON -m venv .venv
fi

# 新加坡服务器可直连 PyPI，不需要镜像；如果慢再加 -i 参数
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
echo "    依赖安装完成"

# ── 4. 检查 .env 文件 ─────────────────────────────────────
echo ""
echo "[3/5] 检查配置文件..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "    ⚠️  已从 .env.example 创建 .env，请填写必要的 API Key"
    echo "    编辑命令: nano .env"
else
    echo "    .env 已存在 ✓"
fi

# ── 5. 安装 systemd 服务（可选）──────────────────────────
echo ""
echo "[4/5] 配置 systemd 后台服务..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=Polymarket Probability Arbitrage Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/.venv/bin/python3 main.py
Restart=on-failure
RestartSec=30
StandardOutput=append:${REPO_DIR}/logs/bot.log
StandardError=append:${REPO_DIR}/logs/bot.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "${REPO_DIR}/logs"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
echo "    systemd 服务已注册: ${SERVICE_NAME}"

# ── 6. 打印操作指南 ───────────────────────────────────────
echo ""
echo "[5/5] 完成！"
echo ""
echo "========================================="
echo "  常用命令"
echo "========================================="
echo ""
echo "  启动 bot:"
echo "    sudo systemctl start ${SERVICE_NAME}"
echo ""
echo "  查看实时日志:"
echo "    tail -f ${REPO_DIR}/logs/bot.log"
echo ""
echo "  停止 bot:"
echo "    sudo systemctl stop ${SERVICE_NAME}"
echo ""
echo "  查看运行状态:"
echo "    sudo systemctl status ${SERVICE_NAME}"
echo ""
echo "  先用 screen 测试（不挂后台）:"
echo "    screen -S bot"
echo "    source .venv/bin/activate && python3 main.py"
echo "    # Ctrl+A D 退出 screen，bot 继续运行"
echo "    screen -r bot  # 重新连接"
echo ""
echo "  ⚠️  记得先编辑 .env 填入 API Key："
echo "    nano ${REPO_DIR}/.env"
echo "========================================="
