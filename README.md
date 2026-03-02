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

### 2) 解压并准备配置

macOS / Linux：

```bash
tar -xzf tg-codex-<os>-<arch>.tar.gz
cd <解压目录>
./tg-codex init --token <TG_BOT_TOKEN>
```

Windows（PowerShell）：

```powershell
Expand-Archive .\tg-codex-windows-<arch>.zip -DestinationPath .\tg-codex
cd .\tg-codex
.\tg-codex.exe init --token <TG_BOT_TOKEN>
```

> 首次自动识别 chat/user id 前，请先在 Telegram 里给你的 bot 发送一次 `/start`（或任意消息）。

### 3) 启动

macOS / Linux：

```bash
chmod +x ./tg-codex
./tg-codex start --host 0.0.0.0 --port 8000
```

Windows：

```powershell
.\tg-codex.exe start --host 0.0.0.0 --port 8000
```

> 可选：用二进制自动生成/更新 `.env`
>
> `./tg-codex init --token <TG_BOT_TOKEN>`

---

## 功能

- Telegram 下发任务：`/run <prompt>`
- 流式输出实时回写（编辑同一条消息）
- diff/patch 输出更友好渲染
- 图片输入支持（photo/document image）
- 每个 chat 自动续接 Codex session
- 每次任务都会上传完整输出文件 `codex-output-*.txt`

## 一键本地构建并运行（二进制）

如果你是项目维护者，直接在仓库里执行：

```bash
./one_click_start.sh --token <TG_BOT_TOKEN>
```

行为：

1. 若 `.env` 不存在，自动用 token 初始化并回填 chat/user id
2. 若 `dist/tg-codex` 不存在，自动调用 `./build_binary.sh` 构建
3. 直接启动服务

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
python cli.py start --host 0.0.0.0 --port 8000
```

## Telegram 命令

- `/start`
- `/id`
- `/run <prompt>`
- `/status`
- `/cancel`
- `/auth <passphrase>`
- `/cmd` / `/cmd <prefix>` / `/cmd reset`

## 关键环境变量

- `TG_BOT_TOKEN`（必填）
- `TG_ALLOWED_CHAT_IDS`（必填）
- `TG_ALLOWED_USER_IDS`（必填）
- `TG_ADMIN_CHAT_IDS` / `TG_ADMIN_USER_IDS`（可选，默认继承 allowlist）
- `CODEX_COMMAND_PREFIX`（默认 `codex -a never exec --full-auto`）
- `CODEX_TIMEOUT_SECONDS`
- `TG_MAX_CONCURRENT_TASKS`
- `TG_MAX_BUFFERED_OUTPUT_CHARS`
- `TG_AUTH_PASSPHRASE` / `TG_AUTH_TTL_SECONDS`

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
