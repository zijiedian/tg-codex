# tg-codex

`tg-codex` 是一个 Telegram -> Codex CLI 的桥接服务（FastAPI + python-telegram-bot）。

## 二进制优先：3 分钟启动（推荐）

### 1) 下载二进制

到 Release 页面下载你系统对应的包：

- `tg-codex-linux-*.tar.gz`
- `tg-codex-macos-*.tar.gz`
- `tg-codex-windows-*.zip`

Release 页面：

- https://github.com/zijiedian/tg-codex/releases

### 2) 一行命令直接启动

macOS / Linux：

```bash
tar -xzf tg-codex-<os>-<arch>.tar.gz
cd <解压目录>
./tg-codex --token <TG_BOT_TOKEN> --port 18000
```

Windows（PowerShell）：

```powershell
Expand-Archive .\tg-codex-windows-<arch>.zip -DestinationPath .\tg-codex
cd .\tg-codex
.\tg-codex.exe --token <TG_BOT_TOKEN> --port 18000
```

这条命令会自动做三件事：

1. 自动写入/更新 `.env`
2. 自动通过 token 拉取并填充 `chat_id/user_id` allowlist
3. 自动生成 `TG_AUTH_PASSPHRASE` 并在终端打印完整 `/auth xxxxx`（首次）
4. 直接启动服务

> 首次自动识别 chat/user id 前，请先在 Telegram 里给你的 bot 发送一次 `/start`（或任意消息）。

后续再次启动（无需再传 token）：

```bash
./tg-codex --port 18000
```

---

## 功能

- Telegram 下发任务：`/run <prompt>`
- 流式输出实时回写（编辑同一条消息）
- 私聊任务运行中会额外调用 Telegram `sendMessageDraft`（原生 HTTP，失败自动回退）
- diff/patch 输出更友好渲染
- 图片输入支持（photo/document image）
- 每个 chat 自动续接 Codex session
- 支持 `/cwd` 切换执行目录（按 chat 独立保存）
- 可选上传完整输出文件 `codex-output-*.txt`（默认关闭，开启后仅长输出上传）

## 一键本地构建并运行（二进制）

如果你是项目维护者，直接在仓库里执行：

```bash
./start.sh --token <TG_BOT_TOKEN>
```

行为：

1. 若传入 `--token`，自动写入 token 并回填 chat/user id
2. 若 `dist/tg-codex` 不存在，自动调用 `./build_binary.sh` 构建
3. 直接启动服务（统一入口：`start.sh`）

开发调试（代码修改自动重启）：

```bash
./start.sh --reload --token <TG_BOT_TOKEN>
```

`--reload` 会自动切换为 Python 模式（`cli.py`）运行，不走二进制。

单独构建命令：

```bash
./build_binary.sh
```

产物：

- `dist/tg-codex`

## 自动化发布（GitHub Actions）

工作流文件：

- `.github/workflows/release.yml`

触发方式：

1. 推送语义化标签（推荐）：

```bash
git tag v1.0.0
git push origin v1.0.0
```

2. GitHub Actions 页面手动触发 `Build And Release`（填写 tag）

发布结果：

- 自动构建 macOS / Linux / Windows 二进制
- 自动打包归档并生成 `SHA256SUMS.txt`
- 自动创建 GitHub Release 并上传可下载资产

## Python 模式（备用）

```bash
cp .env.example .env
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python cli.py --token <TG_BOT_TOKEN> --port 18000
# 后续可直接：
# python cli.py --port 18000
```

## Telegram 命令

- `/start`
- `/id`
- `/run <prompt>`
- `/new`
- `/cwd <path>` / `/cwd reset`
- `/skill` / `/skill <name>`
- `/status`
- `/cancel`
- `/auth <passphrase>`
- `/cmd` / `/cmd <prefix>` / `/cmd reset`
- `/setting`
- `/setting output_file on|off`
- `/setting auth_ttl <duration>`
- `/setting session_resume on|off`

## 关键环境变量

- `TG_BOT_TOKEN`（必填）
- `TG_ALLOWED_CHAT_IDS`（必填）
- `TG_ALLOWED_USER_IDS`（必填）
- `TG_ADMIN_CHAT_IDS` / `TG_ADMIN_USER_IDS`（可选，默认继承 allowlist）
- `CODEX_COMMAND_PREFIX`（默认 `codex -a never exec --full-auto`）
- `CODEX_TIMEOUT_SECONDS`
- `TG_MAX_CONCURRENT_TASKS`
- `TG_MAX_BUFFERED_OUTPUT_CHARS`
- `TG_ENABLE_OUTPUT_FILE`（默认 `0`；设为 `1` 后，长输出会上传 `codex-output-*.txt`）
- `TG_ENABLE_SESSION_RESUME`（默认 `1`；设为 `0` 可禁用会话续接，行为更接近单次 `codex exec`）
- `TG_AUTH_PASSPHRASE` / `TG_AUTH_TTL_SECONDS`（支持 `3600`、`60s`、`30m`、`2h`、`7d`）

## 安全与敏感信息

仓库已忽略以下内容：

- `.env`
- `.venv/`
- `build/`, `dist/`, `*.spec`
- `chat_sessions.json`
- `outputs/`
- `incoming_media/`
- 运行日志与缓存

请勿提交真实 token、webhook secret、生产 chat/user id。

## License

MIT License，见 `LICENSE`。
