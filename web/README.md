# 网站系统 web/

把采集到的企业 JSON 入库 Postgres，并提供查询网站。

## 安装

```bash
python3 -m venv .venv && source .venv/bin/activate   # 在仓库根目录
pip install -r web/requirements.txt
cp db.conf.example db.conf   # 填真实密码；db.conf 不提交
```

`db.conf` 在仓库根目录，网站和入库共用（读取代码在 `shared/`）。配置里有 `ssh_host` 时，会经 SSH 隧道连 Altare 上的 Postgres。

## 入库

```bash
python web/ingest/import_db.py                                   # 默认导入 collector/data/mohurd_shandong_level1.json
python web/ingest/import_db.py --json <文件1> <文件2>            # 指定多个组合文件
python web/ingest/import_db.py --data-dir collector/data         # 导入该目录下全部 mohurd_*.json（不含 legacy/）
```

入库语义：每家企业及 8 张子表各保留一条 current 记录；内容变了就关旧行、开新行（`is_current` + `content_hash`）；同一文件重跑只刷新 `last_seen_at`。同一企业在多个组合出现时按统一社会信用代码去重，只有一条 current。表结构在 `web/ingest/schema.sql`，导入时自动建表。

## 启动网站

```bash
python web/app.py            # http://127.0.0.1:8000
# 或在仓库根目录： uvicorn web.app:app --port 8000
```

- `/` 按企业名、信用代码、法人、属地、资质、注册人员、工程项目等条件搜索（分页 20/页）。
- `/company/{credit_code}` 企业详情：资质、人员、项目、业绩、不良行为、黑名单、失信惩戒、变更记录。
