# shared/

网站系统内部共用的最小代码集，采集系统不用。

- `config.py` — `load_config(path)`：读仓库根目录的 `db.conf`（格式见根目录 `db.conf.example`）。
- `ssh_tunnel.py` — `SshTunnel`：当 Postgres 的内网地址（Altare）不可直达时，经 SSH 隧道连接。`web/app.py` 和 `web/ingest/import_db.py` 都用它。

不要往这里放业务逻辑；两边都要用的纯配置/连接代码才进来。
