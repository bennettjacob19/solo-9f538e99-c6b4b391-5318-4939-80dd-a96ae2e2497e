# 虚拟盘零块回收演示（Flask + raw 稀疏镜像 + fallocate 打洞）

虚拟磁盘里**写成零的块仍然占着宿主盘空间**：文件系统不会因为内容全零就自动释放它。
本项目提供一个网页，预置一张存于数据卷的 raw 镜像（内含稀疏空洞、写过的零块、非零块），
可以：

- 模拟虚拟机按 **4 KiB 块** 读写；
- 一键发起/中断零块回收，实时查看镜像文件的**逻辑容量**与**宿主实占字节数**；
- 观察回收只给“已写入且全零的整块”打洞（`fallocate(PUNCH_HOLE)`），
  **不改变盘容量、不改变任何块的读值**。

## 原理与关键保证

| 机制 | 实现 |
| --- | --- |
| 跳过已有空洞 | 扫描用 `SEEK_DATA`/`SEEK_HOLE` 定位已分配数据段，整段跳过空洞（含历史已打的洞） |
| 只回收整块零 | 段内只处理 4 KiB 对齐的整块；打洞前在块锁内重新校验“仍已分配且全零”，再 `fallocate(PUNCH_HOLE\|KEEP_SIZE)` |
| 容量/读值不变 | `KEEP_SIZE` 保持文件大小；打洞区域读回仍是零，非零块完全不触碰 |
| 读写不等扫描 | 块读写与回收在同一进程内通过**每块一把锁**互斥，锁只覆盖单个 4 KiB 操作，不做全局暂停 |
| 不抹新数据 | “判零 + 打洞”在块锁内原子完成；判零后到达的写入会让二次校验失败而跳过该块 |
| 中断可恢复 | 打洞定期 `fsync`；中断在块边界优雅停止。状态只存在于镜像文件本身，进程重启后镜像与已打的洞都保留，可随时重新发起，扫描自动跳过旧洞 |
| 容量相符才复用 | 已有盘容量与 `BLOCK_COUNT` 一致时原样打开，原盘内容与已打的洞全部保留 |
| 容量不符即停启 | 盘上容量与配置容量一并报告，进程以非零码退出，**不覆盖、不截断**原盘 |
| 原子建盘 | 预置布局先写临时文件 `<镜像>.build.<pid>`，`fsync` 后 `rename` 到正式路径：布局完整前正式镜像不存在，建到一半被强杀也不会把半盘当好盘；重启自动清理残留临时文件后重建 |
| 多进程串行建盘 | 建盘前对 `<镜像>.lock` 取排他 `flock`：空卷上同时最多一个进程建盘，卷上预置数据最多占一份；其余进程阻塞等待，布局完整后直接复用 |

> 说明：本项目用宿主文件系统的稀疏文件模拟虚拟磁盘，`st_blocks*512` 即宿主实占字节，
> 与真实虚拟化场景中虚拟盘文件占用宿主空间的语义一致。要求底层文件系统支持
> `fallocate` 打洞（ext4/xfs 及基于 ext4 的 overlay2、docker 命名卷均支持；
> 个别网络文件系统不支持，启动时会报错）。

## 目录结构

```
app/
  constants.py       常量（4KiB、SEEK_DATA/HOLE、fallocate 标志、块状态）
  disk.py            DiskImage：块读写、块状态、SEEK_DATA 扫描 + 打洞回收；
                     create_image：flock 串行 + 原子 rename 的启动建盘流程
  reclaim.py         ReclaimManager：后台线程回收、取消、进度/实占统计
  api.py             与框架无关的 API 核心（Flask 与 stdlib 入口共用）
  web.py             Flask 路由与应用工厂
  server_stdlib.py   无 Flask 时的纯标准库 WSGI 回退入口（行为与 Flask 路由一致）
  cli.py             init-image / serve 命令
  static/            前端页面（块网格、实占卡片、回收控制、块读写、事件日志）
tests/test_disk.py   纯 stdlib 自测（19 项，含并发写入、中断重跑与启动流程）
Dockerfile, docker/entrypoint.sh, docker-compose.yml, requirements.txt
```

## Docker Compose 启动（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

- 镜像文件固定在命名卷 `disk-data` 挂载的 `/data/disk.raw`，容器重建不丢盘、不丢洞；
- 环境变量 `BLOCK_COUNT`（默认 256，即 1 MiB 镜像）可在 `docker-compose.yml` 调整；
- 容器启动时先初始化镜像再以 gunicorn 单进程 8 线程运行（回收在后台线程，
  块操作走块级锁）。初始化规则：
  - **已有盘容量与 `BLOCK_COUNT` 相符** → 原样复用，原盘内容与已打的洞保留；
  - **容量不符**（例如运维改大了 `BLOCK_COUNT`）→ 日志报告盘上容量与配置容量，
    容器以非零码停止启动，**原盘不被覆盖**；修正 `BLOCK_COUNT` 或更换卷后即可启动；
  - **空卷 / 上次建盘被强杀** → 先清理残留临时文件，再在锁保护下原子建盘；
    多容器共用空卷同时启动也只建一份，预置布局完整后网页才可读写或回收。

停止与清理：

```bash
docker compose down            # 保留数据卷
docker compose down -v         # 连同镜像卷一起删除（下次启动重新预置）
```

## 本地直接运行（无 Docker 时）

```bash
# 有 Flask 时
pip install -r requirements.txt
IMAGE_PATH=./data/disk.raw gunicorn --workers 1 --threads 8 'app.web:create_app()'

# 没有 Flask 也可以跑（纯标准库回退入口，API 完全相同）
IMAGE_PATH=./data/disk.raw python -m app.server_stdlib
```

## 预置镜像布局（256 块 × 4 KiB = 1 MiB）

| 块区间 | 类型 | 是否占宿主空间 | 回收行为 |
| --- | --- | --- | --- |
| 0–63 | 稀疏空洞（`ftruncate`） | 否 | 扫描跳过 |
| 64–127 | 写过的全零块 | 是 | 打洞释放 |
| 128–191 | 非零块（含块号标记） | 是 | 判非零跳过 |
| 192–255 | 稀疏空洞 | 否 | 扫描跳过 |

初始实占 524288 B（128 块）；完整回收后实占 262144 B（仅剩 64 个非零块），容量始终 1048576 B。

## HTTP API

| 方法/路径 | 说明 |
| --- | --- |
| `GET /api/status` | 容量/实占、每块状态、回收任务进度 |
| `GET /api/blocks/<i>` | 查看块状态、hex 全文与 ASCII 预览 |
| `POST /api/blocks/<i>/read` | 模拟虚拟机读（只读，不改任何状态） |
| `POST /api/blocks/<i>/write` | 写 4 KiB，body：`{"hex": "..."}`（必须恰好 8192 个 hex 字符） |
| `POST /api/blocks/<i>/write-pattern/zero|data` | 便捷写全零 / 非零图案 |
| `POST /api/reclaim/start` | 发起回收（已在运行则返回当前状态） |
| `POST /api/reclaim/cancel` | 请求中断，当前块处理完即停，已打的洞保留 |

## 自测

```bash
python3 -m unittest discover -s tests -v
```

覆盖：预置布局与实占统计；完整回收（只打零块、容量/读值不变、二次回收空操作）；
确定性的“判零后写入”竞争、4 线程高压并发写入零数据不丢；中断后重开镜像验证洞落盘、
重启继续回收；启动流程（容量不符报错停启且原盘内容与空洞不动、强杀后清残留重建、
多线程/多进程空卷串行建盘只有一份、持锁期间正式镜像不出现、CLI 退出码）；
API 输入校验与 WSGI 路由。

## 已知边界

- 打洞按 4 KiB 对齐；若数据段首尾不足 4 KiB 对齐（本项目镜像与写入都按 4 KiB 对齐，不会出现），零碎部分保留不动。
- 回收任务状态保存在进程内存（进度计数），持久状态只依赖镜像文件；进程重启后进度计数归零，但盘上已回收的空间真实保留。
- 数据卷上除 `disk.raw` 外还有两个辅助文件：`disk.raw.lock`（建盘串行锁，空文件，保留不删）
  与 `disk.raw.build.<pid>`（建盘临时文件，正常只存在于一瞬间；被强杀会残留，下次启动持锁清理）。
- 当前环境未安装 Docker，compose 配置未经本机实际启动验证；启动流程（init-image → serve）
  与核心逻辑、HTTP 行为已通过纯标准库入口完整自测。
