# 全国建筑市场监管公共服务平台 — 山东企业采集与查询

从 [全国建筑市场监管公共服务平台](https://jzsc.mohurd.gov.cn/data/company) 读取已公开的企业信息（只做山东），入库 Postgres，并提供查询网站。只使用页面上的公开筛选和详情，不绕过验证码；出现验证时脚本会停住，等人在可见浏览器里处理完再继续。

## 两个系统

| 系统 | 目录 | 说明 |
|---|---|---|
| 数据采集 | `collector/` | Playwright 可见浏览器采集；资质 × 属地组合遍历，单组合 450 条上限时自动细化下钻。见 `collector/README.md` |
| 网站 | `web/` | FastAPI 查询站 + 入库（`web/ingest/`）。见 `web/README.md` |
| 共用 | `shared/` | 配置读取与 SSH 隧道（网站与入库共用） |
| 交接文档 | `docs/` | `docs/HANDOVER.md` 总览、`docs/COLLECTOR.md` 遍历规则、`docs/WEB.md` 网站说明 |

**接手的人先读 [`docs/HANDOVER.md`](docs/HANDOVER.md)。**

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate

# 采集（单组合，默认：建筑工程施工总承包一级 × 山东）
pip install -r collector/requirements.txt && playwright install chromium
python collector/collect.py --plan-only    # 先看遍历计划（不打开浏览器）
python collector/collect.py                # 或 --traverse 跑全部资质 × 山东16地市

# 网站
pip install -r web/requirements.txt
cp db.conf.example db.conf                 # 填真实配置，db.conf 不提交
python web/ingest/import_db.py             # 入库
python web/app.py                          # http://127.0.0.1:8000
```

仓库不保存采集数据：原 356 条文件是测试数据，已于 2026-11-22 按 Hu 指示删除。采集产物生成在 `collector/data/`（默认组合的文件名仍为 `mohurd_shandong_level1.json`）。
