# hwcloudpd — 华为云空间照片自动备份工具

一个把**华为云空间（[cloud.huawei.com](https://cloud.huawei.com/)）图库**里的照片和视频自动下载到本地硬盘或 NAS（群晖、飞牛 fnOS、Unraid、极空间、Linux 服务器等）的开源工具。

设计灵感与命令行体验完整参考自优秀的开源项目 [`icloud_photos_downloader (icloudpd)`](https://github.com/icloud-photos-downloader/icloud_photos_downloader) —— 如果你在电脑或 NAS 上用过 `icloudpd` 备份 iCloud 照片，那么上手 `hwcloudpd` 几乎零门槛。

---

## 主要功能

- **三种同步模式（对齐 `icloudpd`）**
  - **复制模式（Copy，默认）**：只下载云端新增的照片和视频，两边都不删，最安全省心。
  - **同步模式（Sync，`--auto-delete`）**：如果你在手机或华为云空间把某张照片移进了“最近删除”，本地对应文件也会一起删掉，保持两端一致。
  - **移动模式（Move，`--keep-cloud-recent-days` / `--delete-after-download`）**：照片在本地下载并校验无误后，自动把云端原图移入“最近删除”，帮你腾出华为云存储空间。
- **自动整理目录，默认按拍摄日期归档**
  - 默认备份除“最近删除”外的**全部相册**（包括相机、截屏录屏以及自定义相册）。
  - 默认按拍摄日期归档为 `年/月/日`（例如 `/photos/2026/11/12/IMG_20261112_093000.heic`），也支持按 `{album}/{:%Y/%m}` 分相册存放，直接挂载给 Immich、PhotoPrism、群晖 Photos 扫描即可使用。
- **原画质保留 & 华为动态照片支持**
  - 支持下载原图（JPEG / PNG / HEIC / RAW 等）、原画视频，并自动展开连拍照片。
  - 支持华为动态照片（Motion Photo）：默认完整保留带动态信息的原图；开启 `--live-photo-mov-filename-policy` 后，还可额外把内嵌的短片拆出成独立的 `.mp4` 文件。
- **断点续传 & 不重复下载**
  - 大体积视频支持 HTTP 断点续传，网络抖动不白下。
  - 下载完成后自动校验文件大小与 `SHA256`/`MD5` 校验和，已成功备份的文件会记录在本地数据库中，后续只扫新增部分，跳过重复文件；遇到同日期同文件名但内容不同的照片会自动加后缀区分，绝不静默覆盖。
- **轻量、免浏览器、适合长期挂机**
  - 像 `icloudpd` 一样直接走账号协议通信，不需要启动吃内存的 Chrome / Playwright。
  - 纯 Python 标准库实现，零第三方依赖，Docker 镜像不到 80MB，在树莓派和老旧 NAS 上也能稳定运行。

---

## 快速开始（Docker 定时备份，推荐）

最省事的用法是放进 Docker，每 2 小时自动检查一次新照片，后台静默运行。

### 第 1 步：准备配置文件并启动容器

```bash
git clone https://github.com/tuber0613/hwcloudpd.git && cd hwcloudpd
mkdir -p config photos
cp .env.example .env
```

打开 `.env`，填上你的华为账号和密码（如果要收登录过期通知，顺便填一下 `HWCLOUD_WEBHOOK_URL`）：

```env
HUAWEI_USERNAME=你的手机号或邮箱
HUAWEI_PASSWORD=你的华为账号密码
```

然后启动容器：

```bash
docker compose up -d
```

### 第 2 步：完成首次二次验证（如果账号开启了双重验证）

如果华为检测到新环境登录，会要求做一次二次验证（短信、受信任手机推送或邮箱验证码）。在终端运行：

```bash
docker exec -it hwcloudpd hwcloudpd --auth-only
```

按屏幕提示选择接收方式并输入收到的 **6 位验证码**。验证通过后，工具会自动记住“信任此设备/浏览器”并把登录态保存到宿主机 `./config` 目录。接下来容器会在十秒内自动开始首次全量同步，以后容器重启或日常定时同步通常**不再需要输入验证码**。

> 小提示：如果不开启双重验证，容器启动后会自动静默登录并直接开始下载，不需要执行第 2 步。

### 第 3 步：查看运行状态

```bash
# 查看同步日志
docker compose logs -f hwcloudpd

# 查看华为云端有哪些相册以及照片/视频数量
docker exec -it hwcloudpd hwcloudpd -l
```

---

## 命令行直接使用（不跑 Docker）

适合在本机 macOS / Linux / Windows 下临时备份，或者配合系统 `crontab` 使用（需 Python 3.11+）：

```bash
pip install .
```

常用命令示例：

```bash
# 1. 登录并保存本地登录态（如有二次验证会直接在当前终端提示输入）
hwcloudpd -u 你的手机号或邮箱 --auth-only

# 2. 查看云端相册列表和照片数量
hwcloudpd -l

# 3. 增量同步全部相册到 ./photos（默认按 年/月/日 归档，排除“最近删除”）
hwcloudpd -d ./photos --until-found 50

# 4. 只下载指定相册，并跳过所有视频
hwcloudpd -d ./photos -a Camera -a Screenshots --skip-videos

# 5. 按“相册名/年/月”归档，同时为旧照片补写 EXIF 拍摄时间并生成 .xmp 侧车文件
hwcloudpd -d ./photos --folder-structure "{album}/{:%Y/%m}" --set-exif-datetime --xmp-sidecar

# 6. 仅演习预览最近 20 条会被下载到本地哪个路径（不真下载）
hwcloudpd -d ./photos --only-print-filenames --recent 20

# 7. 后台常驻，每 1 小时轮询一次（类似 icloudpd 的 --watch-with-interval）
hwcloudpd -d ./photos --watch-with-interval 3600 --until-found 50
```

---

## 登录态维护与常见问题

### 1. 登录过期了怎么办？

和 `icloudpd` 一样，云盘的长期信任凭据在几个月后可能会过期（或在账号改密、异常登录时失效）。建议在 `.env` 里配置一个 `HWCLOUD_WEBHOOK_URL`（支持企业微信、飞书、钉钉、Bark、Telegram 或自定义 Webhook）：

- 平时容器会定时心跳保活，只要本地受信任 Cookie 还有效，就会自动悄悄续期；
- 一旦真的需要重新输一次验证码，容器会发消息提醒你，此时只需再到服务器执行一遍 `docker exec -it hwcloudpd hwcloudpd --auth-only` 即可恢复。

### 2. 不想把密码写进服务器，或触发了网页图形滑块怎么办？

如果你不想在 `.env` 里保存账号密码，或者偶尔遇到必须拖动滑块的人机验证，可以用浏览器的登录 Cookie 兜底：

1. 在电脑浏览器登录 [华为云空间](https://cloud.huawei.com/) 并进入 **「图库」**。
2. 按 `F12` 打开开发者工具 -> **Network（网络）** -> 筛选 **Fetch/XHR** -> 右键任意 `/album/` 开头的请求（例如 `getSimpleFile`）-> 选择 **Copy -> Copy as cURL (bash)**。
3. 把复制的内容保存为宿主机上的 `./config/curl.txt`，运行中的容器会在 15 秒内自动读取导入并继续同步。

---

## 常用配置与参数速查

所有功能都既可以作为 CLI 参数传入，也可以写进 `.env` 作为 Docker 环境变量。

### 账号与目录

| 命令行参数 | 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `-u, --username` | `HUAWEI_USERNAME` | - | 华为账号（手机号 / 邮箱） |
| `-p, --password` | `HUAWEI_PASSWORD` | - | 华为账号密码 |
| `--two-fa-code` | `HUAWEI_2FA_CODE` | - | 直接传入 6 位二次验证码（非交互脚本用） |
| `--auth-only` | - | - | 检查登录态，或在终端完成登录/二次验证后退出 |
| `--force-login` / `--login` | - | - | 忽略已有会话，强制重新走一遍登录流程 |
| `--login-timeout` | `HWCLOUD_LOGIN_TIMEOUT` | `30` | 登录请求超时秒数（`docker-compose` 默认 60） |
| `--import-curl` | - | - | 从文件或标准输入导入浏览器 `Copy as cURL` 会话 |
| `-d, --directory` | `HWCLOUD_DIRECTORY` | `./photos`（容器 `/photos`） | 本地照片存储根目录 |
| `--config-dir` | `HWCLOUD_CONFIG_DIR` | `./config`（容器 `/config`） | 存放登录凭据、同步数据库 `state.db` 与健康状态 |

### 相册与内容筛选

| 命令行参数 | 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `-l, --list-albums` | - | - | 列出云端所有相册及照片/视频数 |
| `-a, --album` | `HWCLOUD_ALBUMS` | 除回收站外的全部相册 | 只同步指定相册（支持多个及 `*` 通配符） |
| `--exclude-album` | `HWCLOUD_EXCLUDE_ALBUMS` | - | 排除指定相册（如 `微博*,QQ*`） |
| `--skip-videos` | `HWCLOUD_SKIP_VIDEOS` | `false` | 跳过所有视频 |
| `--skip-photos` | `HWCLOUD_SKIP_PHOTOS` | `false` | 跳过静态图片（JPEG / PNG / HEIC 等） |
| `--skip-live-photos` | `HWCLOUD_SKIP_LIVE_PHOTOS` | `false` | 跳过华为动态照片 |
| `--skip-bursts` | `HWCLOUD_SKIP_BURSTS` | `false` | 不展开连拍组，只保留连拍封面图 |
| `--only-favorites` | `HWCLOUD_ONLY_FAVORITES` | `false` | 只下载已收藏的照片和视频 |
| `--created-after` / `--created-before` | `HWCLOUD_CREATED_AFTER` / `HWCLOUD_CREATED_BEFORE` | - | 按拍摄时间筛选（如 `2026-01-01`） |
| `--min-size` / `--max-size` | `HWCLOUD_MIN_SIZE` / `HWCLOUD_MAX_SIZE` | - | 按文件体积筛选（如 `50KB`、`500MB`） |
| `--recent` | `HWCLOUD_RECENT` | - | 只检查最近 N 条记录 |
| `--until-found` | `HWCLOUD_UNTIL_FOUND` | CLI 无 / 容器 `50` | 连续遇到 N 张已备份照片即提前结束本次扫描 |

### 目录结构与照片处理

| 命令行参数 | 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `--folder-structure` | `HWCLOUD_FOLDER_STRUCTURE` | `{:%Y/%m/%d}` | 子目录格式，如 `{album}/{:%Y/%m}` 或 `none` |
| `--tz, --timezone` | `TZ` | `Asia/Shanghai` | 用于日期分文件夹与定时计划的时区 |
| `--size` | `HWCLOUD_SIZE` | `original` | 下载规格：`original`（原图）/ `medium` / `thumb` |
| `--file-match-policy` | `HWCLOUD_FILE_MATCH_POLICY` | `name-size-dedup-with-suffix` | 同名冲突策略，可选 `name-id7` |
| `--live-photo-mov-filename-policy` | `HWCLOUD_LIVE_PHOTO_POLICY` | `none` | 额外拆出动态照片内嵌视频：`none` / `suffix` / `original` |
| `--set-exif-datetime` | `HWCLOUD_SET_EXIF_DATETIME` | `false` | 为缺少 EXIF 时间的 JPEG 补写拍摄时间 |
| `--xmp-sidecar` | `HWCLOUD_XMP_SIDECAR` | `false` | 生成兼容 Immich / PhotoPrism / Lightroom 的 `.xmp` |

### 同步、清理、并发与后台守护

| 命令行参数 | 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `--auto-delete` | `HWCLOUD_AUTO_DELETE` | `false` | **同步删除**：云端移入“最近删除”后同步清理本地 |
| `--delete-after-download` | `HWCLOUD_DELETE_AFTER_DOWNLOAD` | `false` | 下载校验成功后立即将云端原文件移入“最近删除” |
| `--keep-cloud-recent-days` | `HWCLOUD_KEEP_CLOUD_RECENT_DAYS` | - | 只在云端保留最近 N 天，超期且已备份项移入“最近删除” |
| `--dry-run` | `HWCLOUD_DRY_RUN` | `false` | 演习模式，不真正下载或删除文件 |
| `--only-print-filenames` | - | `false` | 只输出待下载文件路径 |
| `--download-concurrency` | `HWCLOUD_DOWNLOAD_CONCURRENCY` | `2` | 并发下载线程数 |
| `--page-size` / `--request-delay-ms` | `HWCLOUD_PAGE_SIZE` / `HWCLOUD_REQUEST_DELAY_MS` | `100` / `120` | 单页拉取条数与分页间隔（毫秒） |
| `--cron` | `CRON_SCHEDULE` | 容器 `0 */2 * * *` | 5 字段 Cron 定时同步表达式 |
| `--watch-with-interval` | `HWCLOUD_WATCH_INTERVAL` | - | 每隔固定秒数轮询同步一次 |
| `--keepalive-interval` | `HWCLOUD_KEEPALIVE_INTERVAL` | `60` | 守护模式下登录态心跳保活间隔（秒） |
| `--no-run-on-startup` | `HWCLOUD_RUN_ON_STARTUP` | 容器 `true` | 守护启动时不立即同步，等待首个定时间点 |
| `--full-scan-interval-hours` | `HWCLOUD_FULL_SCAN_INTERVAL_HOURS` | `168` | 每隔 N 小时做一次全量校验扫描（每周兜底） |
| `--webhook-url` / `--telegram-chat-id` | `HWCLOUD_WEBHOOK_URL` / `HWCLOUD_TELEGRAM_CHAT_ID` | - | 企微 / 飞书 / 钉钉 / Bark / Telegram 告警地址 |
| `--notification-script` | `HWCLOUD_NOTIFICATION_SCRIPT` | - | 自定义通知脚本（传参：`event_type title message`） |
| `--log-level` | `HWCLOUD_LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |

---

## 注意事项

- 本项目为个人开源工具，与华为公司没有任何关联。
- 涉及清理或删除的操作（`--auto-delete`、`--delete-after-download`、`--keep-cloud-recent-days`）开启前，建议先加上 `--dry-run` 跑一遍确认无误后再正式启用。

---

## 致谢

衷心感谢开源项目 [**icloud_photos_downloader (`icloudpd`)**](https://github.com/icloud-photos-downloader/icloud_photos_downloader) 及其所有维护者和贡献者，同时也感谢围绕它打造容器体验的 [**docker-icloudpd**](https://github.com/boredazfcuk/docker-icloudpd) 社区。

`hwcloudpd` 的核心产品设计、Copy / Sync / Move 三种工作模式、拍摄日期归档模板、增量早停（`--until-found`）策略，以及“环境变量配账密 + 容器终端输入二次验证码 + 持久化信任 Cookie”的流畅使用体验，均深深得益于 `icloudpd` 多年打磨出的优秀设计。如果你同时也在用 iPhone，强烈推荐直接使用原版 `icloudpd`！

---

## 开发测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
