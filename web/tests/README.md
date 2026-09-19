# web/tests —— translate-web 回归套件（P1-1）

零新依赖：stdlib `unittest` + `unittest.mock`（不需要 pytest / httpx）。

## 运行

```bash
cd /root/translate/web
/root/translate/book/.venv/bin/python -m unittest discover -s tests -v
```

或一键：

```bash
bash tests/run.sh
```

## 覆盖（对应《历史迭代审计 v5.7–v5.9》的验收面）

| 文件 | 覆盖 |
|---|---|
| `test_guest_rules.py` | 游客只读白名单（403 判定：settings/storage/gc/trash/download） |
| `test_auth_middleware.py` | AuthMiddleware 端到端（游客 403 / 错误凭据 401 / 管理员放行 / 限频 429 / 校验缓存「只缓存成功」/ 凭据文件 fail-closed） |
| `test_r2_gc.py` | GC 三守卫（路径非法 / 本地存在同名成品 / 远端已无）+ 删前强制刷新 + 远端不可达整体放弃 + 输入上限 |
| `test_r2_degrade.py` | R2 未配置全链路静默降级（snapshot/storage_view/upload/presign）+ 守护 noconfig 静默 |
| `test_render_cache.py` | 渲染缓存：命中 / mtime 失效 / LRU 条数上限 / 字节预算 / 文件缺失直通 |
| `test_r2_alert.py` | 守护状态机：判定边界（磁盘 84.9/85.1、额度 79/81、断更 35h/37h）、观察期、6h 去抖、条件变化重开、恢复通知、多条件合并；`--selftest` 子进程端到端 |

## 约定（改代码时请遵守）

1. **绝不触碰生产状态**：一切用临时目录 / 隔离状态文件（`--state`/`--history`）/ mock；
   `test_r2_alert.py` 的子进程用例只对 R2 做**只读**列举，不写任何生产文件。
2. **改了哪个行为，就在对应文件补一个用例**（并保持原有用例全绿）。
3. 新增依赖尽量为零：能 mock 就不真连网、能 stdlib 就不装包。
4. 这是审计中「一票否决项（测试零入库）」的解药——**改动前先跑一遍**再动手。
