#!/usr/bin/env bash
# =============================================================================
# torrent-tool 一键部署 / 更新脚本
#
#   首次安装：  curl -fsSL https://raw.githubusercontent.com/mi1314cat/Torrents/main/deploy.sh | sudo bash
#   已安装更新：sudo bash deploy.sh            （幂等：拉代码 → 装依赖 → 重启）
#   自定义参数：sudo bash deploy.sh --dir /opt/torrent-tool --port 10011
#
# 做的事：拉取代码 → 建 venv 装依赖 → 渲染 systemd 单元 → 启动 → 健康检查
# 不做：改 nginx、改防火墙、生成证书（这些请自行决定）
# =============================================================================
set -euo pipefail

REPO_URL="https://github.com/mi1314cat/Torrents.git"
BRANCH="main"
INSTALL_DIR="/opt/torrent-tool"
PORT="10011"
SERVICE_NAME="torrent-tool"

# ---------- 参数解析 ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)   INSTALL_DIR="$2"; shift 2 ;;
    --port)  PORT="$2"; shift 2 ;;
    --repo)  REPO_URL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1（用 --help 查看用法）"; exit 1 ;;
  esac
done

# ---------- 输出helper ----------
say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 0. 前置检查 ----------
[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash deploy.sh"
command -v git >/dev/null || die "缺少 git，请先 apt install git"

# 选一个 ≥3.11 的 python
PYBIN=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    v=$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)
    if [[ "$(printf '%s\n3.11\n' "$v" | sort -V | head -1)" == "3.11" ]]; then
      PYBIN="$cand"; break
    fi
  fi
done
[[ -n "$PYBIN" ]] || die "需要 Python ≥ 3.11（推荐 3.13），当前未找到。
  Debian/Ubuntu 安装：apt install python3.13 python3.13-venv   # 或 python3.11 python3.11-venv"
say "使用 Python: $PYBIN ($($PYBIN -V 2>&1 | cut -d' ' -f2))"

# ---------- 1. 系统依赖 ----------
MISSING=()
command -v "$PYBIN" >/dev/null || MISSING+=("$PYBIN")
"$PYBIN" -c 'import venv' 2>/dev/null || MISSING+=("${PYBIN}-venv")
command -v curl >/dev/null || MISSING+=(curl)
if [[ ${#MISSING[@]} -gt 0 ]]; then
  say "安装系统依赖: ${MISSING[*]}"
  if command -v apt-get >/dev/null; then
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl git "${MISSING[@]}" >/dev/null
  elif command -v dnf >/dev/null; then
    dnf install -y -q ca-certificates curl git "${MISSING[@]}" >/dev/null
  elif command -v yum >/dev/null; then
    yum install -y -q ca-certificates curl git "${MISSING[@]}" >/dev/null
  elif command -v apk >/dev/null; then
    apk add --no-cache ca-certificates curl git "${MISSING[@]}" >/dev/null
  else
    die "未识别的包管理器，请手动安装: ${MISSING[*]}"
  fi
fi

# ---------- 2. 拉取 / 更新代码 ----------
if [[ -d "$INSTALL_DIR/.git" ]]; then
  say "更新已有安装: $INSTALL_DIR"
  cd "$INSTALL_DIR"
  git fetch --depth 1 origin "$BRANCH"
  git checkout "$BRANCH"
  git reset --hard "origin/$BRANCH"   # 丢弃本地改动，保持与仓库一致
  # torrent.db / venv 已在 .gitignore 中，不会被 reset 影响
  say "已更新到 origin/$BRANCH ($(git rev-parse --short HEAD))"
else
  say "全新安装到 $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  cd "$INSTALL_DIR"
  # 浅克隆后只保留一份，减少磁盘占用
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" . 2>/dev/null \
    || die "克隆失败：$REPO_URL（分支 $BRANCH）。私有仓库请改用：
  git clone https://<token>@github.com/mi1314cat/Torrents.git $INSTALL_DIR"
fi

[[ -f server.py ]] || die "目录里没有 server.py，确认拉到了正确的仓库/分支"

# ---------- 3. venv + 依赖 ----------
if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
  say "创建虚拟环境 venv/"
  "$PYBIN" -m venv "$INSTALL_DIR/venv"
fi
say "安装依赖（首次较慢，约 1-3 分钟）…"
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
say "依赖版本："
"$INSTALL_DIR/venv/bin/pip" list --format=freeze 2>/dev/null \
  | grep -iE '^(fastapi|fastmcp|libtorrent|pydantic|starlette|uvicorn)=' | sed 's/^/    /'

# ---------- 4. systemd ----------
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
say "写入 systemd 单元: $UNIT"
sed -e "s#__INSTALL_DIR__#${INSTALL_DIR}#g" -e "s#__PORT__#${PORT}#g" \
    "$INSTALL_DIR/deploy/${SERVICE_NAME}.service" > "$UNIT"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl restart "$SERVICE_NAME"

# ---------- 5. 健康检查 ----------
say "等待服务就绪…"
READY=0
for i in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/" 2>/dev/null; then READY=1; break; fi
  sleep 1
done
if [[ $READY -ne 1 ]]; then
  warn "服务未在 30 秒内就绪，最近日志："
  journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
  die "部署失败"
fi

# 端口是否真的只监听本机
LISTEN=$(ss -tln 2>/dev/null | grep ":${PORT} " || true)
if echo "$LISTEN" | grep -qE '0\.0\.0\.0:|\*:'; then
  warn "端口 ${PORT} 监听在所有网卡上！检查 TT_LISTEN 是否被改成 0.0.0.0"
else
  say "监听正常：仅本机 127.0.0.1:${PORT}"
fi

# ---------- 6. 结果 ----------
MCP_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${PORT}/mcp" || true)
[[ "$MCP_CODE" == "401" ]] \
  && say "MCP 端点已挂载且鉴权生效（无 token → 401）" \
  || warn "MCP 端点返回 ${MCP_CODE}，预期 401（检查 /mcp 是否被反代吞掉）"

# 首次安装时抓出随机生成的 admin 密码
NEWADMIN=$(journalctl -u "$SERVICE_NAME" --since "-2min" --no-pager 2>/dev/null \
           | grep -oE 'password=[^ ]+' | tail -1 || true)

cat <<EOF

$(printf '\033[1;32m')====================================================
 部署完成
====================================================$(printf '\033[0m')
  目录     : $INSTALL_DIR
  本机端口 : $PORT（仅监听 127.0.0.1，不对公网开放）
  状态     : systemctl status $SERVICE_NAME
  日志     : journalctl -u $SERVICE_NAME -f
${NEWADMIN:+
  首次登录 : 用户名 admin，密码 ${NEWADMIN#password=}   ← 只显示这一次，请立刻保存
}  下一步：
  1) 配置反代（公网访问需 nginx/caddy 转发到 127.0.0.1:${PORT}）
     参考：$INSTALL_DIR/deploy/nginx-torrent-tool.conf
  2) 浏览器打开反代地址，用 admin 登录 → 后台「数据源」/「MCP」页签
  3) 在「MCP」页签创建 Token，客户端这样连：
       Endpoint : https://你的域名/mcp
       Auth     : Bearer <你创建的 token>
  4) 更新本服务：sudo bash $INSTALL_DIR/deploy.sh

EOF
