#!/usr/bin/env bash
# NVIDIA 驱动模块循环加载/卸载；只操作已安装驱动，不安装驱动或停止业务。
set -euo pipefail
if [[ -z "${HITEST_RUN_DIR:-}" ]]; then
    script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
    exec bash "$script_dir/../hitest" nvidia-load-unload "$@"
fi
check_only=0
if [[ "${1:-}" == --check ]]; then check_only=1; shift; fi
TOTAL_ROUNDS=${1-1}
SLEEP_SECONDS=${2-100}
if (( $# > 2 )) || [[ ! "$TOTAL_ROUNDS" =~ ^[1-9][0-9]{0,8}$ ]] || [[ ! "$SLEEP_SECONDS" =~ ^(0|[1-9][0-9]{0,8})$ ]]; then
    echo '用法：./hitest nvidia-load-unload [--check] [次数] [等待秒数]；默认 1 轮、100 秒；--check 仅检查条件。' >&2
    exit 2
fi
if (( EUID != 0 )); then
    echo 'NVIDIA 模块测试需要 root，请使用 sudo ./hitest nvidia-load-unload。' >&2
    exit 1
fi
LOG_DIR=$HITEST_RUN_DIR
STOP_FILE=${HITEST_STOP_FILE:-"$LOG_DIR/stop.request"}
MODULE_ROOT=/sys/module
PCI_ROOT=/sys/bus/pci/devices
DRM_ROOT=/sys/class/drm
DEVICE_ROOT=/dev
LOCK_FILE=/run/hitest/nvidia-load-unload.lock
DRIVER_STATE=unchanged
COMPLETED_ROUNDS=0
modules=()
unload_order=(nvidia_fs nvidia_peermem nvidia_drm nvidia_modeset nvidia_uvm nvidia)
stop_requested() { [[ -f "$STOP_FILE" ]]; }
stop_code() { local code; code=$(cat "$STOP_FILE"); [[ "$code" != 143 ]] || return 143; return 130; }
module_loaded() { [[ -d "$MODULE_ROOT/$1" ]]; }
finish() {
    local rc=$?
    trap - EXIT
    if stop_requested && (( rc == 0 )); then stop_code || rc=$?; fi
    if ! dmesg > "$LOG_DIR/sut_dmesg.txt"; then (( rc != 0 )) || rc=1; fi
    printf 'completed_rounds=%s\ndriver_state=%s\n' "$COMPLETED_ROUNDS" "$DRIVER_STATE" >> "$LOG_DIR/result.txt"
    echo "结束，退出码：$rc；日志：$LOG_DIR"
    exit "$rc"
}
for tool in modprobe modinfo nvidia-smi dmesg fuser flock sort cmp; do
    command -v "$tool" >/dev/null || { echo "缺少命令：$tool" >&2; exit 1; }
done
# 主机级互斥，防止两个独立入口同时修改 NVIDIA 模块。
lock_parent=${LOCK_FILE%/*}
mkdir -p -m 700 -- "$lock_parent"
lock_mode=$(stat -c '%a' -- "$lock_parent")
if [[ -L "$lock_parent" || "$(stat -c '%u' -- "$lock_parent")" != "$EUID" ]] ||
    (( (8#$lock_mode & 0022) != 0 )) || [[ -L "$LOCK_FILE" || ( -e "$LOCK_FILE" && ! -f "$LOCK_FILE" ) ]]; then
    echo '测试锁路径权限不安全，停止测试。' >&2
    exit 1
fi
exec 9>"$LOCK_FILE"
flock -n 9 || { echo '已有 NVIDIA load/unload 测试在运行。' >&2; exit 1; }
trap finish EXIT
stop_requested && stop_code
[[ -r "$MODULE_ROOT" && -x "$MODULE_ROOT" && -r "$PCI_ROOT" ]] || { echo '无法读取 sysfs，停止测试。' >&2; exit 1; }
if module_loaded nouveau; then echo '检测到 nouveau 驱动；请先由管理员配置 NVIDIA 驱动。' >&2; exit 1; fi
shopt -s nullglob
found_gpu=0
for device in "$PCI_ROOT"/*; do
    [[ -r "$device/vendor" && -r "$device/class" ]] || continue
    read -r vendor < "$device/vendor"
    read -r pci_class < "$device/class"
    if [[ "$vendor" == 0x10de && "$pci_class" == 0x03* ]]; then found_gpu=1; fi
done
(( found_gpu )) || { echo '没有检测到 NVIDIA PCI 显卡，停止测试。' >&2; exit 1; }
# 核心和 UVM 必需；显示模块存在时纳入测试，额外模块仅在初始加载时纳入。
for module in nvidia nvidia_uvm; do modinfo "$module" >/dev/null; modules+=("$module"); done
for module in nvidia_modeset nvidia_drm; do
    if modinfo "$module" >/dev/null 2>&1; then modules+=("$module");
    elif module_loaded "$module"; then echo "已加载的 $module 缺少磁盘模块，无法重新加载。" >&2; exit 1; fi
done
for module in nvidia_peermem nvidia_fs; do
    if module_loaded "$module"; then modinfo "$module" >/dev/null; modules+=("$module"); fi
done
modinfo -F version nvidia > "$LOG_DIR/version.txt"
DRIVER_VERSION=$(head -n 1 "$LOG_DIR/version.txt")
[[ -n "$DRIVER_VERSION" ]] || { echo '无法读取 NVIDIA 驱动版本。' >&2; exit 1; }
printf 'driver_version=%s\ntotal_rounds=%s\nsleep_seconds=%s\ncheck_only=%s\n' "$DRIVER_VERSION" "$TOTAL_ROUNDS" "$SLEEP_SECONDS" "$check_only" >> "$LOG_DIR/result.txt"
printf 'modules=%s\n' "${modules[*]}" >> "$LOG_DIR/result.txt"
dmesg > "$LOG_DIR/dmesg_before.txt"

check_idle() {
    local module holder name rc node
    local -a devices=("$DEVICE_ROOT"/nvidia* "$DEVICE_ROOT"/nvidia-caps/*)
    # 仅检查 NVIDIA 对应的 DRM 节点，不影响其它厂商显卡。
    for node in "$DRM_ROOT"/*; do
        [[ -L "$node/device/driver" ]] || continue
        [[ "$(basename "$(readlink -f "$node/device/driver")")" == nvidia ]] || continue
        name=${node##*/}; [[ ! -e "$DEVICE_ROOT/dri/$name" ]] || devices+=("$DEVICE_ROOT/dri/$name")
    done
    local -a files=()
    for node in "${devices[@]}"; do [[ ! -c "$node" && ! -f "$node" ]] || files+=("$node"); done
    if (( ${#files[@]} )); then
        rc=0
        fuser -- "${files[@]}" > "$LOG_DIR/device_users.txt" 2>&1 || rc=$?
        if (( rc == 0 )); then
            cat "$LOG_DIR/device_users.txt"
            echo 'NVIDIA 设备仍被进程使用；请结束 GPU 任务及相关服务后重试。不自动杀进程或停止服务。' >&2
            return 1
        elif (( rc != 1 )); then
            cat "$LOG_DIR/device_users.txt"; echo '无法确认设备占用情况，停止测试。' >&2; return 1
        fi
    fi
    for module in "${unload_order[@]}"; do
        for holder in "$MODULE_ROOT/$module/holders"/*; do
            name=${holder##*/}
            case " $name " in ' nvidia_fs '|' nvidia_peermem '|' nvidia_drm '|' nvidia_modeset '|' nvidia_uvm '|' nvidia ') ;;
                *) echo "$module 仍被 $name 引用，停止测试。" >&2; return 1 ;;
            esac
        done
    done
}
unload_stack() {
    local module
    check_idle || return $?
    DRIVER_STATE=unknown
    for module in "${unload_order[@]}"; do
        if module_loaded "$module"; then
            echo "UNLOAD $module"
            modprobe -r "$module" || return $?
            if module_loaded "$module"; then echo "$module 卸载后仍存在。" >&2; return 1; fi
        fi
    done
    # 所有已知模块都应消失；不运行 nvidia-smi，以免触发自动重新加载。
    for module in "${unload_order[@]}"; do
        if module_loaded "$module"; then echo "$module 被重新加载，停止测试。" >&2; return 1; fi
    done
    DRIVER_STATE=unloaded
}
load_stack() {
    local module
    DRIVER_STATE=unknown
    for module in "${modules[@]}"; do
        stop_requested && break
        echo "LOAD $module"
        modprobe "$module" || return $?
        if ! module_loaded "$module"; then echo "$module 加载后未出现。" >&2; return 1; fi
    done
    DRIVER_STATE=loaded
}
check_idle
initial_state=unloaded
for module in "${unload_order[@]}"; do if module_loaded "$module"; then initial_state=loaded; fi; done
printf 'initial_driver_state=%s\n' "$initial_state" >> "$LOG_DIR/result.txt"
if (( check_only )); then echo '前置检查通过，未执行加载或卸载。'; exit 0; fi
stop_requested && stop_code
if [[ "$initial_state" == loaded ]]; then
    echo '初始模块已加载，先卸载建立基线。'
    unload_stack
    dmesg > "$LOG_DIR/dmesg_initial_unload.txt"
fi
for (( round=1; round<=TOTAL_ROUNDS; round++ )); do
    stop_requested && stop_code
    echo "第 $round/$TOTAL_ROUNDS 轮加载：${modules[*]}"
    load_stack
    validated=0
    if ! stop_requested; then
        rc=0
        nvidia-smi --query-gpu=uuid,name,driver_version --format=csv,noheader > "$LOG_DIR/nvidia_smi_round_${round}.csv" || rc=$?
        if (( rc == 0 )) && [[ ! -s "$LOG_DIR/nvidia_smi_round_${round}.csv" ]]; then rc=1; fi
        if (( rc == 0 )); then
            nvidia-smi --query-gpu=uuid --format=csv,noheader | sort > "$LOG_DIR/gpu_uuids_round_${round}.txt" || rc=$?
            if (( rc == 0 )) && [[ ! -s "$LOG_DIR/gpu_uuids_round_${round}.txt" ]]; then rc=1; fi
            if (( round > 1 && rc == 0 )) && ! cmp -s "$LOG_DIR/gpu_uuids_round_1.txt" "$LOG_DIR/gpu_uuids_round_${round}.txt"; then rc=1; fi
        fi
        if (( rc != 0 )); then
            echo 'nvidia-smi 检查失败或 GPU 集合发生变化，尝试本轮卸载后停止。' >&2
            cleanup_rc=0; unload_stack || cleanup_rc=$?
            if (( cleanup_rc != 0 )); then exit "$cleanup_rc"; fi
            exit "$rc"
        fi
        validated=1
        echo "sleep $SLEEP_SECONDS"
        for (( waited=0; waited<SLEEP_SECONDS; waited++ )); do stop_requested && break; sleep 1; done
    fi
    echo "第 $round/$TOTAL_ROUNDS 轮卸载。"
    unload_stack
    if (( validated )); then COMPLETED_ROUNDS=$round; fi
    dmesg > "$LOG_DIR/dmesg_round_${round}.txt"
    stop_requested && stop_code
done
echo "全部 $TOTAL_ROUNDS 轮 NVIDIA load/unload 完成，模块处于卸载状态。"
