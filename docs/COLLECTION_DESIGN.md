# 大规模采集设计：中断续采 / 分组异步 / 结构校验 / 增量更新

2026-11-22。背景：遍历规模 599 资质 × 17 地市 = 10,183 个市级组合（OVERFLOW 下钻区县另计），单机单进程跑不完、也跑不久，必须能随时中断、能分组并行、能周期更新。本文先盘点现状，再给设计。实施分 P1/P2/P3，见文末。

## 0. 现状盘点（已核对代码）

已经有的：

- **记录级断点**：每采完一家企业就原子写输出文件（`atomic_json_write`），state 同步更新；重跑时从输出文件重建「已完整」集合，跳过已采企业。Ctrl-C 有优雅退出（已采的都落盘）。
- **组合级断点**：每组合独立输出 + state 文件；state 的 `status=complete` 的组合在遍历时整段跳过；OVERFLOW 有明确状态，不冒充完成。
- **采集去重**：下钻区县组合预载父级已完整记录的信用代码，`[skip-dedup]` 不重开详情。
- **入库版本化**：`web/ingest/schema.sql` 是 SCD-2 结构——`company` + 8 张子表（qualification / registered_person / project / performance / misconduct / blacklist / punishment / change_log），每行带 `content_hash / valid_from / valid_to / is_current / first_seen_at / last_seen_at`，且有「每业务键至多一行 is_current」的部分唯一索引。`import_db.py` 的 `sync_company/sync_children` 已按 hash 对比：未变只更新 `last_seen_at`，变了才关旧版本、插新版本。另有 `crawl_snapshot` 存每次采集的原始 payload。
- **分组的雏形**：`--category / --city / --level / --qualification` 已可把遍历切成互不重叠的分片。

缺的（本文要补的）：

1. 中断支持只到「同进程重跑」：遍历队列在内存里，没有跨进程的任务台账、没有组合认领/锁，两个进程跑同一分片会互相覆盖同一组合的输出文件。
2. 没有结构校验：`schema.sql` 全是 `CREATE IF NOT EXISTS`——旧表已存在时静默跳过，表结构漂移（少列、少索引、类型不对）发现不了，导入会跑到一半才炸或写错。
3. 没有增量更新机制：平台数据变了只能全量重采重导，不知道哪些企业变了、哪些消失了。

## 1. 中断与续采设计

断点分三层，粒度从粗到细：

| 层 | 状态载体 | 恢复动作 |
|---|---|---|
| 组合 | 每组合 state 文件（已有） | `status=complete` 跳过；其余重跑 |
| 企业 | 组合输出文件（已有，原子写） | 重建已完整集合，跳过已采，残缺的重新采 |
| 任务 | **新增**：组合 state 增加认领字段 | 见下 |

新增设计：

- **组合认领（claim）**：state 增加 `claimed_by`（worker 标识）、`claimed_at`、`heartbeat_at`。worker 开跑一个组合前先写认领并周期性刷新心跳；遍历选组合时跳过「他人认领且心跳未过期（如 30 分钟）」的组合。进程崩溃 → 心跳过期 → 组合自动可被他人接手，不需要人工清状态。
- **组合文件锁**：每个组合输出旁加 `.lock` 文件（`O_EXCL` 创建），防两个 worker 同时写同一文件；锁随认领一起过期。
- **优雅停止**：除现有的 Ctrl-C 外，支持在数据目录放 `STOP.<worker>` 文件，worker 在每家企业之间检查，存在则收尾当前组合状态后退出——适合远程/计划停机。
- **进度总览**：新增 `--status` 只读命令：扫全部 state 文件，汇总 complete / overflow / running（心跳新鲜）/ stale / pending 计数和总企业数，随时能回答「采到哪了」。

## 2. 分组异步采集设计

原则：**不引入消息队列，用「分片 + 独立进程」实现并行**，与现有文件式状态天然契合。

- **分组维度**：按类别（4 组，大小悬殊：工程设计 396 / 建筑业 143 / 监理 34 / 勘察 26 个资质）或按地市（17 组），或类别×地市交叉。推荐按**地市**分组、组内按类别顺序跑：每组数据量相对均衡，且同组内父→子下钻去重是本地读，不跨组。
- **worker 隔离**：每个 worker 一个进程，独立 `--profile` 目录（Playwright 浏览器 profile 不能多进程共用，会锁死）、独立 ready-flag 文件、独立 STOP 文件；共享的只有 `collector/data/`（组合文件互不重叠，下钻读父级文件是只读，安全）和平台本身。
- **协调**：靠第 1 节的认领+心跳防撞车，不需要中心调度。启动命令形如：

```bash
python collector/collect.py --traverse --city 济南 --profile playwright_profile_jinan --worker jinan-1
python collector/collect.py --traverse --city 青岛 --profile playwright_profile_qingdao --worker qingdao-1
```

- **限速与验证码**：每 worker 保持现有 0.5–0.8 秒间隔；并行度建议 ≤4（平台是政务站，礼貌优先，且验证码是人工处理，一台机器同时弹多个验证码人也顾不过来）。任一 worker 遇验证码：该组合置 `needs_human` 状态、Bark 通知（已有），worker 跳过该组合继续下一个，不让整组停摆；人工处理后该组合回 pending。
- **跨机器扩展**（将来）：state 文件即台账，把 `collector/data/` 放共享盘或按组分发到多台机器、最后汇总文件入库即可；真要中心化再换 SQLite 任务表，接口不变（组合 = 任务行）。

## 3. 数据库结构校验设计

采集记录结构固定，所以「先验结构、再导入」做成导入前置门：

- **期望结构清单**：从 `schema.sql` 派生一份机器可读的期望清单（表名、列名+类型、部分唯一索引名），与 schema.sql 同源生成，不手写第二份。
- **`import_db.py --check-schema`**：连库后查 `information_schema` + `pg_indexes`，逐项对比，输出差异报告（缺表 / 缺列 / 类型不符 / 缺索引 / 多余不处理）。全对才返回 0；有差异非 0 退出，**导入默认先自动跑一遍 check，不通过拒绝导入**（`--force` 也不绕过结构错误，只留显式 `--migrate` 按 schema.sql 补齐——注意 `CREATE IF NOT EXISTS` 补不了已漂移的列，漂移要人工处理，工具只报告）。
- **结构版本表**：新增 `schema_meta(version int, applied_at)`，schema.sql 每次变更 version +1；check 同时校验 version 与代码期望一致，防止「表对但版本旧」。
- **记录结构校验**（导入前对 JSON 做）：每条记录必须含合法信用代码、9 个固定栏目键齐全、栏目为 `{rows: [...]}` 形状；不合格的整条隔离计数并报告，不混进正式导入（采集端 `record_is_complete` 是同一口径，入库端再验一遍兜底）。

## 4. 平台数据更新时的增量更新设计

分两层：**采集侧少采**（省时间）、**入库侧版本化**（已有，保证正确）。

### 4.1 采集侧：列表指纹 + 详情按需

- **列表指纹**：每组合刷新时先只翻列表页（不进详情），把列表行（信用代码 + 企业名称 + 法人 + 属地等列表可见字段）排序后算 hash，存进组合 state 的 `list_fingerprint`。与上次相同 → 判定该组合无变化，更新 `last_checked_at` 即结束（成本只有翻页）。
- **指纹变了**：列表里新增的企业 → 采详情；已消失的企业 → 记入组合的 `missing_codes`；其余企业先不动。
- **列表看不见的变化**（资质到期日、人员、业绩等只在详情里）：指纹法测不到。所以设**双节奏**：列表指纹刷新勤（如每周），全量详情刷新疏（如每月/每季），全量时靠入库 hash 对比——没变的企业入库只是 `last_seen_at`  bump，库不会膨胀出假版本。
- **消失处理**：企业在某组合消失 ≠ 注销（可能只是资质被摘）。规则：连续 2 个刷新周期在所有相关组合都缺席 → 在采集侧标记 `missing_since`；库里不删、不关版本（历史还在），网站层可按 `last_seen_at` 过滤展示。真注销判定需要人工或平台注销公告，不自动做。

### 4.2 入库侧：已具备，只需接上

- 重采的记录走现有 `import_db` 即可：`content_hash` 未变 → 只 bump `last_seen_at`；变了 → 旧行 `is_current=false, valid_to=now`，新行插入，历史版本完整保留；`crawl_snapshot` 按 (信用代码, 采集时间) 存原始 payload，可回溯任何一次采集的原文。
- 子表同理（`sync_children` 按 business_key + hash 做同样的开关版
...[truncated 1039 chars]
## 6. 实施状态（2026-11-22，Hu 指示 P1/P2/P3 逐个做）

- **P1a 已落地**（284c1b0b）：`web/ingest/schema_spec.py` 从 schema.sql 派生期望结构；`import_db.py --check-schema / --migrate`；正式导入先验结构、不过拒绝；记录结构校验隔离不合格记录（counts.invalid）。schema.sql 增 `schema_meta` 表，SCHEMA_VERSION=1。
- **P1b 已落地**（99bf2bdf）：组合认领 `claimed_by/claimed_ts/heartbeat_ts`（TTL 1800 秒，过期可接手）、`<state>.lock` 独占锁、STOP / STOP.<worker> 优雅停止、`--status` 总览。
- **P2 已落地**（3559f93c）：`--worker` 自动隔离 profile（`playwright_profile_<worker>`）与 ready-flag（`start_collect.<worker>.flag`），显式参数优先；分组启动方式见 collector/README。一点修正：验证码现行逻辑是组合内等人工处理（原采集设计），未改成 needs_human 跳过——分组时一个 worker 卡验证码只卡自己分片，其余 worker 不受影响；若要「跳过该组合继续下一个」需另行确认改等待逻辑。
- **P3 已落地**：`--refresh` 列表指纹快速路径（未变跳过详情并更新 last_checked_at）、变化时记录 `missing_codes/missing_since`（只标记不删）、`filters.diff_qualifications / diff_counties` 字典对比。详情内部变化的兜底仍是不带 --refresh 的全量重采 + 入库 hash 版本化（已具备）。
- 测试：全量 24/24（filters / dedup+指纹 / claims / schema_spec）。全部为代码与单测验证，采集未实跑。
