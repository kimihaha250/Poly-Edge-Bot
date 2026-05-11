#!/usr/bin/env bash
# ============================================================
# 本地 → 腾讯云服务器同步脚本
# 用法：bash deploy/upload.sh <服务器IP> [用户名] [端口]
# 示例：bash deploy/upload.sh 43.xx.xx.xx ubuntu 22
# ============================================================

SERVER_IP="${1:?请传入服务器 IP，例如: bash deploy/upload.sh 43.xx.xx.xx}"
SERVER_USER="${2:-ubuntu}"
SERVER_PORT="${3:-22}"
REMOTE_DIR="/home/${SERVER_USER}/poly-edga"

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "上传到 ${SERVER_USER}@${SERVER_IP}:${REMOTE_DIR} ..."

rsync -avz --progress \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.pycache/' \
  --exclude='positions.json' \
  --exclude='trader_log.md' \
  --exclude='logs/' \
  -e "ssh -p ${SERVER_PORT}" \
  "${SCRIPT_DIR}/" \
  "${SERVER_USER}@${SERVER_IP}:${REMOTE_DIR}/"

echo ""
echo "✅ 上传完成！"
echo ""
echo "接下来在服务器上执行："
echo "  ssh -p ${SERVER_PORT} ${SERVER_USER}@${SERVER_IP}"
echo "  cd ${REMOTE_DIR}"
echo "  bash deploy/install.sh"
echo "  nano .env                          # 填入 API Key"
echo "  sudo systemctl start poly-edga    # 启动 bot"
