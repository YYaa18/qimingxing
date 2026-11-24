#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="${ROOT_DIR}/.venv/bin/python"
RUN_DIR="${ROOT_DIR}/.run"
HOST="${QIMINGXING_HOST:-127.0.0.1}"
PORT="${QIMINGXING_PORT:-18790}"
BRAIN_PID_FILE="${RUN_DIR}/brain.pid"
SENSES_PID_FILE="${RUN_DIR}/senses.pid"
BRAIN_LOG="${RUN_DIR}/brain.log"
SENSES_LOG="${RUN_DIR}/senses.log"

BASE_HTTP="http://${HOST}:${PORT}"
BASE_WS="ws://${HOST}:${PORT}/ws"

mkdir -p "${RUN_DIR}"

ensure_venv() {
  if [[ ! -x "${VENV_PY}" ]]; then
    echo "❌ 未找到虚拟环境: ${VENV_PY}" >&2
    echo "   请先执行: uv venv --python 3.12 .venv && uv pip install -e '.[dev]'" >&2
    exit 1
  fi
}

read_pid() {
  local pid_file="$1"
  if [[ -f "${pid_file}" ]]; then
    tr -d '[:space:]' < "${pid_file}" || true
  fi
}

is_pid_running() {
  local pid="$1"
  [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null
}

is_health_ok() {
  curl -fsS --max-time 1 "${BASE_HTTP}/health" >/dev/null 2>&1
}

find_orphan_pids() {
  local module="$1"
  local pattern=" -m ${module}"
  if [[ -n "${QIMINGXING_PORT:-}" && "${module}" == "senses.main" ]]; then
    pattern=" -m ${module}.*--ws-url ${BASE_WS}"
  fi
  pgrep -f "${VENV_PY}${pattern}" || true
}

start_brain() {
  local existing_pid
  existing_pid="$(read_pid "${BRAIN_PID_FILE}")"
  if is_pid_running "${existing_pid}"; then
    echo "✅ Brain 已在运行 (PID ${existing_pid})"
    return 0
  fi
  rm -f "${BRAIN_PID_FILE}"

  if is_health_ok; then
    echo "⚠️  端口 ${PORT} 已有 Brain 实例响应，跳过重复启动"
    return 0
  fi

  echo "🚀 启动 Brain 服务 (${BASE_HTTP})..."
  (
    cd "${ROOT_DIR}"
    nohup "${VENV_PY}" -m brain.main >"${BRAIN_LOG}" 2>&1 &
    echo $! > "${BRAIN_PID_FILE}"
  )

  local i=0
  while (( i < 40 )); do
    if is_health_ok; then
      echo "✅ Brain 启动成功 (PID $(read_pid "${BRAIN_PID_FILE}"))"
      return 0
    fi
    sleep 0.25
    i=$((i + 1))
  done

  echo "❌ Brain 启动超时，最近日志：" >&2
  tail -n 20 "${BRAIN_LOG}" >&2 || true
  return 1
}

start_senses() {
  local existing_pid
  existing_pid="$(read_pid "${SENSES_PID_FILE}")"
  if is_pid_running "${existing_pid}"; then
    echo "✅ Senses 已在运行 (PID ${existing_pid})"
    return 0
  fi
  rm -f "${SENSES_PID_FILE}"

  echo "👁️  启动跨平台桌面感官 Senses..."
  (
    cd "${ROOT_DIR}"
    nohup "${VENV_PY}" -m senses.main --ws-url "${BASE_WS}" --http-base "${BASE_HTTP}" >"${SENSES_LOG}" 2>&1 &
    echo $! > "${SENSES_PID_FILE}"
  )
  sleep 0.5
  local spid
  spid="$(read_pid "${SENSES_PID_FILE}")"
  if is_pid_running "${spid}"; then
    echo "✅ Senses 启动成功 (PID ${spid})"
  else
    echo "⚠️  Senses 启动失败，请检查日志: ${SENSES_LOG}" >&2
  fi
}

stop_component() {
  local name="$1"
  local pid_file="$2"
  local module="$3"
  local pids=()

  local file_pid
  file_pid="$(read_pid "${pid_file}")"
  if is_pid_running "${file_pid}"; then
    pids+=("${file_pid}")
  fi

  local orphan
  for orphan in $(find_orphan_pids "${module}"); do
    if [[ " ${pids[*]-} " != *" ${orphan} "* ]]; then
      pids+=("${orphan}")
    fi
  done

  if (( ${#pids[@]} == 0 )); then
    rm -f "${pid_file}"
    echo "ℹ️  ${name} 未运行"
    return 0
  fi

  echo "🛑 正在停止 ${name} (PID: ${pids[*]})..."
  kill "${pids[@]}" 2>/dev/null || true

  local i=0
  while (( i < 20 )); do
    local any_alive=0
    for p in "${pids[@]}"; do
      if is_pid_running "${p}"; then
        any_alive=1
        break
      fi
    done
    if (( any_alive == 0 )); then
      rm -f "${pid_file}"
      echo "✅ ${name} 已停止"
      return 0
    fi
    sleep 0.25
    i=$((i + 1))
  done

  echo "⚠️  ${name} 未在 5s 内退出，强制结束..."
  kill -9 "${pids[@]}" 2>/dev/null || true
  rm -f "${pid_file}"
  echo "✅ ${name} 已强制停止"
}

cmd_start() {
  local no_open=0
  for arg in "$@"; do
    if [[ "${arg}" == "--no-open" ]]; then
      no_open=1
    fi
  done

  ensure_venv
  start_brain
  start_senses

  echo ""
  echo "🌟 启明星已就绪: ${BASE_HTTP}/"
  echo "   日志文件: ${BRAIN_LOG} | ${SENSES_LOG}"
  if (( no_open == 0 )) && command -v open >/dev/null 2>&1; then
    open "${BASE_HTTP}/"
  fi
}

cmd_stop() {
  stop_component "Senses" "${SENSES_PID_FILE}" "senses.main"
  stop_component "Brain" "${BRAIN_PID_FILE}" "brain.main"
}

cmd_status() {
  local bpid spid
  bpid="$(read_pid "${BRAIN_PID_FILE}")"
  spid="$(read_pid "${SENSES_PID_FILE}")"

  echo "=== 启明星（Qimingxing）运行状态 ==="
  if is_pid_running "${bpid}"; then
    echo "Brain : 运行中 (PID ${bpid})"
  elif is_health_ok; then
    echo "Brain : 运行中 (外部启动进程，监听 ${PORT})"
  else
    echo "Brain : 已停止"
  fi

  if is_pid_running "${spid}"; then
    echo "Senses: 运行中 (PID ${spid})"
  else
    local orphans
    orphans="$(find_orphan_pids "senses.main" | tr '\n' ' ')"
    if [[ -n "${orphans// /}" ]]; then
      echo "Senses: 运行中 (PID ${orphans})"
    else
      echo "Senses: 已停止"
    fi
  fi

  if is_health_ok; then
    echo -n "Health: "
    curl -fsS "${BASE_HTTP}/health"
    echo ""
  else
    echo "Health: 不可达 (${BASE_HTTP}/health)"
  fi
}

cmd_logs() {
  touch "${BRAIN_LOG}" "${SENSES_LOG}"
  echo "📜 正在跟踪日志 (Ctrl+C 退出查看，不影响后台运行)..."
  tail -n 30 -f "${BRAIN_LOG}" "${SENSES_LOG}"
}

usage() {
  cat <<EOF
用法: ./qimingxing.sh <command> [options]

Commands:
  start [--no-open]    一键启动 Brain + Senses，并在浏览器打开界面
  stop                 一键停止 Senses 与 Brain
  restart [--no-open]  重启全部服务
  status               查看 Brain / Senses 运行状态与健康检查
  logs                 实时查看后台日志
EOF
}

CMD="${1:-}"
shift || true

case "${CMD}" in
  start)
    cmd_start "$@"
    ;;
  stop)
    cmd_stop
    ;;
  restart)
    cmd_stop
    cmd_start "$@"
    ;;
  status)
    cmd_status
    ;;
  logs)
    cmd_logs
    ;;
  *)
    usage
    exit 1
    ;;
esac
