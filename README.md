# eNodeDone · 多机任务工作台

本地服务使用 Windows 和 Python 3.11+。远端需要 Linux、Python 3.8+、bash、scp/ssh。通用远端脚本只用 Python 标准库，训练容器另需 FSDPTurbo 运行环境及 PyYAML。

```powershell
python -m pip install -r requirements.txt
./start.ps1
# 如果旧服务仍占用 8765，可使用另一端口：
./start.ps1 --port 8766
```

打开服务打印的本地地址。修改执行器后需要重启服务；页面会拒绝连接旧版本执行器。服务启动、打开页面、创建/保存任务均不会连接远端或执行脚本，点击“建立连接”会连接远端；点击“运行任务”会自动补齐连接，再启动 pipeline。

## 任务、机器和脚本

- 新建任务可选训练、推理、后训练及自定义模板；空白任务固定放在最后，只有空白任务保留前置图形。镜像分发不再显示为新建入口。
- 默认任务名为 `模板名任务YYYYMMDDTHHMMSSZ`，空白模板为 `任务YYYYMMDDTHHMMSSZ`，使用 UTC；切换模板时更新默认名称，保留手工输入的名称。创建时输入正整数机器数，同时生成独立 CSV。Master 工作目录默认是 `/mnt/share/enodedone`，可在创建时编辑。
- 右上角“删除任务”经确认后将本地任务和记录移入 `data/.trash`，可用“导入任务”打开其中的 task.json 恢复；外部导入文件只从任务库移除，原始文件保留。运行、建连或 Console 命令执行时不能删除。
- CSV 字段为 `Machine,Info,User,Pwd,Connected`，兼容原四列清单。Connected 只表示本地服务持有的真实 SSH 会话：Y 为已建立，N 为未连接。服务首次载入任务时会清除旧进程留下的 Y；主动关闭、收到 SSH 底层连接退出事件也会更新为 N。远端 SSH/SCP 命令报错不会修改它。选择外部 CSV 会生成任务内的独立副本；密码保留且不传给页面。
- 集群配置中的“机器数”限定任务使用 CSV 前 N 行，CSV 内容保持原样。数量超过行数时使用现有全部行。原 Master 超出范围时会清空，重新选择后可启动任务。
- 顶部“建立连接”并行建立各机持久 SSH 会话，全部连接时禁用；“关闭连接”关闭任务的会话，全部未连接时禁用。运行任务前自动等待连接就绪，连接失败不会执行 pipeline。SSH 状态矩阵采用与 pipeline 相同的分组，点击方块查看 IP 和状态说明。
- 通过 Paramiko Transport 读取线程退出事件接收 EOF、SSH_DISCONNECT 或 socket 错误，不执行心跳命令、不开探测 channel、不因命令慢而关闭连接；真实断线后显示红色、弹出通知并更新 CSV。无数据包返回的网络黑洞仍需等待底层 TCP 判定，不承诺立即发现。连接保持到主动关闭或本地服务退出，任务结束/停止不会关闭连接。界面关闭后本地服务仍管理会话。
- 公钥准备已整合到框架，需要时复用/修复公钥和节点互信，仅认证失败时使用 CSV 的 Pwd。User 默认 root；使用目标行 User；保持 known_hosts 校验，不覆盖已有指纹或其他公钥，私钥留在各自机器上。兼容旧任务的 nodes_ssh 节点，但无需再放入 pipeline。
- 每个节点可编辑范围、脚本来源 IP/路径、Shell/Python 内容及逐行 KV。推送阶段把本地保存的 pipeline_scripts 下脚本副本写到 master；其他来源或路径使用原远端文件。自定义脚本会真实执行。
- 镜像分发节点的 `IMAGE_PATH`、`IMAGE_DEST` 分别指定 Master 源路径与各机目标路径。使用分发节点时，两个值展开后都必须是远程绝对路径；无需分发的任务可省略该节点。导入旧任务时原路径自动转入分发节点的 KV。
- KV 支持 `${MASTER_DIR}`、`${MASTER_IP}`、`${TASK_DIR}`、`${RUN_ID}`、`${WARMUP_STEPS}`；运行时提供 NODE_RANK、NNODES、MASTER_ADDR 和 ENODEDONE_CONTEXT。
- 节点串行，同一节点内各机并行，全部完成后才启动下一节点。非零返回、SSH 错误、超时均失败并停止后续节点。停止会终止本次登记的远程进程组；服务重启后的旧运行需人工核实远端状态。
- 同一任务跨次运行复用本机到各节点的 SSH 连接，按需打开命令通道；持久连接使用 Paramiko，支持 SSH config 的 ProxyCommand/ProxyJump。命令开始执行后不会因连接错误自动重试。远端 ssh/scp 使用短控制路径 `/tmp/ed<UID>/<运行ID的16位哈希>/%C`，为 `%C` 展开和 OpenSSH 随机后缀预留空间；目录校验属主并设为 0700。接收端使用自己的 UID 生成路径。此路径只保存 SSH 控制套接字，与配置中的镜像分发路径独立；空闲 60 秒退出，不关闭主机密钥校验。
- 新运行记录每个节点的 started_at、finished_at、elapsed_seconds，并在看板显示耗时；日志分别记录建连/会话复用、命令执行、SHA 检查、传输与应用耗时。耗时基于单调时钟，不将不同机器的 epoch 相减。
- Master Console 通过现有 SSH 会话打开 PTY 执行手动命令，保留原始分栏、空格及换行；时间戳按浏览器本地时间显示为 `[HH:mm:ss]`，无 UTC 后缀。输入回显只显示用户命令，不显示内部 cd/bash 包装或 SSH 耗时。每次执行保持串行；每条命令独立执行，不保留上条命令的 cd/export。Pipeline 日志放在独立的“运行日志”页签。
- 机器按 8 机横排为柜、8 柜竖排为组、8 组横排为群展示。群占满宽度，组可换行；方块 18–44px，群和组均显示 Rank 范围。

## 本地默认值

任务、机器 CSV 和运行记录保存在 `data/` 下。可在其中创建 `defaults.json`，为新任务设置 `master_dir`、`shared_dir` 和内置分发节点的 `IMAGE_PATH`、`IMAGE_DEST`。四项均可省略，填写时使用远端绝对路径。重启服务后读取这些默认值。

## 压缩包分发与功能验证

公钥准备和会话建立由框架负责，不作为 pipeline 节点。

1. 推送实际脚本到 master，内容一致的文件复用，不重复落盘。
2. 由本地执行器通过持久 SSH 会话并行查询 master 源 SHA256 和各机目标 SHA；缺失的 master 测试源创建为含 `test.txt`（内容为 `test`）的确定性 tar.gz。全部检查完成后才规划倍增拷贝，不预先 ping，也不让 master 再开 SSH 重查 SHA。测试包 SHA 相同的机器直接完成，全部命中时跳过整个远端分发/应用脚本；缺失或 SHA 不同的机器才重新分发。SHA 不同的目标在新文件校验通过、旧 SHA 再次复核后原子替换；目标变化则报错。接收端一次命令完成拉取、SHA 校验和原子发布；预先展示倍增路径，各分支按依赖就绪立即启动，不等待整轮。收到文件后即可继续转发，各机应用完成立即显示成功，下一 pipeline 节点仍等全体完成。测试包只提取并校验 test.txt，不调用 Docker。
3. 各机在任务的共享目录创建本次 run_id/rank 的唯一标记。master 验证全部标记后清理，失败时按机器并行清理本次运行的标记。
4. 每机按 DELAY_MIN_SECONDS / DELAY_MAX_SECONDS 随机延时（默认 1–5 秒，含上下限），采集 CPU 一分钟平均负载、采样时间和实际延时，等待所有机器完成。此延时属于测试内容，保留配置。
5. master 汇总各机 JSON 到 runs/<run_id>/nodes/<rank>/；自身文件直接本地复制，其他机器经复用 SSH 的 scp 获取。
6. master 生成 analysis.json、metrics.csv，调用通用可视化接口；SHOW_TABLE 默认 true，CHART_KIND 默认 bar，none 不画图。

可手动添加 master ping 调试节点，并配置 PING_COUNT / PING_TIMEOUT_SECONDS。该节点按机器并行。

机器连接信息来自任务 CSV，是否启动由用户决定。训练节点保留 NPU 空闲检查；测试任务没有训练节点，不使用 NPU。

通用镜像模板使用真实 docker load；成功后按归档 SHA 缓存镜像引用和 ID，下次同时核实当前引用/ID 一致才跳过 load，镜像删除或重标记会重新装载。文件 SHA 仍每次通过本地持久 SSH 严格检查；真实 Docker 即使文件命中也会确认装载状态，避免归档还在但 Docker 镜像已被删除时误跳过。8 机测试任务的 artifact_kind=archive 只走压缩包路径。含 `${RUN_ID}` 的目标路径每次不同；需要跨次复用时使用固定目标路径，框架不会擅自改写此配置。

## 训练模板

内置“训练”模板已提取已准备的 K31 任务公共逻辑，依次包含脚本推送、master 初始化、镜像检查分发与装载、共享代码目录检查、容器训练、log/profiling/snapshot 汇总和性能分析。公共脚本与基础 YAML 保存在 `training_template/`，新建时固化为任务内可编辑副本；不读取既有任务目录，也不自动覆盖已有任务。

代码默认放在 `${MASTER_DIR}/code/FSDPTurbo`，基础 YAML 放在 `${MASTER_DIR}/configs/train.yaml`，模型与数据目录分别为 `${MASTER_DIR}/assets/models`、`${MASTER_DIR}/assets/data`。创建时填写的 Master 工作目录会用于展开这些路径；机器 CSV 与 master IP 仍需独立配置，不复制原任务机器信息。初始化只在基础 YAML 缺失时生成模板，保留已有文件。

训练节点保留 K31 recipe 默认值：镜像 `fsdpturbo-kimi-k31:env-20261031`、相对入口 `examples/kimi_k31/train.py`、每机 16 进程、端口 6000、200 步。模型、数据、网卡、入口、并行配置需要匹配实际任务，`HCCL_SOCKET_IFNAME` 默认留空。`CONDA_PREFIX`、`CANN_ENV`、`IMAGE_ENTRYPOINT` 留空时继承镜像已有环境和入口；需要替换时在 KV 填绝对路径。`CONDA_SH` 非空时可通过 `CONDA_ENV` 显式激活环境。修改 `CODE_DIR`、`ENTRYPOINT` 时同步初始化与共享检查节点，修改 `CONFIG_PATH` 时同步初始化节点。

每机容器在同一进程链中初始化环境并启动 torchrun，采用只读的代码/配置/资源/驱动挂载和独立输出目录。`PROFILE_ENABLED`、`SNAPSHOT_ENABLED` 控制采集开关；细节在基础 YAML 中配置，收集包含日志、有效 YAML、启动元数据及采集目录，不批量复制 checkpoint。默认任务超时为 7200 秒。

分析节点的 `STATS_START_STEP=101`、`STATS_END_STEP=200` 定义稳定统计窗口，`REQUIRE_FULL_WINDOW=true` 时缺步报错；曲线仍展示全程指标。`LOG_POLICY=master` 适用于仅全局 rank 0 打日志；`slowest` 取各机共有 step 的最大耗时。`GLOBAL_BATCH_SIZE=auto` 从有效 YAML 推导标准非 packed 每步样本数，特殊输入可显式填写，samples/s 为每步样本数除以平均耗时。

## 节点绘图接口

页面常驻“数据可视化”区域，无展示内容时只显示标题。所有远程 Python/Shell 节点均自动获得 SDK，可视化不限定节点 operation 或 master。节点成功结束后，执行器通过 SSH 取回其发布的表格和图形；每次 plot() 调用生成一个独立页签，以 title 命名。创建和保存任务不会执行绘图或连接远端。

图形支持点位悬停提示；折线、散点、柱状、箱线等坐标轴图支持滚轮缩放、拖动平移、框选缩放、重置视图。图表工具栏可下载 PNG，双击图形可恢复坐标范围；饼图支持扇区信息和 PNG 下载。切换页签及运行状态刷新保留图形的当前视图；页签也支持方向键、Home/End 切换。Plotly.js 4.1.1 与中文资源在 web/vendor 本地打包，使用时无需访问 CDN；远端仍无需安装绘图库。

Python 节点：

```python
from enodedone_viz import plot

plot("/path/metrics.csv", show_table=True, kind="bar",
     x="machine", y="value", title="各机器负载")
# 只展示数据表：plot("/path/metrics.csv", True, None)
# 只绘图：plot("/path/metrics.csv", False, "bar", x="machine", y="value")
# 多序列：y=["loss", "grad_norm"]。
```

训练指标分别调用，可得到三个独立页签：

```python
for key, title in [("loss", "Loss"), ("grad_norm", "Grad norm"), ("seconds", "Step time (s)")]:
    plot("/path/training_metrics.json", False, "line", x="step", y=key, title=title)
```

训练模板的分析节点已按此方式发布三类全程指标，统计均值按配置的稳定步数窗口计算；日志中完全缺失的指标会打印说明并跳过，不生成虚构数据。SHOW_TABLE 控制每个页签是否同时展示原始指标表。

Shell 节点（SDK 命令自动加入 PATH）：

```sh
enodedone_viz.sh --data /path/metrics.csv --show-table true \
  --kind bar --x machine --y value --title '各机器负载'
# 只展示数据表：--show-table true --kind none
# 多序列时重复 --y，例如 --y loss --y grad_norm。
```

- kind / --kind：Python None、Shell none（不画图）、line（折线）、box（箱线，按 Y 列分布）、bar（柱状）、pie（饼图）、scatter（散点）。饼图只接收一列非负 Y，合计必须大于零。
- data_path / --data：节点执行机器上的数据文件路径，支持 JSON、JSONL、CSV、TSV，CSV/TSV 带表头，编码 UTF-8。自动识别记录数组、数值数组、单条记录、等长列数组、标签到数值的映射。例如 `{"A":2,"B":3}` 配合 x="label", y="value" 可绘饼图。
- show_table / --show-table：Python bool、Shell true/false，控制是否展示数据表，与数据文件结构无关。数据表展示所有原始字段，不限于 X/Y；仅表格模式支持纯文本数据，无需数值列。
- x / --x 可省略以使用序号；y / --y 可省略以选取数值列，或明确指定一个/多个字段。空数值按缺失点处理，NaN / Infinity、缺失字段等无效输入会报错。
- SDK 无第三方依赖，支持远端 Python 3.8+。Shell 默认使用 python3；执行器会设置 ENODEDONE_PYTHON 为实际解释器。

| show_table | kind | 展示内容 |
| --- | --- | --- |
| True | None | 仅数据表 |
| False | 图形类型 | 仅图形 |
| True | 图形类型 | 数据表与图形 |
| False | None | 不发布内容、不读取数据文件 |

执行器自动设置 ENODEDONE_VIZ_DIR，各节点与 Rank 使用独立目录。每项结果输出唯一 ID 的数据 JSON、元数据 JSON，需要绘图时再输出 SVG；通过标准输出事件通知执行器，请保留 SDK 的标准输出。pipeline 内使用默认输出目录。独立运行 SDK 时可用 output_dir / --output-dir 指定目录，或 `python enodedone_viz.py --help` 查看参数。下载后的文件保存在当前运行的 `visualizations/` 下，可脱离前端查看。

## 本地检查

```powershell
python -m unittest discover -s tests -v
node --check web/app.js
node --test tests/test_charts.mjs
```

单元测试使用隔离临时目录和内存传输替身，不建立真实 SSH 连接，不执行已准备的测试任务。运行数据与凭据 CSV 不应提交到版本库。
