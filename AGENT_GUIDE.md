# AGENT_GUIDE · AI Agent 部署与操作指南

> 把这份文档交给任何 AI 助手（Claude / ChatGPT / WorkBuddy / Copilot…），它读完就能在这台
> Windows 电脑上完成部署、启动、验证和日常操作。
>
> Hand this file to any AI assistant — after reading it, the agent can deploy, launch,
> verify and operate this toolkit on a Windows PC.

---

# 第一部分 · 中文

## 1. 这个项目是什么

「闲鱼搬家用工具包」：一个**纯本地**的闲鱼商品搬家工作台。

- 后端 `app/workbench.py`：Python 标准库 http.server，只监听 127.0.0.1，无任何云端依赖。
- 网页四步流程：① 环境准备 → ② 导出包（弹 Edge 扫码导出自己账号的在售商品）→
  ③ 清洗与改写（离线改文案 + 抹二维码/条形码/面单/水印 + 平台规则体检）→
  ④ 发布（默认演练；真实发布走 goofish-cli）。
- 所有数据只落在工具包目录内，不上传任何服务器。

## 2. 部署步骤（按顺序执行）

### 第 1 步：确认 Python ≥ 3.9
```bat
python --version
```
- 有 3.9+ → 继续。
- 没有 → 直接双击根目录 `启动工作台.cmd`，它会按
  `XIANYU_PYTHON 环境变量 → runtime\python.exe → %USERPROFILE%\.workbuddy\...\python.exe → PATH`
  的顺序找；都找不到时会**询问并自动下载便携版 Python 3.12.10 + pip** 到 `runtime\`（约 12 MB，需联网）。
- 也可以手动装：https://www.python.org/downloads/ 安装时勾选 *Add python.exe to PATH*。

### 第 2 步：安装 Python 依赖
```bat
python -m pip install -r app\requirements.txt
```
装的是：playwright、opencv-python-headless、numpy、pillow、rapidocr-onnxruntime、openpyxl。
**不需要**执行 `playwright install`——浏览器自动化走系统自带 Edge（channel 方式），Win10/11 自带。

### 第 3 步：启动
```bat
python app\workbench.py
```
浏览器自动打开 `http://127.0.0.1:8765`（端口占用会自动往后试到 8774）。
可选参数：`--port 9000`、`--no-browser`。

### 第 4 步：验证
```bat
curl http://127.0.0.1:8765/api/health
curl -X POST http://127.0.0.1:8765/api/env/check -H "Content-Type: application/json" -d "{}"
```
`/api/env/check` 返回的 `env.ok` 为 `true` 即部署完成。缺什么会列在 `env.problems`。

### 第 5 步（可选，仅真实发布需要）
1. **Node.js ≥ 16** 必须在 PATH 里。goofish-cli 靠 execjs 执行签名 JS，没有 Node 会回退
   JScript 并报 `SyntaxError`（这是发布失败最常见根因）。
2. 安装 goofish-cli：`python -m pip install goofish-cli`（工作台第 4 页也有「一键安装」按钮）。
3. 登录：工作台第 4 页「扫码登录」按钮会弹独立 Edge 窗口；登录态存 `数据\浏览器资料\`，之后免扫码。

## 3. 目录结构

```
工具包根/
  启动工作台.cmd        双击入口（自动找/下载 Python）
  使用说明.txt          面向人的说明
  AGENT_GUIDE.md        本文件（面向 AI）
  README.md / README_EN.md
  config.json           {"data_dir": "数据", "port": 8765}
  app/                  workbench.py（后端+页面）、export_items.py（自导出器）、
                        rewrite_engine.py（离线改写）、qr_login_shim.py、
                        index.html、requirements.txt
  数据/                 收件箱/（导出 ZIP）、项目/（一项目一账号）、
                        发布包/（一商品一文件夹 + publish_all.py + 发布记录.json）、
                        浏览器资料/（Edge 登录态）、工作台项目.json（台账）、工作台日志.txt
```

## 4. HTTP API 速查（127.0.0.1，JSON）

**GET（读状态）**：`/api/health`、`/api/state`（数据目录/项目/收件箱总览）、`/api/items`、
`/api/pkg/items`（发布包清单，含 published 标记）、`/api/import/status`、`/api/env/status`、
`/api/export/status`、`/api/publish/status`、`/api/cli/status`

**POST（动作，body 为 JSON）**：
`/api/env/check`（自检）、`/api/env/install`（pip 装依赖）、`/api/import`（导入 ZIP）、
`/api/projects/switch`、`/api/config`（改 data_dir/port）、`/api/save`（存单件文案）、
`/api/auto-rewrite`（一键改写）、`/api/smart-clean`（排图清洗动作）、`/api/rules-check`
（平台规则体检）、`/api/build`（生成发布包）、`/api/export/start|stop`（自导出，弹 Edge）、
`/api/publish/start|stop`（发布；`ids` 可选指定件）、`/api/pkg/edit`（改单件标题/价格/描述）、
`/api/cli/install`、`/api/cli/auth`（扫码登录）、`/api/cli/auth/cancel`、`/api/bulk-include`

长任务（导出/发布/装依赖）都是后台线程：POST 启动 → 轮询对应 `/status` → `running:false` 即结束。
想看细节直接读 `app/workbench.py` 的 `do_GET` / `do_POST`。

## 5. 给 Agent 的行为红线（必须遵守）

1. **数据不出本机**：`数据\` 下所有内容（尤其 `浏览器资料\` 含登录 Cookie）禁止上传、
   粘贴到网络、或拷贝给第三方。
2. **真实发布必须用户明确确认**后才允许调 `/api/publish/start`；默认状态是演练。
3. 任务运行中（导出/发布/装依赖）不要手改 `数据\项目\` 下的文件。
4. 不绕风控：导出器遇滑块会停，属正常，让用户人工过滑块后重试；不要尝试破解。
5. 抓后端子进程日志时设 `PYTHONIOENCODING=utf-8`，否则中文 Windows 下日志是 GBK 乱码。
6. 出问题先读 `数据\工作台日志.txt`，再读 `工作台日志.txt` 同目录的旧日志。

## 6. 常见故障

| 现象 | 原因与处理 |
|---|---|
| 发布报 `SyntaxError` | 没装 Node.js，装 Node ≥16 并确保 `node -v` 可用 |
| 双击 .cmd 乱码 | .cmd 是纯 ASCII，别用记事本另存成 UTF-8 |
| 端口被占 | 启动器会清 8765 旧进程；工作台自动试 8765–8774 |
| 图片清洗 0 张 | 初始导入时已按同规则排好动作，看「看清洗效果」核对 |
| pip 装依赖很慢 | 网络问题，可加 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| 找回项目 | `数据\项目\` 下每个文件夹是一个号，删掉即弃，工作台自动跳过 |

---

# Part 2 · English

## 1. What this is

"Goofish Listing Migration Toolkit" — a **fully local** web workbench for migrating your
own Xianyu/Goofish listings between accounts.

- Backend `app/workbench.py`: Python stdlib http.server bound to 127.0.0.1 only.
- 4-step web UI: ① Environment → ② Export (QR-login in a separate Edge window, export
  your own on-sale listings) → ③ Clean & Rewrite (offline rewrite; scrub QR codes /
  barcodes / shipping labels / watermarks; platform-rule check) → ④ Publish
  (dry-run by default; real publishing via goofish-cli).
- Nothing leaves the machine.

## 2. Setup

**Step 1 — Python ≥ 3.9**: `python --version`. If missing, just double-click
`启动工作台.cmd` — it searches `XIANYU_PYTHON` → `runtime\python.exe` →
`%USERPROFILE%\.workbuddy\...` → `PATH`, and offers to auto-download a portable
Python 3.12.10 + pip (~12 MB) into `runtime\` when none is found.

**Step 2 — Dependencies**:
```bat
python -m pip install -r app\requirements.txt
```
Installs playwright, opencv-python-headless, numpy, pillow, rapidocr-onnxruntime, openpyxl.
**No** `playwright install` needed — automation drives the system Edge via channel.

**Step 3 — Launch**: `python app\workbench.py` (opens `http://127.0.0.1:8765`,
auto-falls back to 8774; flags `--port N`, `--no-browser`).

**Step 4 — Verify**:
```bat
curl http://127.0.0.1:8765/api/health
curl -X POST http://127.0.0.1:8765/api/env/check -H "Content-Type: application/json" -d "{}"
```
`env.ok == true` means ready; missing items are listed in `env.problems`.

**Step 5 — Real publishing (optional)**:
1. **Node.js ≥ 16** must be on PATH — goofish-cli runs its signing JS through execjs;
   without Node it falls back to JScript and fails with `SyntaxError` (the #1 cause of
   publish failures).
2. `python -m pip install goofish-cli` (or the one-click button on page 4).
3. Login via the page-4 QR button (opens a dedicated Edge window; session persists in
   `数据\浏览器资料\`).

## 3. Layout

See the tree in the Chinese part above — same structure. `数据\` = all data
(inbox ZIPs, per-account projects, publish packages, Edge profile with login state,
ledger + logs).

## 4. API map

GET status: `/api/health`, `/api/state`, `/api/items`, `/api/pkg/items`,
`/api/import/status`, `/api/env/status`, `/api/export/status`, `/api/publish/status`,
`/api/cli/status`.

POST actions (JSON body): `/api/env/check`, `/api/env/install`, `/api/import`,
`/api/projects/switch`, `/api/config`, `/api/save`, `/api/auto-rewrite`,
`/api/smart-clean`, `/api/rules-check`, `/api/build`, `/api/export/start|stop`,
`/api/publish/start|stop`, `/api/pkg/edit`, `/api/cli/install`, `/api/cli/auth`,
`/api/cli/auth/cancel`, `/api/bulk-include`.

Long tasks run as background threads: POST to start → poll the matching `/status`
until `running:false`. Authoritative source: `do_GET` / `do_POST` in `app/workbench.py`.

## 5. Hard rules for agents

1. **Nothing in `数据\` ever leaves this machine** — especially `浏览器资料\`
   (login cookies). Do not upload, paste online, or share it.
2. Never call `/api/publish/start` without the user's explicit confirmation;
   the default mode is dry-run.
3. Do not hand-edit files under `数据\项目\` while a task is running.
4. Do not bypass anti-bot measures — the exporter stops at slider captchas by design;
   let the user solve the slider manually and retry.
5. When capturing backend subprocess output, set `PYTHONIOENCODING=utf-8` or logs
   will be GBK-garbled on Chinese Windows.
6. On failure, read `数据\工作台日志.txt` first.

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Publish fails with `SyntaxError` | Node.js missing — install Node ≥16, ensure `node -v` works |
| .cmd shows mojibake | .cmd files are ASCII-only; never re-save them as UTF-8 in Notepad |
| Port busy | Launcher clears stale 8765 listeners; backend auto-tries 8765–8774 |
| "0 images adjusted" | Actions were pre-arranged at import; verify via the preview button |
| Slow pip | Add `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| Recover a project | Each folder in `数据\项目\` is one account; delete to discard |
