# HiTest

服务器测试脚本集合。使用统一入口运行，每次测试单独保存日志。

## 快速开始

```bash
cd /home/shiai/aototest/subtools/HiTest
./hitest list
./hitest load-unload         # 默认 1 轮，每轮等待 100 秒
./hitest load-unload 3       # 3 轮，每轮等待 100 秒
./hitest load-unload 10 30   # 10 轮，每轮等待 30 秒
./hitest load-unload 3 0     # 3 轮，加载成功后立即卸载
```

在其他目录也可以通过绝对路径运行，日志位置不受当前工作目录影响。
建议使用普通用户执行，脚本会在需要时调用 sudo，日志归当前用户所有。

## 文件布局

```text
HiTest/
├── hitest                  # 统一命令入口
├── README.md
├── .gitignore
├── scripts/                # 测试脚本，一个文件对应一个命令
│   ├── load-unload.sh
│   └── nvidia-load-unload.sh
├── deploy/deploy.py        # 使用本机平台凭据部署到登记的测试机
├── tests/                  # 工程自身的模拟验证，不操作设备
│   └── test_safe_stop.py
└── logs/                   # 运行产物，不提交到版本库
    └── load-unload/
        └── 2026-09-23_17-00-00/
            ├── run.log
            ├── result.txt
            ├── version.txt
            ├── dmesg_before.txt
            ├── dmesg_round_1.txt
            └── sut_dmesg.txt
```

脚本放在 `scripts/`，所有运行生成的文件放在本次日志目录。后续若有配置、
静态数据或第三方工具，再按需添加 `config/`、`data/`、`tools/`，不要混入日志。

## 驱动加载/卸载测试

`./hitest load-unload [次数] [等待秒数]`，先次数、后等待秒数。
次数为 1～999999999 的整数，默认 1；等待秒数为 0～999999999 的整数，
默认 100，0 表示不等待。参数不接受小数、负数或前导零。
等待时间作用于每轮加载成功后、卸载前，实际每轮耗时还包括驱动操作和日志保存。
参数会记录到 `run.log` 和 `result.txt`（`total_rounds`、`sleep_seconds`）。
由原 `driver_load_unload_simple.sh` 整理而来，原环境版本为
`6.4.2-V2.1.0 20260911-092539`。

运行需要 Bash、常见 Linux 工具、可用的 sudo 权限（或 root），以及：

- `/opt/hyhal/bin/hy-smi`
- `/opt/hyhal/.info/version`
- 读取 `dmesg` 的权限

启动时按 `/sys/module/hycu` 是否存在判断初始加载状态，并写入日志：

- 未加载：直接进入第一轮加载。
- 已加载：先执行一次 `hy-smi --unloaddriver`，确认 hycu 模块已消失后再进入第一轮加载。
- 初始卸载失败或模块仍存在：停止，不继续加载。

初始卸载不计入测试轮数；模块存在与否仅用来判断加载状态，不代表设备健康。
每轮依次执行 `hy-smi --loaddriver`、等待指定秒数（默认 100 秒）、
`hy-smi --unloaddriver`，然后保存本轮内核日志。任一命令失败即停止，
结束时尝试保存完整内核日志。该测试会实际加载、卸载驱动，请在设备空闲时运行；
成功完成后驱动处于卸载状态；收到安全停止请求时按下面的规则收尾。
退出码 0 表示所有命令执行成功，不代表已自动分析内核日志中的错误。

## 中途停止

运行 `./hitest load-unload`、`./hitest nvidia-load-unload`（或直接用 Bash 运行测试脚本）时，按一次 Ctrl+C
会请求安全停止，不直接打断正在执行的驱动命令：

| 收到请求时的阶段 | 处理方式 |
| --- | --- |
| 尚未开始加载 | 不开始新一轮，保存日志后退出 |
| 正在加载 | 等待加载返回；成功后跳过等待并执行一次卸载 |
| 等待期间 | 在约 1 秒内结束等待，执行一次卸载 |
| 正在卸载 | 等待卸载返回，保存日志后退出 |

初始卸载阶段也遵循上述规则：收到停止请求后完成卸载，不再开始第一轮加载。

连续按 Ctrl+C 仍然只请求安全停止，不会升级为强制终止。
向入口进程发送 SIGTERM 也会走相同流程。
驱动命令和 `tee` 在测试期间忽略 SIGINT/SIGTERM，入口通过本次目录中的
`stop.request` 通知测试收尾，因此请向入口进程发信号，不要单独终止内部进程。

Ctrl+C 安全停止的退出码为 130，SIGTERM 为 143；驱动操作或日志收尾失败时
返回对应失败码。`result.txt` 额外记录 `completed_rounds` 和 `driver_state`：
`unchanged` 表示本次尚未操作驱动，`loaded` / `unloaded` 表示对应命令已成功，
`unknown` 表示驱动命令失败、实际状态待检查。这些状态依据命令结果记录，
不是独立的设备健康检查。加载或卸载失败时不自动重试，也不继续下一轮。

这套处理不能防御 `kill -9`、断电或内核卡死；如果驱动命令一直不返回，
安全停止也会等待，不强行打断。sudo 等待密码输入时仍需完成认证，
可在运行前用 `sudo -v` 提前验证权限。已启动的旧进程不会获得本次改动。

修改停止逻辑后，可执行模拟验证（需要 Python 3，不需要 sudo、不操作真实驱动）：

```bash
python3 -m unittest discover -s tests -v
```

## 日志

入口打印本次日志目录，使用北京时间（`Asia/Shanghai`，UTC+8），
格式为 `YYYY-MM-DD_HH-MM-SS`，不带字母或随机后缀。
日志内容与目录使用同一时区，`result.txt` 的时间带时区偏移 `+0800`。
可通过 `HITEST_TZ` 指定其他时区，例如 `HITEST_TZ=UTC ./hitest load-unload`；不修改系统时区。
同一测试在同一秒已有日志目录时，等待下一秒再创建，避免覆盖。
每次运行保留独立目录，不覆盖历史记录，也不自动删除日志。

| 文件 | 内容 |
| --- | --- |
| `run.log` | 测试标准输出和标准错误 |
| `result.txt` | 测试名、可复用命令、测试驱动版本、开始/结束时间和退出码 |
| `version.txt` | 运行时读取的 `/opt/hyhal/.info/version` 完整内容；首行版本也会标注在 `run.log` 中 |
| `dmesg_before.txt` | 测试前完整内核日志 |
| `dmesg_initial_unload.txt` | 初始已加载时，预先卸载后的完整内核日志 |
| `dmesg_round_N.txt` | 第 N 轮完成后的完整内核日志 |
| `sut_dmesg.txt` | 测试退出时的完整内核日志 |

初始化失败时可能只有部分文件；进程被强制终止或主机断电时可能没有结束记录。
入口返回测试退出码，日志写入失败也会返回非零值。
可通过环境变量改变日志根目录：

```bash
HITEST_LOG_ROOT=/home/shiai/hitest-logs ./hitest load-unload 3
```

相对路径以 HiTest 根目录为基准。定期归档或清理历史目录；
目录唯一仅用于隔离日志，同一设备的驱动测试应串行执行。

## 添加新测试

1. 新建 `scripts/<测试名>.sh`，名称使用小写字母、数字、连字符或下划线，
   不要使用入口保留的 `help`、`list`。
2. 脚本通过位置参数接收选项，使用 `$HITEST_RUN_DIR` 保存运行产物，
   输出直接写标准输出/错误，由入口统一记录；成功返回 0，失败返回非零。
3. 在本 README 补充依赖、参数及设备影响。

无需修改入口，`./hitest list` 会自动发现脚本，使用 `./hitest <测试名> [参数...]` 运行。
相对资源路径应基于脚本自身位置解析，不要依赖调用者的当前目录。


## 英伟达驱动加载/卸载

独立命令 `nvidia-load-unload`，不会调用海光的 hy-smi，也不会安装驱动。需要 root、Bash、modprobe/modinfo、nvidia-smi、dmesg、fuser（通常来自 psmisc）、flock（通常来自 util-linux）。测试针对 Linux 已安装的 NVIDIA 闭源或开放内核模块，不测试 nouveau。

```bash
cd /home/shiai/aototest/subtools/HiTest
sudo ./hitest nvidia-load-unload --check  # 仅检查依赖、显卡及占用，不加载或卸载
sudo ./hitest nvidia-load-unload 3 0      # 3 轮，每轮加载后立即验证并卸载
sudo ./hitest nvidia-load-unload 10 30    # 10 轮，每轮验证后等待 30 秒
```

默认 1 轮、100 秒，次数和等待时间的范围与海光脚本一致。直接运行 `bash scripts/nvidia-load-unload.sh ...` 也会经统一入口保存日志和处理停止请求。

脚本先确认 PCI NVIDIA 显卡、模块文件和驱动版本；检测到 nouveau、设备进程占用或未知内核模块引用时，停止且不修改模块。日志中的 `device_users.txt` 给出占用进程。请在空闲测试机使用；GPU 任务、显示服务或 nvidia-persistenced 等占用需由管理员事先处理，脚本不会杀进程、关闭图形会话或修改服务配置。模块列表及持久化守护进程行为参考 [NVIDIA 模块说明](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/latest/kernel-modules.html) 和 [NVIDIA 持久化守护进程说明](https://docs.nvidia.com/deploy/driver-persistence/persistence-daemon.html)。

- 加载顺序：nvidia → nvidia_uvm → nvidia_modeset → nvidia_drm；显示模块仅在已安装时纳入，可用于仅安装计算组件的机器。
- 初始已加载的 nvidia_peermem、nvidia_fs 也会纳入。卸载顺序为 nvidia_fs → nvidia_peermem → nvidia_drm → nvidia_modeset → nvidia_uvm → nvidia，跳过当前未加载的模块。
- 初始已有模块时，先卸载建立基线。每轮验证模块实际存在，读取 nvidia-smi GPU 信息和 UUID；GPU 集合变化、命令失败或空输出均停止，不算成功。卸载后检查模块消失，不再调用 nvidia-smi，避免触发重新加载。
- 主机级 flock 保证同一主机只运行一个本命令。模块加载/卸载失败后不强制卸载、不重试、不进入下一轮；状态记录为 unknown，需人工核对。GPU 查询失败时，会尝试本轮正常卸载再以失败码结束。
- Ctrl+C/SIGTERM 采用统一的安全停止机制：当前模块命令返回后收尾；若加载只完成了一部分，卸载已加载模块，不计为完整轮次。不能处理 kill -9、掉电或不返回的内核命令。

**正常完成后 NVIDIA 模块处于卸载状态，不会恢复最初加载状态。** 后续需由管理员按机器用途重新加载驱动或恢复相关服务。退出码 0 表示循环、模块状态及 GPU 查询检查通过，不代表已自动分析 dmesg 中的 Xid 或其他错误。

日志放在 `logs/nvidia-load-unload/<时间>/`，沿用 run.log、result.txt、version.txt、dmesg_before.txt、dmesg_round_N.txt 和 sut_dmesg.txt，并增加：

| 文件 | 内容 |
| --- | --- |
| nvidia_smi_round_N.csv | 每轮 GPU UUID、名称及实际驱动版本 |
| gpu_uuids_round_N.txt | 每轮 GPU UUID 集合，用于比较设备是否丢失或变化 |
| device_users.txt | 最近一次卸载前的设备占用检查输出 |

## 部署到 192.168.0.107

从本机的工作副本打包，包含本次未提交的英伟达脚本；无需在测试机访问 GitHub。部署器使用相邻 `hygon-dcu-test-platform` 的数据库和 SSHTransport，复用管理员页面已保存的 root 凭据及严格主机指纹校验。不会上传凭据、Git 元数据、历史日志，密码不放进参数或部署包。

```bash
cd /home/shiai/aototest/subtools/HiTest
python3 deploy/deploy.py --host 192.168.0.107 --actor shiai
```

平台数据库与凭据须已初始化；`--actor` 指定用于审计的启用平台管理员账号。系统管理员需有本机凭据文件的读取权限，API/worker 正在使用的 `DCU_*` 环境配置也应传给此终端。平台路径不同时，增加 `--platform-dir /实际平台路径`。`--package-only` 只生成部署包，不连接测试机。

部署前会申请 15 分钟平台预约，已有测试、工具操作或预约占用时拒绝，结束后只释放本次预约。部署器检查目标 root 与 Python 3.10+，通过 SCP 传送部署包，核对压缩包和每个文件的 SHA256，拒绝路径越界、符号链接和清单不符的归档。通过 Bash 语法和 `hitest list` 检查后，原子切换 current；保留之前版本和共享日志目录。部署过程不执行任何驱动测试。

按当前 107 工作目录配置，部署位置为：

```text
/var/lib/dcu-tests/tools/HiTest/
├── current -> releases/<源文件内容标识>
├── releases/<源文件内容标识>/
└── logs/    # 跨版本保留的测试日志
```

目标机部署成功后使用：

```bash
/var/lib/dcu-tests/tools/HiTest/current/hitest list
/var/lib/dcu-tests/tools/HiTest/current/hitest nvidia-load-unload --check
# 在空闲的 NVIDIA 测试机上实际执行；107 若无 NVIDIA 显卡，前置检查会拒绝。
/var/lib/dcu-tests/tools/HiTest/current/hitest nvidia-load-unload 3 0
```

本机 `logs/deploy/<时间-标识>/deployment-result.json` 保存部署包位置、校验值和真实成功/失败结果，成功部署会记入平台操作记录。当前助手环境创建 SSH socket 被禁止（Operation not permitted），所以尚未实际复制到 107；本机终端执行上面的部署命令可继续。所有工程测试使用临时模拟模块、GPU 命令和本地暂存目录，不操作真实显卡，不代替远程机器验收。
