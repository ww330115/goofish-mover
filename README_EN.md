# Goofish Listing Migration Toolkit (闲鱼搬家用工具包)

**Fully local · your data never leaves the PC · a double-click Xianyu/Goofish listing
migration workbench.**

Move all on-sale listings from one Goofish (Xianyu) account to another: automatic
export → offline image cleaning → offline copy rewriting → platform-rule check →
rate-limited publishing. Listens on `127.0.0.1` only; nothing is uploaded anywhere.

[中文说明](README.md)

---

## ✨ Features

| Step | Capability |
|---|---|
| ① Environment | Self-checks Python / dependencies / Edge / goofish-cli; one-click install of anything missing — no terminal needed on a fresh PC |
| ② Export | QR-login in a dedicated Edge window, then automatically fetches your own on-sale listings, downloads images and packs a ZIP; session persists so later runs skip login |
| ③ Clean & Rewrite | Offline rewrite engine (restructures title/description without any cloud LLM); scrubs QR codes & barcodes, removes shipping labels, crops watermarks; **platform-rule check** scans every item for 9 risk classes (off-site contact, counterfeit words, superlatives, duplicate listings, …) |
| ④ Publish | Dry-run by default; real publishing via goofish-cli with 1 item/minute rate limit, automatic skip on failure, read-back verification, and published-markers to prevent duplicates |

Also included:

- **One account = one project** — per-account state, packages and publishing are fully isolated
- Item list with checkboxes, preview, and per-item editing (title / price / shipping / description)
- Automatic rate-limit wait-and-retry and category fallback
- Configurable data directory (portable / D: drive / USB stick)
- Original images and ZIPs are never modified; all results go to new folders

## 🚀 Quick Start

1. Double-click **`启动工作台.cmd`**
   - Python 3.9+ present → starts directly
   - No Python → it offers to **auto-download a portable Python (~12 MB)**; just answer Y
2. On page ① "Environment", click **Install dependencies** once (needs internet,
   once per machine), then continue when all checks are green
3. Follow the four web pages: Export → Clean → Publish

### 🤖 Let an AI set it up for you

Don't want to touch anything? Hand **`AGENT_GUIDE.md`** to any AI assistant
(Claude / ChatGPT / Copilot…) — it contains everything an agent needs to install,
launch, verify and operate the toolkit, including setup steps, an API map, the data
layout, and hard behavioral rules for the agent.

## 📁 Layout

```
启动工作台.cmd          the only entry point (double-click)
使用说明.txt            full user manual (Chinese)
AGENT_GUIDE.md          deployment & operation guide for AI agents (CN/EN)
config.json             data directory / port
app/                    backend, exporter, rewrite engine, web UI, requirements
数据/                   inbox / projects / publish packages / Edge profile (created at runtime)
```

## 🔒 Privacy & Security

- All data stays on the machine; no telemetry, no cloud dependencies
- `数据\浏览器资料\` holds your Goofish login session — **do not share that folder**
- The exporter only reads two endpoints of your own account; it never searches other
  people's listings, fakes devices, or bypasses captchas
- On slider captchas / risk control it stops and waits for a human

## ⚠️ Disclaimer

This tool is for managing **your own account's** listings only. Before using it:

- Xianyu/Goofish prohibits unauthorized automation and bulk migration; using this
  tool may violate the platform's terms and can lead to delisting, throttling or
  account suspension — **at your own risk**
- Comply with local laws; do not publish prohibited, infringing, or false listings
- Provided as-is; the author is not liable for account or data losses

## 📄 License

- ✅ **Allowed**: free for personal use — download, use and modify for your own migrations
- ❌ **Not allowed**: reselling, bundling for sale, republishing under another name,
  or redistributing with author attribution removed
- Provided as-is; the author is not liable for account or data losses

If you find it useful, a ⭐ Star is the best support.

---

*Distribution and licensing may change between releases — see the latest Release notes.*
