# hwcloudpd — 华为云空间图库自动同步工具（Docker / CLI）

对标 [`icloud_photos_downloader`](https://github.com/icloud-photos-downloader/icloud_photos_downloader)（`icloudpd`）设计的华为云空间（`https://cloud.huawei.com/`）云图库同步与 Docker 后台守护工具。与 `icloudpd`（`pyicloud-ipd`）一样采用**纯 HTTP 协议级认证 + 持久化 CookieJar（`cas_cookies.lwp`）+ Bash 交互式二次验证（`--auth-only`）**，**完全不依赖浏览器或 Playwright**，同时保留 `Copy as cURL` 导入兜底。

---

## 核心特性

- **与 `icloudpd` 一致的纯 HTTP 协议认证（零浏览器依赖）**：
  - 全程基于 Python 标准库实现，镜像无 Chromium 等庞大依赖（`< 80MB`）。
  - 将华为账号与密码配置在 `.env` / 环境变量（`HUAWEI_USERNAME` / `HUAWEI_PASSWORD`），认证器直接对接 `id1.cloud.huawei.com/CAS` 登录协议（`getPageInfo` -> `chkRisk` -> `remoteLogin` -> REST 二次验证 -> `updateTrustBrowser` -> `others/login.action` 票据换取 -> `/html/getHomeData` 提取 `userid`）。
  - 首次登录或触发华为双重验证（华为受信任设备推送 / 短信 `getSMSCodeV3` / 邮箱 `getEMailAuthCode`）时，在宿主机 Bash 执行 `docker exec -it hwcloudpd hwcloudpd --auth-only` 输入 6 位验证码，程序会自动调用 `updateTrustBrowser` 并将跨域名 Cookie（含 `.id1.cloud.huawei.com` `CASTGC` 与信任 Cookie）持久化到 `/config/cas_cookies.lwp`。
  - 会话过期时，守护进程优先尝试 `jsRemoteLogin` SSO 快登与静默重登；仅当华为再次强制要求新二次验证码或触发极验滑块（`isNeedImageCode=1`）时，才发 Webhook 通知人工介入。
- **默认按拍摄日期归档**：默认 `{:%Y/%m/%d}`（如 `/photos/2026/11/12/IMG_31832.jpg`），默认同步除“最近删除”外的所有系统相册与自定义相册；也支持 `{album}/{:%Y/%m}` 或扁平 `none`。
- **完整 `icloudpd` 对标功能**：
  - 增量早停（`--until-found`）+ SQLite 状态库（`state.db`）+ 周期性全量兜底扫描；
  - `.hwpart` HTTP `Range` 断点续传 + 原图 `fileSize` 与 `SHA256`/`MD5` 校验 + 原子落盘；
  - 连拍展开（`burstphotolist`）、华为动态照片（`fileType="5"`）原图保留与可选独立 MP4 提取（`--live-photo-mov-filename-policy`）；
  - 可选无损补写 EXIF `DateTimeOriginal`（`--set-exif-datetime`）与生成 `.xmp` 侧车文件（`--xmp-sidecar`）；
  - 相册白/黑名单通配符、媒体类型/收藏/日期/体积/`--recent` 过滤、`--auto-delete`、`--keep-cloud-recent-days`、`--delete-after-download`、`--dry-run` / `--only-print-filenames`。

---

## `icloudpd` 与 `hwcloudpd` 认证逻辑对比

| 阶段 | `icloudpd` (`pyicloud-ipd`) | `hwcloudpd` (华为 CAS 纯 HTTP) |
| :--- | :--- | :--- |
| **密码握手** | Apple `idmsa.apple.com` SRP-6a 加密握手 | `POST .../ajaxHandler/login/remoteLogin` HTTPS TLS 直传（+ `+86` -> `0086` 规范化） |
| **二次验证 (2FA)** | 409 触发后调用 `/verify/trusteddevice/securitycode` 等提交 6 位码 | `10012072`/`10002080` 返回可用通道（受信任设备推送 / `getSMSCodeV3` / `getEMailAuthCode`），携 `twoStepVerifyCode` 重提 `remoteLogin` |
| **设备/浏览器信任** | `GET /2sv/trust` 获取 `X-Apple-Twosv-Trust-Token` | `POST .../login/updateTrustBrowser` (`trustBrowser=1`) 下发信任 Cookie |
| **状态持久化** | `LWPCookieJar` + `session.json` | `/config/cas_cookies.lwp` (`LWPCookieJar`) + `/config/auth.json` |
| **业务网关兑换** | `setup.icloud.com/setup/ws/1/accountLogin` | SSO `callbackURL` (`others/login.action?ticket=ST-...`) + `POST /html/getHomeData` 提取 `userid` |

---

## 部署方式一：环境变量配置账号密码 + Bash 二次验证（推荐，与 `icloudpd` 一致）

### 1. 配置 `.env` 并启动守护容器

```bash
git clone https://github.com/tuber0613/hwcloudpd.git && cd hwcloudpd
mkdir -p config photos
cp .env.example .env
# 编辑 .env，填入 HUAWEI_USERNAME 和 HUAWEI_PASSWORD（以及可选的 HWCLOUD_WEBHOOK_URL）
docker compose up -d
```

### 2. 在 Bash 完成首次登录与二次验证（如触发）

如果账号未触发二次验证且未出现滑块挑战，容器启动后会自动静默登录并立即开始首次同步；若触发了设备/短信/邮箱二次验证，执行一次：

```bash
docker exec -it hwcloudpd hwcloudpd --auth-only
```

按终端提示选择验证方式（如有多种）并输入收到的 6 位验证码即可（程序会自动调用 `updateTrustBrowser` 记住信任状态，并将跨域名 Cookie 持久化至 `./config/cas_cookies.lwp`，把图库会话写入 `./config/auth.json`）。守护容器检测到新凭证后会在 15 秒内自动开始同步。

> 若后续某天因长时间跨度导致会话失效且受信任状态失效，容器会推送 Webhook 告警（如配置了 `HWCLOUD_WEBHOOK_URL`），只需再次执行一次 `docker exec -it hwcloudpd hwcloudpd --auth-only` 即可恢复。

---

## 部署方式二：浏览器 `Copy as cURL` 凭证导入（免账号密码/滑块兜底）

若遇到风控滑块验证（`isNeedImageCode=1`），或不想在服务器写入华为账号密码，也可使用浏览器导出的凭证：

1. 在电脑浏览器登录 [华为云空间](https://cloud.huawei.com/) 并进入 **「图库」**。
2. 按 `F12` -> **Network（网络）** -> 筛选 **Fetch/XHR** -> 右键任意 `/album/` 请求（如 `getSimpleFile`）-> **Copy -> Copy as cURL (bash)**。
3. 保存为 `./config/curl.txt`，运行中的容器将在 15 秒内自动导入为 `auth.json` 并热恢复同步。

---

## CLI 常用命令示例

```bash
# 交互式登录 / 验证当前会话（本地或容器内）
hwcloudpd -u 13800138000 -p 'your_password' --auth-only

# 强制刷新登录并进入 bash 二次验证
hwcloudpd --auth-only --force-login

# 查看云端所有相册与照片/视频数量
hwcloudpd -l

# 演习模式（仅打印待下载文件路径）
hwcloudpd --only-print-filenames --recent 20

# 单次增量同步（按日期归档，除回收站外全部相册）
hwcloudpd -d ./photos --config-dir ./config --until-found 50
```

---

## 参数与环境变量对照表（对标 `icloudpd`）

| CLI 参数 | 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `-u, --username` | `HUAWEI_USERNAME` | - | 华为账号（手机号 / 邮箱） |
| `-p, --password` | `HUAWEI_PASSWORD` | - | 华为账号密码 |
| `--two-fa-code` | `HUAWEI_2FA_CODE` | - | 可选 6 位二次验证码（非交互模式提交） |
| `--auth-only` | - | - | 校验登录态或执行纯 HTTP 协议登录 + Bash 二次验证 |
| `--force-login` / `--login` | - | - | 强制重新通过 CAS 协议登录并更新受信任会话 |
| `--login-timeout` | `HWCLOUD_LOGIN_TIMEOUT` | `30` | CAS 登录 HTTP 请求超时时间（秒） |
| `-d, --directory` | `HWCLOUD_DIRECTORY` | `./photos`（容器 `/photos`） | 本地归档根目录 |
| `--config-dir` | `HWCLOUD_CONFIG_DIR` | `./config`（容器 `/config`） | 存放 `auth.json`、`cas_cookies.lwp`、`state.db`、`health.json` |
| `--folder-structure` | `HWCLOUD_FOLDER_STRUCTURE` | `{:%Y/%m/%d}` | 目录模板，支持 `{album}/{:%Y/%m}` 或 `none` |
| `--tz, --timezone` | `TZ` | `Asia/Shanghai` | 拍摄日期分桶与 Cron 调度时区 |
| `-l, --list-albums` | - | - | 列出所有相册 ID、名称及媒体数量 |
| `-a, --album` | `HWCLOUD_ALBUMS` | 全部非回收站相册 | 仅同步指定相册（可重复或逗号分隔，支持通配符） |
| `--exclude-album` | `HWCLOUD_EXCLUDE_ALBUMS` | - | 排除指定相册（如 `微博*,QQ*`） |
| `--size` | `HWCLOUD_SIZE` | `original` | 下载尺寸：`original`（原图） / `medium` / `thumb` |
| `--file-match-policy` | `HWCLOUD_FILE_MATCH_POLICY` | `name-size-dedup-with-suffix` | 冲突命名策略，可选 `name-id7` |
| `--live-photo-mov-filename-policy` | `HWCLOUD_LIVE_PHOTO_POLICY` | `none` | 提取动态照片内嵌 MP4：`none` / `suffix` / `original` |
| `--set-exif-datetime` | `HWCLOUD_SET_EXIF_DATETIME` | `false` | JPEG 缺少 `DateTimeOriginal` 时无损补写 EXIF |
| `--xmp-sidecar` | `HWCLOUD_XMP_SIDECAR` | `false` | 生成 `.xmp` 元数据侧车文件 |
| `--skip-videos` | `HWCLOUD_SKIP_VIDEOS` | `false` | 跳过视频（`fileType=4`） |
| `--skip-photos` | `HWCLOUD_SKIP_PHOTOS` | `false` | 跳过静态图片（如 JPEG/PNG `fileType=1`、HEIC `fileType=9`） |
| `--skip-live-photos` | `HWCLOUD_SKIP_LIVE_PHOTOS` | `false` | 跳过动态照片（`fileType=5`） |
| `--skip-bursts` | `HWCLOUD_SKIP_BURSTS` | `false` | 不展开连拍组，仅下载连拍封面 |
| `--only-favorites` | `HWCLOUD_ONLY_FAVORITES` | `false` | 仅同步已收藏媒体 |
| `--created-after` / `--created-before` | `HWCLOUD_CREATED_AFTER` / `HWCLOUD_CREATED_BEFORE` | - | 拍摄时间范围过滤 |
| `--min-size` / `--max-size` | `HWCLOUD_MIN_SIZE` / `HWCLOUD_MAX_SIZE` | - | 文件大小过滤（如 `50KB`、`500MB`） |
| `--recent` | `HWCLOUD_RECENT` | - | 仅检查最近 N 条媒体 |
| `--until-found` | `HWCLOUD_UNTIL_FOUND` | 命令行默认空 / 容器 `50` | 连续命中 N 条已同步记录即早停 |
| `--auto-delete` | `HWCLOUD_AUTO_DELETE` | `false` | 同步删除：云端移入“最近删除”后同步清理本地文件 |
| `--delete-after-download` | `HWCLOUD_DELETE_AFTER_DOWNLOAD` | `false` | 校验下载成功后将云端源文件移入“最近删除” |
| `--keep-cloud-recent-days` | `HWCLOUD_KEEP_CLOUD_RECENT_DAYS` | - | 仅保留云端最近 N 天媒体，更早且已备份项移入云回收站 |
| `--dry-run` / `--only-print-filenames` | `HWCLOUD_DRY_RUN` | `false` | 演习模式 / 仅打印目标路径 |
| `--download-concurrency` | `HWCLOUD_DOWNLOAD_CONCURRENCY` | `2` | 并发下载线程数 |
| `--cron` | `CRON_SCHEDULE` | - | 守护模式 5 字段 Cron 表达式（如 `0 */2 * * *`） |
| `--watch-with-interval` | `HWCLOUD_WATCH_INTERVAL` | - | 守护模式固定间隔秒数 |
| `--keepalive-interval` | `HWCLOUD_KEEPALIVE_INTERVAL` | `60` | 会话心跳保活间隔（秒） |
| `--full-scan-interval-hours` | `HWCLOUD_FULL_SCAN_INTERVAL_HOURS` | `168` | 每 N 小时执行一次全量兜底扫描（跳过早停） |
| `--webhook-url` | `HWCLOUD_WEBHOOK_URL` | - | 企微 / 飞书 / 钉钉 / Bark / Telegram / 通用 Webhook |
| `--notification-script` | `HWCLOUD_NOTIFICATION_SCRIPT` | - | 自定义告警脚本（参数：`event title message`） |

---

## 本地测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
