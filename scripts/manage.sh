#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
EXISTING_PLIST="$(/usr/bin/find "${PROJECT_DIR}" -maxdepth 1 -name 'com.*.dws-auto-reply.plist' -print -quit)"
if [[ -n "${EXISTING_PLIST}" ]]; then
  SOURCE_PLIST="${EXISTING_PLIST}"
  LABEL="${EXISTING_PLIST:t:r}"
else
  LABEL="${DWS_AUTO_REPLY_LABEL:-com.local.dws-auto-reply}"
  SOURCE_PLIST="${PROJECT_DIR}/runtime/${LABEL}.plist"
fi
USER_ID="$(id -u)"
DOMAIN="gui/${USER_ID}"
SERVICE="${DOMAIN}/${LABEL}"
USER_HOME_PATH="$(eval print -r -- '~'"$(id -un)")"
AGENTS_DIR="${USER_HOME_PATH}/Library/LaunchAgents"
INSTALLED_PLIST="${AGENTS_DIR}/${LABEL}.plist"
PYTHON="${PROJECT_DIR}/.venv/bin/python"

usage() {
  print "用法: $0 {install|start|stop|restart|status|logs|check}"
}

ensure_source_plist() {
  if [[ -f "${SOURCE_PLIST}" ]]; then
    return
  fi
  mkdir -p "${PROJECT_DIR}/runtime"
  /usr/bin/sed \
    -e "s|com.local.dws-auto-reply|${LABEL}|g" \
    -e "s|PROJECT_DIR|${PROJECT_DIR}|g" \
    "${PROJECT_DIR}/launchd.plist.example" > "${SOURCE_PLIST}"
}

is_loaded() {
  launchctl print "${SERVICE}" >/dev/null 2>&1
}

preflight() {
  ensure_source_plist
  test -x "${PYTHON}" || {
    print -u2 "缺少虚拟环境：${PYTHON}"
    print -u2 "请先运行：cd ${PROJECT_DIR} && uv sync"
    exit 1
  }
  mkdir -p "${PROJECT_DIR}/logs" "${PROJECT_DIR}/runtime"
  plutil -lint "${SOURCE_PLIST}" >/dev/null
  "${PYTHON}" -m app.main --config "${PROJECT_DIR}/config.yaml" --check
}

show_status() {
  if is_loaded; then
    print "launchd: 已加载 (${SERVICE})"
    launchctl print "${SERVICE}" | awk '
      /state =/ || /pid =/ || /last exit code =/ || /runs =/ { print "  " $0 }
    '
  else
    print "launchd: 未加载"
  fi
  awk '
    /^safety:/ { in_safety=1; next }
    in_safety && /^  send_enabled:/ { print "发送开关:" $2 }
    in_safety && /^  send_scope:/ { print "发送范围:" $2; exit }
  ' "${PROJECT_DIR}/config.yaml"
  print "控制台: http://127.0.0.1:8765"
  print "日志: ${PROJECT_DIR}/logs/app.log"
}

action="${1:-}"
case "${action}" in
  install)
    preflight
    mkdir -p "${AGENTS_DIR}"
    if is_loaded; then
      launchctl bootout "${DOMAIN}" "${INSTALLED_PLIST}"
    fi
    /usr/bin/install -m 600 "${SOURCE_PLIST}" "${INSTALLED_PLIST}"
    launchctl bootstrap "${DOMAIN}" "${INSTALLED_PLIST}"
    print "已安装并启动；以后登录 macOS 时会自动运行。"
    show_status
    ;;
  start)
    preflight
    if is_loaded; then
      launchctl kickstart "${SERVICE}"
    elif test -f "${INSTALLED_PLIST}"; then
      launchctl bootstrap "${DOMAIN}" "${INSTALLED_PLIST}"
    else
      print -u2 "尚未安装，请先运行：$0 install"
      exit 1
    fi
    show_status
    ;;
  stop)
    if is_loaded; then
      launchctl bootout "${DOMAIN}" "${INSTALLED_PLIST}"
      print "已优雅停止；plist 仍保留，下次可用 start 启动。"
    else
      print "服务当前未运行。"
    fi
    ;;
  restart)
    preflight
    if is_loaded; then
      launchctl bootout "${DOMAIN}" "${INSTALLED_PLIST}"
    fi
    test -f "${INSTALLED_PLIST}" || /usr/bin/install -m 600 "${SOURCE_PLIST}" "${INSTALLED_PLIST}"
    launchctl bootstrap "${DOMAIN}" "${INSTALLED_PLIST}"
    show_status
    ;;
  status)
    show_status
    ;;
  logs)
    lines="${2:-100}"
    case "${lines}" in
      *[!0-9]*|'') print -u2 "日志行数必须是正整数"; exit 1 ;;
    esac
    tail -n "${lines}" "${PROJECT_DIR}/logs/app.log"
    ;;
  check)
    preflight
    print "启动前检查通过。"
    ;;
  *)
    usage
    exit 2
    ;;
esac
