# 检索（线索）系统使用与部署

业务设计见 `docs/LEADS_DESIGN.md`。系统组成：

| 文件 | 作用 |
|---|---|
| `web/crm.py` | 团队 Web 应用（登录、今日待办、公海接单、企业拨打页、团队总览、后台），端口 8001 |
| `web/crm_core.py` | 业务规则与数据库操作：线索生成/回收、接单（上限+老客户优先）、通话结果状态流转、配置 |
| `web/leads_jobs.py` | 定时任务 CLI：generate / recycle / fetch-phones / create-user |
| `web/qichacha.py` | 企查查开放 API 客户端（号码拉取） |
| `web/ingest/schema.sql` | 含 CRM 表（app_user / lead / contact_record / company_phone / do_not_call / system_config 等），SCHEMA_VERSION=2 |

## 首次部署

```bash
pip install -r web/requirements.txt
python web/ingest/import_db.py --migrate          # 建表并登记结构版本 2
python web/leads_jobs.py create-user admin --role admin   # 创建管理员（密码交互输入）
python web/crm.py                                   # 启动，http://<host>:8001/crm/login
```

管理员登录后在「后台」页：确认业务参数（默认值：未联系回收 3 天、持有上限 100、软拒绝冷却 90 天、未接通释放 5 次、犹豫逾期回收 14 天、老客户优先 7 天）、新建成员账号、点「生成/刷新线索」。

## 日常与定时任务

```cron
30 6 * * *  cd /path/to/spider && python web/leads_jobs.py all
0 7 * * *   cd /path/to/spider && QICHACHA_KEY=xxx QICHACHA_SECRET=yyy python web/leads_jobs.py fetch-phones --limit 200
```

- `generate`：按到期窗口（已过期回溯 365 天 / ≤30 天 / ≤90 天）从当前资质数据生成与刷新公海线索；资质被续期或消失的线索自动关闭；软拒绝冷却到期的自动回公海；成交过的企业再到期时新线索带「老客户」标记并在优先期内只许原成交人接。
- `recycle`：接单 3 天未联系、犹豫回访逾期 14 天的线索自动回公海。
- `fetch-phones`：给尚无可用号码的企业（有活跃线索的优先）逐个查企查查并写入号码库；凭证只走环境变量 `QICHACHA_KEY` / `QICHACHA_SECRET`；若会员订阅的是专门的联系方式类接口，用 `QICHACHA_API_PATH` 指定接口路径（默认 `ECIInfoVerify/GetInfo`），号码字段提取是宽松匹配（Tel/Phone/Mobile 等），首次上线请先用 `--limit 5` 核对拉回的号码是否正确。

## 成员动线

今日待办（逾期 → 今日回访/重拨 → 新接未联系）→ 公海补货接单 → 企业页逐个资质记录通话结果（未接通/号码无效/软拒绝/勿扰/犹豫必填回访日/成交填产品金额）→ 随手补录号码并标记有效性（团队共享）。

## 验证状态（2026-11-23）

纯逻辑单测覆盖：到期日解析、窗口分层、结果状态流转、回收判定、号码规范化、密码哈希、企查查签名与号码提取（假传输层）。Web 应用已在本机 venv 验证可导入、17 条路由注册、登录页 200、全部 7 个模板可渲染。**未做**：连真实 Postgres 的端到端（本机无库）、企查查真实 Key 调用（需 Hu 的会员凭证与订阅接口确认）。
