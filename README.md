# 翻译文库 · Translation Archive

基于 **translate-book skill**（`/root/translate/book`）的个人线上翻译网站（`/root/translate/web`）。
上传 PDF/DOCX/EPUB → 自动分块 → LLM 翻译 → 5 格式成品（PDF/DOCX/EPUB/HTML/Markdown），全程浏览器操作、后台自动推进。

- **在线访问**：`https://trs.andrew-li.top`（**游客可直接参观**；上传/删除/下载/改术语表需登录）
- **引擎**：`book/` 目录（上游 `Andrew-li777/translate-book` 仓库移植 + 适配）
- **驱动**：Hermes cron 消费者每 3 分钟自动翻译

> 项目文档见 `迭代文档/`（版本演进、问题记录、经验总结、架构、优化方向、未来）。

---

## 功能特性

| 模块 | 能力 |
|---|---|
| **上传** | PDF/DOCX/EPUB（≤100MB），书名/目标语言可选，二级模态框界面 |
| **任务** | 队列管理（状态徽章/进度条/取消/单选全选删除），SSE 秒级实时进度，点击展开详情（失败给出具体原因） |
| **自动翻译** | Hermes cron 每 3 分钟增量翻译（3 块并行），断点续译、绝不重复翻译，全书完成自动合并 |
| **书库** | 藏书网格/侧栏列表、单选全选删除、回收站（恢复/清空） |
| **查看** | 章节对照阅读（原文+译文，同步滚动）、markdown 渲染、图片查看、元数据 |
| **下载** | 5 格式成品一键下载；**成品走 Cloudflare R2 预签名 URL**（省 VPS 出网流量、PDF 不进浏览器内存），R2 不可用自动回退本地直连 |
| **术语表** | 每书独立术语表，**保存即触发全书重译**（专有名词可控） |
| **搜索** | 全文搜索（译文/原文/全部），命中直接跳转对照阅读 |
| **侧栏** | 可收起（localStorage 记忆） |
| **权限** | 三档鉴权：**游客**（只读参观：书库/对照/搜索/渲染）、**管理员**（全功能）、凭据错误一律 401；游客搜索限频 10 次/分 |

## 系统架构

```
浏览器 → Cloudflare(HTTPS) → Origin Rule(Hostname→8080) → Caddy(按 Host 分流) → :8788
                                                                    └→ :8787(scan)

成品下载（E 方案）：前端 → GET /api/download/{name}/book.pdf → R2 预签名 URL → 顶级导航直下 R2
                    （R2 未配置/失败 → 自动回退本地 /api/books/{name}/download/{path}）
```

- **translate-web**：FastAPI 单页应用（systemd `translate-web.service`，自启+自愈）
- **translate-book**：翻译引擎（Calibre + Pandoc + `.venv`），纯函数流水线
- **Hermes cron**：`ab9ef9653179` 每 3 分钟，agent 模式加载 translate-book skill，`local` 投递
- 详见 `迭代文档/架构与流程图.md`

## 目录结构

```
/root/translate/
├── book/                  # 翻译引擎（上游仓库 + 适配补丁）
│   ├── SKILL.md           # skill 蓝图
│   ├── scripts/           # 11 个流水线脚本
│   └── .venv/             # Python 虚拟环境
├── web/                   # 在线网站
│   ├── main.py            # FastAPI 应用（全部 API）
│   ├── books.py           # 书库逻辑
│   ├── jobs.py            # 任务队列
│   ├── consumer.py        # cron 消费者辅助（pick/finish/request-merge/status）
│   ├── config.py          # 常量
│   └── static/            # 前端（index.html + app.js）
├── work/                  # 翻译工作区（运行数据，不入库）
│   ├── {书名}_temp/       # 每本书的 chunk/译文/成品
│   └── jobs/{id}/         # 任务元数据 + 进度
└── 迭代文档/               # 项目文档
```

## 快速开始（服务器已有部署）

### 前置依赖

- Python 3.11 + venv：`book/.venv`（pypandoc, beautifulsoup4, fastapi, uvicorn, python-multipart, Markdown）
- Calibre（`ebook-convert`）+ Pandoc（apt 安装）
- Caddy 2.x（反代）、systemd

### 启动服务

```bash
systemctl start translate-web      # 或 enable --now 开机自启
curl -s http://127.0.0.1:8788/api/books   # 健康检查
```

### 查看进度/日志

```bash
journalctl -u translate-web -f     # 服务日志
tail -f /root/translate/work/*/progress.json  # 任务进度
cat /root/translate/work/jobs/*/job.json     # 任务状态
```

### 手动触发翻译（cron 停用时的兜底）

```bash
# 从 Hermes 会话执行：
# 1. /root/translate/web/consumer.py pick    → 取本批 3 块
# 2. delegate_task 翻译（加载 translate-book skill）
# 3. /root/translate/web/consumer.py finish <job_id> <chunk...>
```

## 使用流程

1. 打开 `https://trs.andrew-li.top` —— **默认以游客身份直接浏览**（书库/对照阅读/搜索都可用）
2. 要做管理操作（上传/删除/下载/改术语表）？点顶栏 **`游客 · GUEST`** 徽章输入凭据（`/root/.translate-web-cred`），或直接在触发时弹出的登录框里输入
3. 侧栏「＋ 上传新书」→ 选文件 → 开始上传并转换
4. 任务卡片显示 converting → ready（SSE 实时）
5. **无需任何操作**：cron 自动翻译（每 3 分钟 3 块），书库出现后即可浏览/对照
6. 全书完成后自动合并 → 详情页下载 5 格式成品
7. 想改专有名词译法？详情页「术语表」tab 编辑保存 → 自动触发重译

## 配置说明

| 项 | 位置 | 说明 |
|---|---|---|
| 凭据 | `/root/.translate-web-cred`（chmod 600，**仓库目录之外**）| **应用层** Basic 认证：`账号` / `密码` / `bcrypt` 三行；代码只读文件（`auth.py:load_admin_cred`），**哈希绝不进仓库**。轮换：`/root/scripts/rotate_admin_cred.py` + 重启服务；回归验证：`/root/scripts/verify_auth.py` |
| Caddy | `/etc/caddy/Caddyfile` | 双站反代（trs→8788 / :8080→8787），**只转发不做认证** |
| Cloudflare | 控制台 Origin Rules | Hostname→8080（**必须 Hostname 字段**） |
| cron | Hermes `ab9ef9653179` | 每 3 分钟，prompt 含「超时安全：output 落盘后下轮 record_only 续译」 |
| LLM（网关） | Hermes config `model:` | 主通道 **deepseek 官方 / deepseek-v4-flash，reasoning medium**；fallback 首位 ark |
| LLM（D 引擎） | `web/translate_direct.py` 的 `PROVIDERS` | ⚠️ 仍为 `[amd, ark]`——两跳当前均不可用（问题 #29），待补 deepseek 官方 |
| R2 凭证（E 方案）| `/root/.translate-r2-cred`（chmod 600）| `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_BUCKET`；**留空则成品自动走本地直连**（不影响下载）|
| R2 同步 | systemd `translate-r2sync.timer` | 每 15 分钟幂等上传成品；手动：`book/.venv/bin/python web/r2_sync.py [--dry-run]` |

## 已知问题与维护

- **空尾 chunk**：PDF 末尾空白页会生成 0 字节 chunk，已打 `source_empty` 补丁（manifest/merge_and_build）
- **本地成品文件不可删**：`books.py` 的 `_status()` 判 `done`、`_file_summary()` 出文件清单都依赖 `book.pdf/docx/epub` 的存在与大小——**R2 只是分发通道，不是备份**
- **merge 勿用 `--cleanup`**：会删 chunk 文件，查看器对照 tab 依赖它们
- **内存**：3.8GB 服务器，勿同时跑 scan 重型解析 + 全书重译
- 完整坑位记录见 `迭代文档/问题记录.md`

## License

引擎部分遵循上游仓库 LICENSE（`book/LICENSE`）；网站部分为本项目原创。

---
POWERED BY translate-book · Hermes Agent · 2026
