# 交接总览

这个仓库做一件事：把全国建筑市场监管公共服务平台上公开的山东建筑企业信息采下来，入库，再提供一个查询网站。

## 架构

```
平台网站 (jzsc.mohurd.gov.cn)
        │  公开筛选 + 详情页，可见浏览器，验证码人工处理
        ▼
collector/  采集系统（Playwright）
        │  collector/data/*.json（每组合一份 + .state.json）
        ▼
web/ingest/ 入库（import_db.py + schema.sql）
        │  Postgres：company + 8 张子表，is_current 版本化
        ▼
web/        查询网站（FastAPI，app.py）
shared/     网站与入库共用的 load_config / SshTunnel
```

两个系统可独立运行：采集只需要 `collector/requirements.txt`，网站只需要 `web/requirements.txt`。

## 目录

| 路径 | 内容 |
|---|---|
| `collector/collect.py` | 采集主程序（原 `spider/spider.py`） |
| `collector/filters.py` | 遍历/细化纯逻辑：资质×地市组合、450 上限判断、OVERFLOW 下钻 |
| `collector/data/` | 采集产物；`legacy/` 是早期实验数据，不要导入 |
| `web/app.py` | 查询网站 |
| `web/ingest/` | 入库程序与表结构 |
| `web/templates/` `web/static/` | 网站模板与样式 |
| `shared/` | 配置读取与 SSH 隧道 |
| `docs/` | 本文件 + `COLLECTOR.md`（遍历规则）+ `WEB.md` |
| `tests/` | `filters.py` 的单元测试：`python -m pytest tests/ -q` |
| `misc/tsst.html` | 与本项目无关的旧页面，仅存档 |
| `spider.code-workspace` | 旧 VS Code 工作区文件，可删 |

## 配置清单（都不提交）

- `db.conf`（仓库根目录）：Postgres 连接，模板 `db.conf.example`；含 `ssh_host` 时经 SSH 隧道连 Altare。
- `bark.url`（仓库根目录）或环境变量 `BARK_URL`：验证码 Bark 提醒地址。
- `playwright_profile/`（仓库根目录）：采集浏览器的用户数据。

## 数据说明（重要）

- 正式数据：`collector/data/mohurd_shandong_level1.json` — 建筑工程施工总承包一级 × 山东，356 条完整记录（2026-11-21），未触 450 上限，是采全的。与它成对的 `.state.json` 必须一起保留，断点续跑靠它。
- `collector/data/legacy/` 两份早期实验文件**不是重复文件**，内容不同，不要合并、不要导入：
  - `mohurd_shandong_construction_level_74.json`：74 条，含重复企业与非山东企业（早期未加属地校验的试验跑）。
  - `mohurd_shandong_construction_level_root3.json`：上面那份的前 3 条子集。

## 接手顺序

1. 读本文件 → `collector/README.md` 跑通单组合采集（或 `--plan-only` 先看遍历计划）。
2. 读 `docs/COLLECTOR.md` 了解 450 上限与下钻规则，再跑 `--traverse`。
3. 读 `web/README.md` 做入库与启动网站。
4. 改遍历逻辑前先跑 `tests/`。
