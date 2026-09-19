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
| **书库** | 藏书网格/侧栏列表、单选全选删除、回收站（恢复/清空）；**存储分工可视化**：顶部汇总条（本地 N 成品 X MB ｜ R2 N 对象 Y MB ｜ 差异）、每本书 R2 徽章（✓ 已同步 / ⚠ 缺·尺寸不符 / — 未配置 / ? 读不到）|
| **存储页** | 侧栏「▦ 存储 · STORAGE」（仅管理员）：**逐书三列对照**（本地 / R2 / 差异逐项）、**孤儿对象一键清理**（本地已不该有的远端副本）、容量汇总（工作区占用 / 磁盘 / R2 免费额度条）、**近 30 天容量趋势图**（只统计 trs 自己）|
| **查看** | 章节对照阅读（原文+译文，同步滚动）、markdown 渲染、图片查看、元数据 |
| **下载** | 5 格式成品一键下载；**成品走 Cloudflare R2 预签名 URL**（省 VPS 出网流量、PDF 不进浏览器内存），R2 不可用自动回退本地直连；下载时 **toast 明示本次走 R2 还是本地（含原因）**，管理员可一键「强制本地下载」排障（免重启）|
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

存储分工可视化：GET /api/books → 附 storage 汇总 + 每本书 r2{object_count,expected,missing,mismatch,synced}
                  后端 r2.snapshot()：一次列举 books/ 前缀分组比对，30s 缓存、短超时、失败静默降级

存储页/下载源（S2/S4）：GET /api/storage（逐书三列 + 孤儿 + 容量）· POST /api/storage/gc（清理孤儿，删前复核）
                        GET/POST /api/settings（force_local_download，改完免重启）

归档守护（S3+S6）：Hermes no_agent cron 每 30 分钟跑 web/r2_alert.py
                5 个条件（归档差异 / R2 读不到 / 磁盘>85% / R2 额度>80% / 采样断更>36h）
                异常持续 ≥30min → 合并成一条告警（stdout 原样投递 QQ）；正常 → stdout 为空 = 零打扰
                顺带每日写一行容量采样（work/.storage_history.jsonl，只统计 trs）
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
│   ├── r2.py              # R2 封装（上传/预签名/列举/删除 + snapshot 快照）
│   ├── r2_sync.py         # 成品幂等同步脚本（15 分钟 timer 调用）
│   ├── r2_alert.py        # 归档守护（差异 ≥30min 告警；`--selftest` 可自检）
│   ├── settings.py        # 运行期设置（force_local_download）
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

**改了 `main.py`/`app.js` 要重启生效**：服务是 `Restart=on-failure`，所以也可以不碰 `systemctl`——
用 `bash /root/scripts/restart_translate_web.sh`（SIGKILL 旧进程 → systemd 5s 内自愈 → 等新 PID + 健康检查，
实测 6 秒拉起；该脚本还会先跑 `py_compile` 与 `node --check`）。静态文件（`static/*`）无需重启，但**改了前端要提升 `index.html` 里的 `app.js?v=` 版本号**，否则浏览器吃缓存（问题 #27）。

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
8. 想核对「本地 / R2 到底同步了没有」？侧栏「**▦ 存储 · STORAGE**」（管理员）→ 逐书三列对照 + 孤儿对象清理 + 容量汇总 + **近 30 天容量趋势图**；**异常会自动推给你**（归档守护 cron：归档差异 / R2 读不到 / 磁盘 >85% / R2 额度 >80% / 采样断更，持续 ≥30 分钟才提醒，恢复正常再通知一次）

## 配置说明

| 项 | 位置 | 说明 |
|---|---|---|
| 凭据 | `/root/.translate-web-cred`（chmod 600，**仓库目录之外**）| **应用层** Basic 认证：`账号` / `密码` / `bcrypt` 三行；代码只读文件（`auth.py:load_admin_cred`），**哈希绝不进仓库**。轮换：`/root/scripts/rotate_admin_cred.py` + 重启服务；回归验证：`/root/scripts/verify_auth.py` |
| Caddy | `/etc/caddy/Caddyfile` | 双站反代（trs→8788 / :8080→8787），**只转发不做认证** |
| Cloudflare | 控制台 Origin Rules | Hostname→8080（**必须 Hostname 字段**） |
| cron | Hermes `ab9ef9653179` | 每 3 分钟，prompt 含「超时安全：output 落盘后下轮 record_only 续译」 |
| cron（归档守护）| Hermes `5a2841fafa9d`（`no_agent`）| 每 30 分钟跑 `/root/.hermes/scripts/translate_r2_alert.sh` → `web/r2_alert.py`；**5 个条件**（归档差异 / R2 读不到 / 磁盘>85% / R2 额度>80% / 采样断更>36h），**多条件合并成一条**；**正常时零输出=零打扰**，异常持续 ≥30min 才推送、恢复正常发一条「已恢复」；顺带每日采样 |
| LLM（网关） | Hermes config `model:` | 主通道 **deepseek 官方 / deepseek-v4-flash，reasoning medium**；fallback 首位 ark |
| LLM（D 引擎） | `web/translate_direct.py` 的 `PROVIDERS` | 首位 **deepseek 官方 flash**（key 走 `/root/.hermes/.env` 的 `DEEPSEEK_API_KEY`），**ark / amd 兜底**；⚠️ 官方为**付费**通道（单 chunk ≈2.2k tokens），省费可调顺序（问题 #29、v5.6）|
| R2 凭证（E 方案）| `/root/.translate-r2-cred`（chmod 600）| `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_BUCKET=translate-archive`；**留空则成品自动走本地直连**（不影响下载）。当前已归档 **12/12 成品 / 207.4 MB** |
| R2 同步 | systemd `translate-r2sync.timer`（**已启用**，15 分钟增量）| 幂等上传成品（同尺寸跳过）；手动跑：`book/.venv/bin/python web/r2_sync.py`。**判断归档是否完成要对账**：`/root/scripts/r2_reconcile.py`（列远端比尺寸）——`r2_sync.py --dry-run` **只列本地待传文件、不查远端**，别用它判归档（问题 #32）|
| 运行期设置 | `work/.web_settings.json` | `force_local_download`（下载强制走本地，排障用）；改法：存储页开关或 `POST /api/settings`，**改完立即生效、无需重启** |
| 容量采样 | `work/.storage_history.jsonl` | 守护脚本**每日首次运行**写一行（`trs_bytes` 只统计 trs 工作区 + R2 成品数/字节 + 下载签发数；保留 180 天）；手动补采样：`book/.venv/bin/python web/r2_alert.py --force-sample` |
| 下载计数 | `work/.web_stats.json` | `presign_count`（服务端自记的预签名签发次数，近似下载量；预签名是本地计算、不产生 R2 API 调用）|

## 已知问题与维护

- **空尾 chunk**：PDF 末尾空白页会生成 0 字节 chunk，已打 `source_empty` 补丁（manifest/merge_and_build）
- **本地成品文件不可删**：`books.py` 的 `_status()` 判 `done`、`_file_summary()` 出文件清单都依赖 `book.pdf/docx/epub` 的存在与大小——**R2 只是分发通道，不是备份**
- **R2 上传必须单分片串行**：本机出网仅 ~1Mbps（~110KB/s），boto3 默认 `max_concurrency=10` 会让 >8MB 的文件超时断连（问题 #31）——`web/r2.py` 已固定 `max_concurrency=1` + `use_threads=False` + `read_timeout=600`，**改上传逻辑时勿动这几个参数**
- **merge 勿用 `--cleanup`**：会删 chunk 文件，查看器对照 tab 依赖它们
- **孤儿对象只识别、不自动删**：存储页的清理按钮**每次都会重新核对远端**（本地存在同名成品一律拒绝删除），自动删远端副本的收益不值风险——要清就人工点一次（经验总结 #28）
- **归档守护有 30 分钟观察期**：异常持续 ≥30 分钟才推送 → 发现延迟最长约 60 分钟（属设计取舍：宁可晚，不要误报）；监护 **5 个条件**（归档差异 / R2 读不到 / 磁盘>85% / R2 额度>80% / 采样断更>36h），**多条件合并成一条消息**；想立刻确认状态就跑 `book/.venv/bin/python web/r2_alert.py --json`
- **磁盘告警是机器级、趋势只统计 trs**：`disk` 条件看的是整盘 40GB（盘满会连带 trs/scan 全挂）；存储页趋势图按用户要求**只画 trs 自己**（工作区 + R2 成品），不含 scan
- **公网看新版要认对 URL**：用户入口 `/` 是 `cf-cache-status: DYNAMIC`（不缓存）→ 部署即时可见；但 `/static/index.html` **直链**会被 CF 缓存（`max-age=14400`）→ 别拿它验版本（问题 #35）
- **内存**：3.8GB 服务器，勿同时跑 scan 重型解析 + 全书重译
- 完整坑位记录见 `迭代文档/问题记录.md`

## License

引擎部分遵循上游仓库 LICENSE（`book/LICENSE`）；网站部分为本项目原创。

---
POWERED BY translate-book · Hermes Agent · 2026
