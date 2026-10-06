#!/bin/bash
# 驱动版本在每次运行时读取并记录。
# 加载/取消加载测试：./hitest load-unload [次数] [等待秒数]，默认 1 次、100 秒。
set -euo pipefail

if [[ -z "${HITEST_RUN_DIR:-}" ]]; then
    SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
    exec bash "$SCRIPT_DIR/../hitest" load-unload "$@"
fi

TOTAL_ROUNDS=${1-1}
SLEEP_SECONDS=${2-100}
if (( $# > 2 )) || [[ ! "$TOTAL_ROUNDS" =~ ^[1-9][0-9]{0,8}$ ]] ||
    [[ ! "$SLEEP_SECONDS" =~ ^(0|[1-9][0-9]{0,8})$ ]]; then
    echo "用法：./hitest load-unload [次数] [等待秒数]；次数为 1～999999999，等待秒数为 0～999999999 的整数（不带前导零）；默认 1 轮、100 秒。" >&2
    exit 2
fi

HY_SMI=/opt/hyhal/bin/hy-smi
LOG_DIR=$HITEST_RUN_DIR
STOP_FILE=${HITEST_STOP_FILE:-"$LOG_DIR/stop.request"}
DRIVER_STATE=unchanged
COMPLETED_ROUNDS=0

stop_requested() {
    [[ -f "$STOP_FILE" ]]
}

stop_code() {
    local code
    code=$(cat "$STOP_FILE")
    case "$code" in
        143) return 143 ;;
        *) return 130 ;;
    esac
}

# 仅驱动操作和读取内核日志使用 root，日志文件由当前用户创建。
SUDO=()
if (( EUID != 0 )); then
    SUDO=(sudo -n)
fi

finish() {
    local rc=$?
    trap - EXIT
    if stop_requested; then
        echo "[$(date '+%F %T')] 收到安全停止请求。"
        if (( rc == 0 )); then
            stop_code || rc=$?
        fi
    fi
    if ! "${SUDO[@]}" dmesg > "$LOG_DIR/sut_dmesg.txt"; then
        echo "保存结束时的 dmesg 失败。"
        rc=1
    fi
    printf 'completed_rounds=%s\ndriver_state=%s\n' "$COMPLETED_ROUNDS" "$DRIVER_STATE" >> "$LOG_DIR/result.txt"
    if [[ "$DRIVER_STATE" == unknown ]]; then
        echo "驱动操作失败，状态未确认；请检查日志，不自动重试加载或卸载。"
    fi
    echo "[$(date '+%F %T')] 结束，退出码：$rc；日志：$LOG_DIR"
    exit "$rc"
}
trap finish EXIT

if stop_requested; then
    stop_code
fi
if (( EUID != 0 )); then
    sudo -v
fi

echo "日志目录：$LOG_DIR"
printf '测试参数：%s 轮，每轮加载后等待 %s 秒\n' "$TOTAL_ROUNDS" "$SLEEP_SECONDS"
printf 'total_rounds=%s\nsleep_seconds=%s\n' "$TOTAL_ROUNDS" "$SLEEP_SECONDS" >> "$LOG_DIR/result.txt"
cat /opt/hyhal/.info/version > "$LOG_DIR/version.txt"
DRIVER_VERSION=$(head -n 1 "$LOG_DIR/version.txt")
printf '[%s] 测试驱动版本：%s\n' "$(date '+%F %T')" "$DRIVER_VERSION"
printf 'driver_version=%s\n' "$DRIVER_VERSION" >> "$LOG_DIR/result.txt"
"${SUDO[@]}" dmesg > "$LOG_DIR/dmesg_before.txt"

# 先建立未加载基线；初始卸载不计入测试轮数。
if [[ ! -d /sys/module || ! -r /sys/module || ! -x /sys/module ]]; then
    echo "无法读取 /sys/module，不能确认初始驱动状态。" >&2
    exit 1
fi
if [[ -d /sys/module/hycu ]]; then
    INITIAL_DRIVER_STATE=loaded
else
    INITIAL_DRIVER_STATE=unloaded
fi
printf '[%s] 初始驱动状态：%s（依据 hycu 模块是否存在）\n' "$(date '+%F %T')" "$INITIAL_DRIVER_STATE"
printf 'initial_driver_state=%s\n' "$INITIAL_DRIVER_STATE" >> "$LOG_DIR/result.txt"
if stop_requested; then
    stop_code
fi
if [[ "$INITIAL_DRIVER_STATE" == loaded ]]; then
    echo "[$(date '+%F %T')] 初始驱动已加载，先执行 hy-smi --unloaddriver"
    DRIVER_STATE=unknown
    "${SUDO[@]}" "$HY_SMI" --unloaddriver
    if [[ -d /sys/module/hycu ]]; then
        echo "初始卸载后 hycu 模块仍存在，停止测试。" >&2
        exit 1
    fi
    DRIVER_STATE=unloaded
    "${SUDO[@]}" dmesg > "$LOG_DIR/dmesg_initial_unload.txt"
fi

for (( round=1; round<=TOTAL_ROUNDS; round++ )); do
    if stop_requested; then
        stop_code
    fi
    # 每轮刷新 sudo 凭据，避免多轮执行时原有凭据过期。
    if (( EUID != 0 )); then
        sudo -v
    fi
    if stop_requested; then
        stop_code
    fi
    echo "[$(date '+%F %T')] 第 $round/$TOTAL_ROUNDS 轮：hy-smi --loaddriver"
    DRIVER_STATE=unknown
    "${SUDO[@]}" "$HY_SMI" --loaddriver
    DRIVER_STATE=loaded

    echo "[$(date '+%F %T')] sleep $SLEEP_SECONDS"
    for (( waited=0; waited<SLEEP_SECONDS; waited++ )); do
        stop_requested && break
        sleep 1
    done

    echo "[$(date '+%F %T')] 第 $round/$TOTAL_ROUNDS 轮：hy-smi --unloaddriver"
    DRIVER_STATE=unknown
    "${SUDO[@]}" "$HY_SMI" --unloaddriver
    DRIVER_STATE=unloaded
    COMPLETED_ROUNDS=$round
    "${SUDO[@]}" dmesg > "$LOG_DIR/dmesg_round_${round}.txt"
    echo "[$(date '+%F %T')] 第 $round/$TOTAL_ROUNDS 轮完成。"
    if stop_requested; then
        stop_code
    fi
done
echo "[$(date '+%F %T')] 全部 $TOTAL_ROUNDS 轮 load/unload 命令执行完成。"
