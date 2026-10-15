#!/usr/bin/env bash
#
# claude-tuner 一键安装（第一版只支持 macOS）
#
#   curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/main/install.sh | bash
#
# 它做四件事：把插件放到 ~/.claude/skills/claude-tuner/、写好让客户端进 3P 的状态、
# 拉起本地网关、打开配置页。**真正改文件的判断都在 scripts/bootstrap.py 里**，
# 这个脚本只做调度与前置检查 —— 免得同一件事有两处实现、各说各话。
#
#   bash install.sh --dry-run     # 只打印将做什么，一个文件都不写、也不出网
#   bash install.sh --uninstall   # 收回：停网关、撤客户端设置、删插件目录
#
set -euo pipefail

# ↓ 发布时改这两行（公开仓库的 owner/repo；镜像是给国内直连用的，可以留空）
REPO_SLUG="${CLAUDE_TUNER_REPO:-ls-open/claude-tuner}"
MIRROR_REPO="${CLAUDE_TUNER_MIRROR:-}"
REF="${CLAUDE_TUNER_REF:-main}"
PLUGIN_NAME="claude-tuner"
DEST="${CLAUDE_TUNER_DIR:-$HOME/.claude/skills/$PLUGIN_NAME}"
BRIDGE_HOME="${CLAUDE_TUNER_HOME:-$HOME/.claude-tuner}"

DRY_RUN=0 FORCE=0 OPEN_PAGE=1 WITH_CLI=1 UNINSTALL=0
API_KEY="${CLAUDE_TUNER_API_KEY:-}" UPSTREAM="" PRESET="" MODEL=""

log()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }
die()  { printf '\n❌ %s\n' "$*" >&2; exit 1; }
act()  { if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

usage() {
  cat <<'USAGE'
用法：bash install.sh [选项]

  --key <KEY>        API Key（也可用环境变量 CLAUDE_TUNER_API_KEY；都不给则稍后在配置页填）
  --upstream <URL>   上游地址，默认 DeepSeek 官方
  --preset <ID>      上游预设：deepseek / opencode / custom
  --model <NAME>     上游模型名
  --no-cli           不写 ~/.claude/settings.json（终端 Claude Code 不走本网关）
  --no-open          装完不自动打开配置页
  --dry-run          只打印计划，不写任何文件
  --force            重写已经装过的配置
  --uninstall        卸载
  -h, --help         看这段
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --key) API_KEY="${2:-}"; shift 2 ;;
    --upstream) UPSTREAM="${2:-}"; shift 2 ;;
    --preset) PRESET="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --no-cli) WITH_CLI=0; shift ;;
    --no-open) OPEN_PAGE=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "不认识的参数：$1（-h 看用法）" ;;
  esac
done

PY="$(command -v python3 || true)"

# ── 前置检查 ───────────────────────────────────────────────
step "① 检查环境"
[ "$(uname -s)" = "Darwin" ] || die "这一版只支持 macOS。Windows 支持还在做。"
[ -n "$PY" ] || die "没找到 python3。macOS 可以装 Xcode 命令行工具：xcode-select --install"
"$PY" - <<'PYEOF' || die "python3 版本太低，需要 3.11 或更高"
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PYEOF
ok "python3 $("$PY" -V 2>&1 | cut -d' ' -f2)"

if [ -d "/Applications/Claude.app" ]; then
  ok "找到 Claude 桌面端"
  # 刚下载、从没启动过时：应用支持目录还不存在。我们**能**把这棵树建出来（实测过），
  # 但客户端首次启动也会自己建一遍 —— 谁先谁后、它建的时候是合并还是覆盖，我们没实测过。
  # 与其赌，不如让用户先开一次：反正下载来的 app 有 quarantine，本来也得手动开一次。
  if [ ! -d "$HOME/Library/Application Support/Claude" ] \
     && [ ! -d "$HOME/Library/Application Support/Claude-3p" ]; then
    warn "看起来 Claude 还没启动过（应用支持目录还不存在）。"
    warn "建议先打开一次 Claude、进到界面再退出，然后重跑本脚本 ——"
    warn "客户端首次启动会自己建一遍配置，让它先建完，我们再往上改，顺序反了就说不清谁盖谁。"
  fi
else
  warn "没找到 /Applications/Claude.app —— 装完只有命令行 Claude Code 能用，桌面端那部分等你装上再说"
  warn "还没装的话，到官网下载： https://claude.com/product/claude-code"
fi

# 客户端在跑时**不能装**：安装要写它的配置文件（deploymentMode / 3P 配置），运行中的客户端会把
# 改动回写覆盖 —— 结果是「装完了却没生效」。这检查必须放在动手之前，不能放最后（那时候文件已经写了）。
if [ "$DRY_RUN" != 1 ] && pgrep -f "Claude.app/Contents/MacOS/Claude" >/dev/null 2>&1; then
  warn "检测到 Claude 正在运行 —— 安装要写它的配置文件，运行中的客户端会把改动覆盖回去。"
  warn "请先在菜单里点「退出 Claude」（不是关窗口），然后重跑本脚本。"
  die "已中止，未写入任何文件。"
fi

# ── 卸载 ───────────────────────────────────────────────────
if [ "$UNINSTALL" = 1 ]; then
  step "卸载"
  if [ -f "$DEST/scripts/bootstrap.py" ]; then
    act "$PY" "$DEST/scripts/bootstrap.py" uninstall
  fi
  if [ -d "$DEST" ]; then
    act rm -rf "$DEST"
    ok "已删除插件目录 $DEST"
  fi
  log ""
  log "3P 推理配置没有动（那是客户端的资产）。要一起清掉："
  log "  $PY $DEST/scripts/bootstrap.py uninstall --purge-3p   # 需先重装再执行"
  log "改动前的备份都在 $BRIDGE_HOME/backups/3p/"
  exit 0
fi

# ── 取源码 ─────────────────────────────────────────────────
step "② 准备插件文件"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
TMP_DIR=""
SRC=""
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/scripts/gateway.py" ]; then
  SRC="$SELF_DIR"
  ok "就地安装（源码就在 ${SRC}，全程不出网）"
else
  command -v curl >/dev/null || die "没找到 curl，无法下载插件"
  log "  从公开仓库下载：$REPO_SLUG@$REF"
  # 国内直连 github 常不通，按这几条通道依次试；全不通就让用户走 zip 那条路。
  # 镜像（Gitee 等）排在最前 —— 国内直连它是这几条里最快的。
  CANDIDATES=""
  [ -n "$MIRROR_REPO" ] && CANDIDATES="https://gitee.com/$MIRROR_REPO/repository/archive/$REF.tar.gz"
  CANDIDATES="$CANDIDATES
https://codeload.github.com/$REPO_SLUG/tar.gz/refs/heads/$REF
https://gh-proxy.com/https://github.com/$REPO_SLUG/archive/refs/heads/$REF.tar.gz
https://ghfast.top/https://github.com/$REPO_SLUG/archive/refs/heads/$REF.tar.gz"
  for base in $CANDIDATES; do
    if [ "$DRY_RUN" = 1 ]; then
      # 演练模式什么都别下，但要把整条回退链列出来给人看
      log "  [dry-run] 会依次尝试：$base"
      continue
    fi
    TMP_DIR="$(mktemp -d)"
    if curl -fsSL --connect-timeout 10 --max-time 180 -o "$TMP_DIR/pkg.tar.gz" "$base" 2>/dev/null; then
      tar -xzf "$TMP_DIR/pkg.tar.gz" -C "$TMP_DIR"
      SRC="$(find "$TMP_DIR" -maxdepth 1 -type d -name '*claude-tuner*' -o -maxdepth 1 -type d -name '*claude-tuner*' | head -1)"
      [ -n "$SRC" ] || SRC="$(find "$TMP_DIR" -maxdepth 1 -mindepth 1 -type d | head -1)"
      ok "已下载（$base 这条通道）"
      break
    fi
    rm -rf "$TMP_DIR"; TMP_DIR=""
    warn "这条通道不通：$(printf '%s' "$base" | cut -c1-56)…"
  done
  if [ "$DRY_RUN" = 1 ]; then
    SRC="$DEST"   # 演练模式没真下载，后面的步骤只打印计划，不碰这个路径
  fi
  [ "$DRY_RUN" = 1 ] || [ -f "${SRC:-/nonexistent}/scripts/gateway.py" ] || die "所有下载通道都不通。
   请改用：下载仓库 zip 解压后，在解压出来的目录里跑 bash install.sh
   或者：把这段话粘给任意 AI Agent，让它帮你装。"
fi
[ "$DRY_RUN" = 1 ] || trap '[ -n "$TMP_DIR" ] && rm -rf "$TMP_DIR"' EXIT

# ── 放到位 ─────────────────────────────────────────────────
step "③ 安装到 $DEST"
SAME_DIR=0
if [ -d "${SRC:-}" ] && [ -d "$DEST" ]; then
  # 两个目录都存在才比对（演练模式下源码可能压根没落盘）
  [ "$(cd "$SRC" && pwd)" = "$(cd "$DEST" && pwd)" ] && SAME_DIR=1
fi
if [ "$SAME_DIR" = 1 ]; then
  ok "已经在目标位置，跳过复制"
elif [ ! -d "${SRC:-}" ]; then
  log "  [dry-run] 会把下载下来的源码复制到 $DEST"
else
  # 目标位已有东西且不是本插件 -> 先备份，绝不直接覆盖别人的插件
  if [ -d "$DEST" ] && [ ! -f "$DEST/.claude-plugin/plugin.json" ]; then
    STAMP="$(date +%Y%m%dT%H%M%S)"
    warn "$DEST 已存在且不是本插件，先备份到 $BRIDGE_HOME/backups/skills-$STAMP"
    act mkdir -p "$BRIDGE_HOME/backups"
    act mv "$DEST" "$BRIDGE_HOME/backups/skills-$STAMP"
  fi
  act mkdir -p "$DEST"
  if command -v rsync >/dev/null; then
    act rsync -a --delete --exclude=__pycache__ --exclude=.git --exclude=.DS_Store \
        "$SRC/" "$DEST/"
  else
    act rm -rf "$DEST"; act mkdir -p "$DEST"; act cp -R "$SRC/." "$DEST/"
  fi
  ok "插件文件已就位"
fi

# 演练模式下文件还没真的复制过去，脚本要从源码那边调
if [ "$DRY_RUN" = 1 ]; then SCRIPTS="$SRC/scripts"; else SCRIPTS="$DEST/scripts"; fi

# 同名插件不能共存：装过 marketplace 版的话，skills 这份会被客户端拒载
if compgen -G "$HOME/.claude/plugins/cache/*/$PLUGIN_NAME" >/dev/null 2>&1; then
  warn "检测到之前用插件市场装过同名插件。两者不能共存，请先执行："
  warn "    claude plugin uninstall $PLUGIN_NAME"
fi

# ── 问 Key ─────────────────────────────────────────────────
if [ -z "$API_KEY" ] && [ "$DRY_RUN" = 0 ] && [ -r /dev/tty ]; then
  # 这一问是新用户唯一要做的事，必须能照着做：说清「哪一家」「去哪申请」
  # 「长什么样」「能不能跳」。原来只写「请输入上游 API Key」——
  # 新用户不知道「上游」是什么，也不知道该去谁家申请。
  printf '\n'
  printf '本插件默认接 DeepSeek 官方，模型会自动配好，你只需要一个 API Key。\n'
  printf '\n'
  printf '  没有的话：打开 https://platform.deepseek.com 注册，在「API Keys」里建一个\n'
  printf '\n'
  printf '粘贴在这里（形如 sk-xxxxxx，输入时不显示），直接回车可跳过、装完在网页里填也行：\n'
  read -r -s API_KEY </dev/tty || API_KEY=""
  printf '\n'
fi

# ── 写配置 ─────────────────────────────────────────────────
step "④ 写好客户端状态"
BOOT_ARGS=(install --no-restart)
[ "$DRY_RUN" = 1 ] && BOOT_ARGS+=(--dry-run)
[ "$FORCE" = 1 ] && BOOT_ARGS+=(--force)
[ "$WITH_CLI" = 0 ] && BOOT_ARGS+=(--no-cli)
[ -n "$API_KEY" ] && BOOT_ARGS+=(--key "$API_KEY")
[ -n "$UPSTREAM" ] && BOOT_ARGS+=(--upstream "$UPSTREAM")
[ -n "$PRESET" ] && BOOT_ARGS+=(--preset "$PRESET")
[ -n "$MODEL" ] && BOOT_ARGS+=(--model "$MODEL")
if [ "$DRY_RUN" = 1 ] && [ ! -f "$SCRIPTS/bootstrap.py" ]; then
  # 下载分支的演练：源码都还没下来，算不出「会写哪些文件」——
  # 想完整演练就先下载仓库，在仓库目录里跑（那是就地模式，全程不出网）
  log "  [dry-run] 源码尚未下载，这一步只能等真下载后再演练。"
  log "            想完整看计划：下载仓库后在目录里跑 bash install.sh --dry-run"
else
  "$PY" "$SCRIPTS/bootstrap.py" "${BOOT_ARGS[@]}"
fi

if [ "$DRY_RUN" = 1 ]; then
  step "演练结束"
  log "以上动作一个都没执行。确认无误后去掉 --dry-run 再跑一次。"
  exit 0
fi

# ── 起网关 ─────────────────────────────────────────────────
step "⑤ 拉起本地网关"
"$PY" "$SCRIPTS/manager.py" restart >/dev/null 2>&1 || true

# 不能只看「端口有没有人应答」：别人的服务（或另一份 gateway.py）占着同一个端口时，
# 那也会被当成「已就绪」。核对监听者的身份，跟 tools/sync.py 的判据一致。
# 还要等 —— manager 拉起进程就返回了，那会儿它还没绑定端口。
GATEWAY_OK=0
GATEWAY_CAUSE=""
DEADLINE=$((SECONDS + 12))
while [ "$SECONDS" -lt "$DEADLINE" ]; do
  PORT="$("$PY" -c "import json;print(json.load(open('$BRIDGE_HOME/config.json')).get('gateway_port',8789))" 2>/dev/null || echo 8789)"
  LISTEN_PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
  LISTEN_CMD="$(ps -p "${LISTEN_PID:-0}" -o command= 2>/dev/null || true)"
  case "$LISTEN_CMD" in
    *"$DEST/scripts/gateway.py"*)
      ok "网关就绪（端口 ${PORT}），且确认是本次装进去的这份代码"
      GATEWAY_OK=1; break ;;
    *gateway.py*)
      break ;;   # 是网关但不是我们这份，不必再等
  esac
  sleep 0.4
done

if [ "$GATEWAY_OK" = 0 ]; then
  if [ -n "$LISTEN_CMD" ]; then
    case "$LISTEN_CMD" in
      *gateway.py*)
        GATEWAY_CAUSE="foreign_gateway"
        warn "端口 $PORT 上是**另一份** gateway.py，本次安装不会生效："
        warn "    $LISTEN_CMD"
        warn "先停掉它：kill ${LISTEN_PID:-?}" ;;
      *)
        GATEWAY_CAUSE="port_taken"
        warn "端口 $PORT 被别的程序占着，网关绑不上它："
        warn "    $LISTEN_CMD"
        warn "停掉它，或改个端口：编辑 $BRIDGE_HOME/config.json 的 gateway_port 后重跑本脚本" ;;
    esac
  else
    GATEWAY_CAUSE="not_up"
    warn "网关没起来（等了 12 秒）。在客户端里发一条消息会自动拉起，也可以手动跑："
    warn "    $PY $DEST/scripts/manager.py restart"
    warn "起不来时看 $BRIDGE_HOME/logs/gateway.log"
  fi
fi

# ── 收尾 ───────────────────────────────────────────────────
step "⑥ 打开配置页"
PAGE_URL="$("$PY" "$SCRIPTS/webui.py" --url 2>/dev/null || true)"
if [ -n "$PAGE_URL" ]; then
  if [ "$OPEN_PAGE" = 1 ]; then
    if act open "$PAGE_URL"; then
      ok "配置页已在浏览器打开 —— 在上面填一个 API Key 就能用了"
      log "  这个地址带访问令牌，别分享给别人"
    else
      # 自动打开失败（SSH、没配默认浏览器等）时，必须把「接下来做什么」说清楚，
      # 否则用户就卡在这里了 —— 安装脚本最后一步不能只丢一个地址。
      warn "浏览器没能自动打开。请手动复制下面这一整行到浏览器地址栏："
      log ""
      log "  $PAGE_URL"
      log ""
      # PORT 是在上面那个等待循环里赋的值；用 :- 兜底，免得循环没跑到时
      # 在 set -u 下直接报错（安装脚本的最后一步不该因为这种事失败）
      log "  打开一次之后，以后直接访问 http://127.0.0.1:${PORT:-8789} 即可（浏览器会记住一年）"
    fi
  else
    log "  配置页：$PAGE_URL"
  fi
else
  warn "没拿到配置页地址。装完在客户端里发一条消息会自动拉起网关，"
  warn "然后运行这条命令拿地址：$PY $DEST/scripts/webui.py --url"
fi

log ""
log "──────────────────────────────────────────────"
if [ "$GATEWAY_OK" = 0 ]; then
  # 三种原因的后果**不一样**，别用一句话盖过去：
  # 端口上跑着另一份 gateway.py 时，客户端连得上是连得上 —— 只是用的是旧那份代码。
  # 把它说成「会报连不上」会让人去排查一个不存在的故障（重装时最容易撞上）。
  case "$GATEWAY_CAUSE" in
    foreign_gateway)
      warn "网关连得上是连得上，但端口上跑的是**旧那份**代码 —— 重启一次网关才会用上这次装的。" ;;
    *)
      warn "网关没就位（见上）：客户端连上去会报连不上，先把它解决掉再重启客户端。" ;;
  esac
  log ""
fi
log "最后一步：**完全退出 Claude 再重新打开**"
log "（不是关窗口 —— 要选「退出」；3P 的地址、Key、模型映射都只在启动时读一次）"
log "──────────────────────────────────────────────"
