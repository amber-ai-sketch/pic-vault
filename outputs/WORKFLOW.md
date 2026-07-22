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
ls -d /Volumes/Storage /Volumes/WD4T /Volumes/YM
```

### 2. 安装项目

把整个项目目录放到 `~/code/PicVault/`（或任意位置）。脚本里的路径都用绝对路径，不依赖安装位置。

### 3. 配置

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml   # 检查路径是否一致
```

### 4. 初始化工作盘

```bash
./scripts/init_storage.sh
# 默认在 /Volumes/Storage/ 创建顶层目录骨架
# 可重跑，幂等
```

---

## 首次启动：SSD 迁移存量数据

**一次性，把 `/Volumes/WD4T/MediaVault/` 里的现有数据搬到新结构。**

### 步骤 0：准备 SSD 备份

```bash
# 挂载 SSD，确认路径
ls /Volumes/YM

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

# 重命名再跑一次（让已分桶的文件搬到主题桶）
./scripts/rename_organize.py --work /Volumes/YM/MediaVault/working
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

期望输出"全部 sha256 一致"。

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
# 截图/录屏检测（v4）：
#   规则 1: 文件名匹配任一关键字（大小写不敏感子串）
#          screenshot / screenrecording / screen recording / screenrecord
#          / screenrecorder / screencapture / screen capture / rpreplay
#   规则 2: 无 EXIF GPS + 无相机标识（图片视频通用）
# 其他照片 → by-date/<YYYY-MM>/photos/；视频 → videos/
# 主题文件进 2026-MM_<theme>/，无主题文件进 2026-MM/
```

**截图/录屏去哪**：满足以下任一条件即归到根目录 `/Volumes/Storage/screenshots/`（不分月份/主题）：
- 文件名匹配任一关键字（覆盖 macOS 录屏 `Screen Recording`、iOS 录屏 `RPReplay_Final_*`、Android 录屏 `Screenrecorder_*` / `Screenrecord_*` 等）
- 无 EXIF GPS + 无镜头厂商信息

保留 `screenshot_` 前缀命名：`screenshot_20260715_183022_iphone_a3f2.png`（图片和录屏都用此前缀）。

> **示例**：
> - iOS 录屏 `RPReplay_Final_1689496200.mov` → 命中关键字 `rpreplay` → `screenshots/`
> - Android 录屏 `Screenrecorder_20260715_140012.mp4` → 命中关键字 `screenrecorder` → `screenshots/`
> - iPhone 默认截图 `IMG_XXXX.PNG`（无 EXIF）→ 规则 2 → `screenshots/`
> - iPhone 默认照片 `IMG_0001.HEIC`（有 Apple Make）→ 不满足任何规则 → `by-date/photos/`
> - 佳能单反 `IMG_4521.CR2`（有 Canon Make，无 GPS）→ 不满足规则 2 → `by-date/photos/`

幂等：可重复跑，已分桶的文件会被搬到主题桶。

### 5. 添加主题（按需）

```bash
./scripts/add_theme.py --interactive
# 提示输入：主题名 / 月份 / 日期范围 / 来源设备 / 显式文件列表
# 写入 _meta/events.yaml

./scripts/rename_organize.py    # 再跑一次让新主题生效
```

### 6. Web 浏览 + 加星

```bash
./scripts/web_browse.py --host 0.0.0.0 --port 8765 &
# 浏览器开 http://mac-mini.local:8765/

# 在月份/主题页面点 ★ 加星
# 星标写入 _meta/stars/<bucket>.json
```

`web_browse.py` 后台跑着就行，随时打开浏览器看。

### 7. 导出精选到 iPhone

```bash
# 带主题
./scripts/pick_to_iphone.py --bucket 2026-07_海南
# 复制到 _favorite/2026-07_海南/，生成 favorite-2026-07_海南.scpt

# 无主题
./scripts/pick_to_iphone.py --bucket 2026-08
# 复制到 _favorite/ 根，生成 favorite-2026-08.scpt
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

期望"全部 sha256 一致"。

### 12. 人工收尾

按顺序：

```bash
# （人工）iMovie 剪辑 vlog，导出到 /Volumes/Storage/_vlogs/<theme>.mp4
# 跑一次 sync_to_backup.sh 把新 vlog 同步到 WD4T

# （人工）SD 卡格式化（在 macOS 磁盘工具里）

# （人工）工作盘清空
# 确认 _meta/logs/sync-verify.log 最新一次成功后，可以删：
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

```bash
$EDITOR /Volumes/Storage/_meta/events.yaml
./scripts/rename_organize.py    # 应用新配置
```

`events.yaml` 支持热改。

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
  Allowed: /Volumes/Storage, /Volumes/WD4T/MediaVault, /Volumes/YM
```

解决：传正确的 `--work` / `--backup` / `--migration-ssd` 参数，或改 `config.yaml` 的 paths 配置。

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

