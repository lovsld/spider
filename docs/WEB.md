# 网站系统说明

组成：

- `web/app.py` — FastAPI 查询站：`/` 搜索（企业名/信用代码/法人/属地/资质/注册人员/工程项目/资质异常/资质到期区间），`/company/{credit_code}` 详情（8 个栏目）。
- `web/templates/`、`web/static/` — Jinja2 模板与样式。
- `web/ingest/import_db.py` + `web/ingest/schema.sql` — 采集 JSON 入库 Postgres（用法见 `web/README.md`）。
- `shared/` — `load_config`（读根目录 `db.conf`）与 `SshTunnel`（Altare SSH 隧道），网站与入库共用。

数据流：`collector/data/*.json` → `import_db.py` → Postgres（company + 8 张子表，`is_current` 版本化）→ `app.py` 查询。

注意：

- `db.conf` 在仓库根目录，不提交；模板是 `db.conf.example`（含 `ssh_host=Altare` 时走隧道，本地端口从 15432 起找空闲口）。
- 网站启动时会建连接池（min 1 / max 4）；数据库连不上时启动即失败，先确认 `db.conf` 与隧道/网络。
- 同一企业在多个采集组合中出现时，入库按统一社会信用代码去重，只保留一条 current 记录；子表按各自 business_key 同理。
