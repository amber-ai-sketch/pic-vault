# PicVault 操作手册

# WORKFLOW.md — 逐步操作手册

> 配套 `PLAN.md` 使用。本文档讲"怎么做"，PLAN 讲"为什么"。

## 目录

- [一次性设置](#一次性设置)
- [首次启动：SSD 迁移存量数据](#首次启动ssd-迁移存量数据)
- [日常流水线](#日常流水线)
- [特殊任务](#特殊任务)
- [故障排查](#故障排查)

---

## 一次性设置

### 1. 检查前置条件

```bash
# macOS 版本 ≥ Ventura（自带 Photos Duplicates 相簿）
sw_vers -productVersion

# 命令行工具自带 sips, osascript, rsync, diskutil, mdls
which sips osascript rsync diskutil mdls

# Python 3.9+
python3 --version

# 第三方依赖
python3 -c "import PIL, hashlib; print('PIL OK')"
python3 -c "import flask; print('Flask OK')" || pip3 install --user Pillow Flask
which ffmpeg || brew install ffmpeg

# 三个盘都挂载
ls -d /Volumes/Storage /Volumes/WD4T/MediaVault /Volumes/YM/MediaVault
```

### 2. 安装项目

把整个项目目录放到 `~/code/PicVault/`（或任意位置）。脚本里的路径都用绝对路径，不依赖安装位置。

### 3. 配置参考

`outputs/config.example.yaml` 只作字段/路径参考，CLI 不直接读取这个 YAML。

如果要改默认工作盘，直接用 `WORK` 或 `PICVAULT_WORK`；日常命令仍以 `--work` / `--backup` 为准。

### 4. 初始化工作盘

```bash
./scripts/init_storage.sh
# 默认在 /Volumes/Storage/ 创建顶层目录骨架：
#   inbox/ by-date/ screenshots/ screenrecords/ docs/ things/ _favorite/ _vlogs/ _trash/ _meta/
# 可重跑，幂等
```

---

## 首次启动：SSD 迁移存量数据

**一次性，把 `/Volumes/WD4T/MediaVault/` 里的现有数据搬到新结构。**

### 步骤 0：准备 SSD 备份

```bash
# 挂载 SSD，确认路径
ls -d /Volumes/YM/MediaVault

# 创建备份目录并放数据
mkdir -p /Volumes/YM/MediaVault/_pre_migration_backup

# 方式 A：APFS clone（零空间，推荐）
cp -c -r /Volumes/WD4T/MediaVault/* /Volumes/YM/MediaVault/_pre_migration_backup/

# 方式 B：全量复制
cp -r /Volumes/WD4T/MediaVault/* /Volumes/YM/MediaVault/_pre_migration_backup/

# 方式 C：rsync（带进度）
rsync -avh --progress /Volumes/WD4T/MediaVault/ /Volumes/YM/MediaVault/_pre_migration_backup/
```

**重要**：用户自己决定要不要排除某些文件/文件夹；脚本不会自动判断。

### 步骤 1：脚本 stage + init

```bash
./scripts/onboard_migrate.sh --dry-run
# 看完输出确认无误

./scripts/onboard_migrate.sh
# 自动：
#   - 验证 _pre_migration_backup 存在且非空
#   - 在 working/ 创建目录骨架
#   - 把 _pre_migration_backup 内容复制到 working/inbox/
#   - 验证 _pre_migration_backup sha256 不变
```

### 步骤 2：跑流水线

```bash
# 去重
./scripts/dedupe.py --work /Volumes/YM/MediaVault/working

# 重命名归档
./scripts/rename_organize.py --work /Volumes/YM/MediaVault/working

# 加主题（如已知历史主题）
./scripts/add_theme.py --work /Volumes/YM/MediaVault/working --interactive

# 按主题同步（不要再跑 rename；rename 只处理 inbox）
picvault theme rebucket --theme <主题名> --yes
# 或：./scripts/rename_organize.py --work … --rebucket-themes --theme <主题名>
```

### 步骤 3：rsync 回 WD4T

```bash
./scripts/sync_to_backup.sh \
  --work   /Volumes/YM/MediaVault/working \
  --backup /Volumes/WD4T/MediaVault

# 默认 append-only；不会删除 WD4T 上 _meta/ / inbox/ 等
```

### 步骤 4：校验

```bash
./scripts/sync_to_backup.sh --verify \
  --work   /Volumes/YM/MediaVault/working \
  --backup /Volumes/WD4T/MediaVault
```

期望输出"Verification passed: size/mtime match"。

### 步骤 5：人工收尾

1. 在 Finder 里打开 `/Volumes/WD4T/MediaVault/by-date/` 检查归档结果
2. 验证 OK 后：
   - 删 SSD 的 `working/`（结果已镜像到 WD4T）
   - **保留 SSD 的 `_pre_migration_backup/` ≥30 天**（回滚保险）

---

## 日常流水线

**每次有新素材要处理时跑一次。**

### 1. 设备 → inbox 手动拖拽

Finder 拖到 `/Volumes/Storage/inbox/`。子目录结构任意：

- 单文件：直接拖
- 整张 SD 卡的 DCIM：拖整个 `DCIM` 文件夹
- 用户自命名子文件夹：随意

### 2. （可选）混合源场景放 `.source` 旁路

如果 inbox 里同一个文件夹混了多台相机的照片，用 `.source` 标记子目录：

```
inbox/
├── 2026-07-15-iPhone/
│   ├── IMG_0001.heic
│   └── .source             ← 内容 "iphone"
└── 2026-07-15-Canon/
    ├── IMG_0001.CR2
    └── .source             ← 内容 "canon"
```

`.source` 是个纯文本文件，写 source 名字（一行）。

### 3. 去重

```bash
./scripts/dedupe.py --dry-run
# 先看报告：会识别多少对重复、保留哪些、丢弃哪些

./scripts/dedupe.py
# 实际执行：副本移入 _trash/，保留的移到 by-date/<year>/<month>/<photos|videos>/
```

**重要**：dedupe 不会自动加 source。源识别失败的文件会用 _trash 但 source 段省略。

### 4. 重命名 + 归档

```bash
./scripts/rename_organize.py
# 读 _meta/events.yaml 的主题配置
# 1) 扫 inbox/：重命名 + 分桶（可为空）
# 2) 不再自动全量同步主题（避免冲掉各主题桶手工调整）
#    主题生效：picvault theme rebucket --theme <名> [--yes]
# 截图/录屏检测（v7）：
#   0. 已知相机文件名（IMG_/VID_/DJI_/…）→ by-date/（normal）
#   图片：含 screenshot → screenshots/；无 GPS → screenshots/
#   视频：含 record → screenrecords/；（无 Make 或 无 GPS）→ screenrecords/
#   非图片/视频（如 HTML、PDF、TXT）→ 跳过，留在 inbox/
# 其他照片 → by-date/<YYYY-MM>/photos/；视频 → videos/
# 主题文件进 2026-MM_<theme>/，无主题文件进 2026-MM/
```

**截图 / 录屏分两桶**（均不分月份/主题）：

| 类型 | 目录 | 命名前缀 |
|---|---|---|
| 截图（图） | `/Volumes/Storage/screenshots/` | `screenshot_<date>_<source?>_<hash>.ext` |
| 录屏（视频） | `/Volumes/Storage/screenrecords/` | `screenrecorder_<date>_<source?>_<hash>.ext` |
| 文档（手动） | `/Volumes/Storage/docs/` | `doc_<date>_<source?>_<hash>.ext`（Web 勾选「移至文档」，不自动分类）|
| 物品（手动） | `/Volumes/Storage/things/` | `things_<date>_<source?>_<hash>.ext`（Web 勾选「移至物品」，不自动分类）|

> **示例**：
> - `Screenshot_….png` → 关键字 → `screenshots/`
> - 无 GPS 的微信/小红书 JPG（非相机名）→ `screenshots/`
> - Android `Screenrecorder_….mp4` → 含 `record` → `screenrecords/`
> - iOS `RPReplay_Final_….mov`（无 Make/GPS）→ `screenrecords/`
> - `VID_….mp4` / `IMG_….HEIC`（相机文件名白名单）→ `by-date/`
> - `.html` / `.pdf` / `.txt` 等非图片、非视频 → 不处理，留在 `inbox/`
> - **Live Photo**：同名 `IMG_xxxx.HEIC` + `IMG_xxxx.MOV`（同目录，或 inbox 内跨文件夹但 stem 全局唯一）→ 成对进 `by-date/.../photos/`，共用 stem `<date>_<source>_live_<hash>.{heic,mov}`（无 source 则为 `<date>_live_<hash>`）；跨目录配对日志为 `[live-pair-cross]`。同名多份有歧义则跳过并告警。Web 画廊只显示静图并标 Live。单边仍按普通照片/视频规则。
> - 手机拍的证件/票据 → 整理进 by-date 后，在 Web 勾选「移至文档」
> - 物品照片/视频 → 整理进 by-date 后，在 Web 勾选「移至物品」

幂等：可重复跑 rename（inbox）。主题请按主题同步；全量 `--all` 会按配置收敛所有主题桶。存量误分可用 Web 纠错（按当前页只显示可去的目标）：
- 普通分类页：移至截图录屏 / 移至文档 / 移至物品
- 主题桶页：放回默认月桶 / 移至截图录屏 / 移至文档 / 移至物品
- 截图、录屏页：移回普通分类 / 移至文档 / 移至物品
- 文档页：移至截图录屏 / 移回普通分类 / 移至物品
- 物品页：移至截图录屏 / 移回普通分类 / 移至文档

### 5. 添加主题（按需）

```bash
./scripts/add_theme.py --interactive
# 提示输入：主题名 / 月份 / 日期范围 / 来源设备 / 显式文件列表
# 写入 _meta/events.yaml

picvault theme rebucket --theme <主题名> --yes   # 让该主题配置生效到 by-date
```

### 6. Web 浏览 + 加星

```bash
./scripts/web_browse.py --host 0.0.0.0 --port 8765 &
# 浏览器开 http://mac-mini.local:8765/
# 或用 dashboard「04 Web 浏览」打开

# 首页是图库文件夹总览；按年月分类入口显示为「年月」（/by-date）。
# 在年月、月份/主题、截图、录屏页面：
#   · 点 ★ 加星 → _meta/stars/<bucket>.json
#   · 勾选文件 → 按当前页显示可用目标（主题桶可「放回默认月桶」；另有移至截图录屏 / 移回普通分类 / 移至文档 / 移至物品）
#   · 「移至回收站」→ _trash/<批次>/…（软删除，可找回；不进备份盘）
```

导航含 **年月**（`/by-date`）、**加星**（`/starred` 汇总全部 ★）、**截图**、**录屏**、**文档**、**物品**；首页只显示回收站计数，不提供回收站浏览页。`web_browse.py` 后台跑着就行。

### 7. 导出精选到 iPhone

```bash
# 带主题
./scripts/pick_to_iphone.py --bucket 2026-07_海南
# 刷新 _favorite/2026-07_海南/，生成 favorite-2026-07_海南.scpt

# 无主题
./scripts/pick_to_iphone.py --bucket 2026-08
# 刷新 _favorite/2026-08/，生成 favorite-2026-08.scpt（Photos 相簿名仍为 Picks）
```

### 8. 跑 AppleScript

```bash
osascript /Volumes/Storage/_meta/scripts/favorite-2026-07_海南.scpt
osascript /Volumes/Storage/_meta/scripts/favorite-2026-08.scpt
```

AppleScript 自动打开 Photos.app、建相簿、导入、打星标。

### 9. Photos Duplicates 合并（人工，必做）

1. macOS Photos.app 打开
2. 左侧栏 → **Duplicates**（macOS Ventura+ 直接可见）
3. 每组右上角 **Merge** 按钮逐组合并
4. **不能跳**，否则 iCloud 上会有冗余

### 10. iCloud Photos 后台同步（自动）

跑完 AppleScript 后，无需操作。iCloud 自己上传/推送，几分钟到几小时。

### 11. 同步到 WD4T 备份盘

```bash
./scripts/sync_to_backup.sh
./scripts/sync_to_backup.sh --verify
```

期望"Verification passed: size/mtime match"。

### 12. 人工收尾

按顺序：

```bash
# （人工）iMovie 剪辑 vlog，导出到 /Volumes/Storage/_vlogs/<theme>.mp4
# 跑一次 sync_to_backup.sh 把新 vlog 同步到 WD4T

# （人工）SD 卡格式化（在 macOS 磁盘工具里）

# （人工）工作盘清空
# 确认 _meta/logs/sync-*.log 最新一次成功后，可以删：
#   - /Volumes/Storage/inbox/
#   - /Volumes/Storage/_trash/ 里超过 30 天的文件
```

---

## 特殊任务

### 制作 Vlog

```bash
# 推荐路径：用 iMovie 或 Final Cut 编辑
# 编辑完导出 mp4 到 /Volumes/Storage/_vlogs/<theme>.mp4
# 跑一次 sync_to_backup.sh 同步到 WD4T

# 可选路径：用 make_vlog.py
# 1. 写 EDL JSON
cat > /Volumes/Storage/_meta/edl/2026-07_海南.json << 'EOF'
{
  "theme": "2026-07_海南",
  "clips": [
    {"path": "by-date/2026/2026-07_海南/videos/20260710_140012_dji-nano_7c1e.mp4", "in": 0, "out": 15.2},
    {"path": "by-date/2026/2026-07_海南/videos/20260710_140145_dji-nano_3a8b.mp4", "in": 2, "out": 20.0}
  ],
  "transition": "crossfade-1s",
  "music": null
}
EOF

# 2. 渲染
./scripts/make_vlog.py --work /Volumes/Storage --theme 2026-07_海南

# 3. 输出到 _vlogs/2026-07_海南.mp4

# 4. sync
./scripts/sync_to_backup.sh
```

### 调整主题

在 Web 打开 `/themes` 可直接编辑 `_meta/events.yaml`（保存前校验；原文件备份为 `events.yaml.bak`）。也可本机编辑。

**保存 ≠ 搬家**。推荐**逐个主题**生效：只扫该主题相关默认月桶与主题桶，**不改写**其它主题桶里已有文件；本主题内不再匹配的文件会退回默认月，或**改派到其它仍匹配的主题**。

| 步骤 | 做什么 |
|---|---|
| 1 | `/themes` 改某个主题的 `date_range` / `sources` 等 → **保存** |
| 2 | 该行点「复制同步命令」→ 先 dry-run，确认后再 `--yes` |
| 3 | 需要时再改下一个主题并单独同步 |

```bash
# 只同步「香港-深圳」（推荐）
picvault theme rebucket --theme 香港-深圳          # dry-run
picvault theme rebucket --theme 香港-深圳 --yes    # apply

# 或：
./scripts/rename_organize.py --rebucket-themes --theme 香港-深圳 --dry-run
./scripts/rename_organize.py --rebucket-themes --theme 香港-深圳

# 全量同步所有主题（会覆盖各桶手工调整；慎用）
picvault theme rebucket --all --yes
```

| 改动（对该主题同步后） | 行为 |
|---|---|
| **扩大** date_range | 区间覆盖的各默认月桶里新命中的 → **开始月**主题桶 |
| **缩小** date_range | 该主题桶里不再匹配的 → 按拍摄日所在月的默认月桶 |
| **改名 / 删主题后全量** | 旧桶在 `--all` 时才会被清扫；单主题同步只碰你点名的主题 |

`date_range` **允许跨月**（如 `2025-12-28`–`2026-01-05`）。主题桶路径固定为 `by-date/<开始年>/<开始月>_<主题名>/`；`month` 可由 `start` 推导（可省略），手写则必须与开始月一致。Web「放回默认月桶」仍按**文件拍摄日**所在月。

`picvault rename` **只处理 inbox**（首次归档仍可按配置进主题桶），**不再**自动全量同步主题。主题桶页「放回默认月桶」仍可做精细纠错。

### 清理 `_trash/`

```bash
# 查看 30 天前的副本
find /Volumes/Storage/_trash -type f -mtime +30

# 人工确认后删除
find /Volumes/Storage/_trash -type f -mtime +30 -delete

# 同步清空 WD4T 上的对应 _trash/（如果有的话，目前 rsync 不带 _trash/）
```

### 月度 SMART 检查

```bash
diskutil info /Volumes/WD4T | grep -E "Volume Name|Total Size|Used Space|SMART Status"
# 期望 SMART Status: Verified
```

---

## 故障排查

### 脚本拒绝执行 "路径不在白名单内"

```
ERROR: --work /Users/foo is not in path whitelist
  Allowed: /Volumes/Storage, /Volumes/WD4T/MediaVault, /Volumes/YM/MediaVault, /Users/ym/Downloads/pic-test
```

解决：传正确的 `--work` / `--backup` 参数；`/Users/ym/Downloads/pic-test` 仅用于自测。

### dedupe 没识别重复

检查：
- 文件是否真的字节相同？`shasum -a 256 file1 file2` 对比
- 文件是否在不同子目录里？dedupe 只在同一次跑里比，跨批不比对
- 文件是否被 read-only 锁定？

### rename 报"目标已存在"

文件重名了（极小概率同秒同 hash）。脚本会自动追加 `_1` `_2` 等。如果还在报错：
- 检查 `by-date/<year>/<month>/<source>/<photos|videos>/` 里有没有同名的
- 可能是上次未完成留下的，删了再跑

### AppleScript 报错

```
error "Photos got an error: ..." number -1
```

常见原因：
- Photos.app 没安装/没打开 → `open -a Photos` 后重试
- iCloud 同步未完成 → 等 iCloud 上传完再跑
- 相簿已存在且被锁定 → 重启 Photos.app
- macOS 版本太老 → 手动执行 `.scpt` 内容

### iCloud 同步慢

- 看 macOS 顶部菜单栏 iCloud 图标状态
- 看 系统设置 → Apple ID → iCloud → iCloud 存储 剩余空间
- 必要时清理 iCloud 上无关照片腾空间

### 第一次跑很慢

- SHA-256 计算 4T 数据可能要几小时
- 不要中断；如果中断，dedupe 是原子的，重跑继续
- 加 `--batch <id>` 可以只处理一批

### 找不到 AppleScript 生成的相簿

- 打开 Photos.app，等几秒让相簿出现
- 确认 AppleScript 跑了（看 `_meta/logs/` 日志）
- 看 macOS Photos 通知中心有没有 import 通知

---

## 速查

| 任务 | 命令 |
|---|---|
| 看全部脚本 | `ls scripts/` |
| 看主题配置 | `cat /Volumes/Storage/_meta/events.yaml` |
| 看加星状态 | `ls /Volumes/Storage/_meta/stars/` |
| 看 sha256 清单 | `ls /Volumes/Storage/_meta/checksums/` |
| 看缩略图缓存 | `ls /Volumes/Storage/_meta/thumbs/` |
| 看 AppleScript 模板 | `ls /Volumes/Storage/_meta/scripts/` |
| 看运行日志 | `tail -f /Volumes/Storage/_meta/logs/*.log` |
| 看 WD4T 状态 | `diskutil info /Volumes/WD4T` |
| 看 iCloud 状态 | 系统设置 → Apple ID → iCloud |
