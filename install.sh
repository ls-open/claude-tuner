#!/usr/bin/env bash
#
# install.sh —— macOS / Linux 的一行安装入口（curl | bash 那条路）
#
# 它只做一件事：找到这台机器上的 Python，把活交给 install.py。
# 安装流程只有**一份实现**（claude-tuner/install.py，纯标准库、跨平台），
# 这里再写一套必然会漂 —— 两边各自修 bug、各自漏改，是这个仓库反复踩过的坑
# （见 tool_rules.py / volatile_markers.py 的由来）。
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/ls-open/claude-tuner/main/install.sh | bash
#   bash install.sh --dry-run
#
set -eu

REPO_SLUG="${CLAUDE_TUNER_REPO:-ls-open/claude-tuner}"
REF="${CLAUDE_TUNER_REF:-main}"

die() { printf '\n❌ %s\n' "$*" >&2; exit 1; }

# ── 找 Python：3.11 是硬要求（install.py 自己会再核一次版本） ──
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PY="$candidate"
      break
    fi
  fi
done
[ -n "$PY" ] || die "需要 Python 3.11 或更新版本。macOS 可用 'xcode-select --install' 或到 python.org 安装。"

# ── 就地模式：脚本旁边就有源码，直接跑 ──
here="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")"
if [ -n "$here" ] && [ -f "$here/install.py" ]; then
  exec "$PY" "$here/install.py" "$@"
fi

# ── 远程模式：只把 install.py 落到临时目录再交给它（它自己会去下载插件包） ──
tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/claude-tuner-shim.XXXXXX")"
trap 'rm -rf "$tmpdir"' EXIT INT TERM

printf '正在下载安装程序…\n'
for url in \
  "https://raw.githubusercontent.com/${REPO_SLUG}/${REF}/install.py" \
  "https://gh-proxy.com/https://raw.githubusercontent.com/${REPO_SLUG}/${REF}/install.py" \
  "https://ghfast.top/https://raw.githubusercontent.com/${REPO_SLUG}/${REF}/install.py"
do
  if "$PY" - "$url" "$tmpdir/install.py" <<'PYEOF' 2>/dev/null
import sys, urllib.request
url, dest = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url, headers={"User-Agent": "claude-tuner-installer/1.0"})
with urllib.request.urlopen(req, timeout=15) as resp, open(dest, "wb") as fh:
    fh.write(resp.read())
PYEOF
  then
    exec "$PY" "$tmpdir/install.py" "$@"
  fi
  printf '  ⚠️ 通道不可用，换下一条…\n'
done

die "三条下载通道都不可达。请手动下载仓库 Zip，解压后运行：python install.py"
