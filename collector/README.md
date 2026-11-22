# 采集系统 collector/

从 [全国建筑市场监管公共服务平台](https://jzsc.mohurd.gov.cn/data/company) 读取已公开的企业信息。只使用页面上的公开筛选和详情，不绕过验证码。出现验证时脚本会停住，等你在可见浏览器里处理完再继续。

每家企业保存基础信息，以及这些栏目里的表格：企业资质资格、注册人员、工程项目、业绩技术指标、不良行为、黑名单记录、失信联合惩戒记录、变更记录。每条记录带 `source_query {qualification, province, city, county}`，标明它是在哪个检索组合下采到的。

## 安装

```bash
python3 -m venv .venv && source .venv/bin/activate   # 在仓库根目录
pip install -r collector/requirements.txt
playwright install chromium
```

## 运行：历史单组合（默认）

```bash
python collector/collect.py
# 资质：建筑工程施工总承包一级；属地：山东（省级）
# 输出：collector/data/mohurd_shandong_level1.json（356 条已采，断点续跑）
```

脚本会打开可见的 Chrome。若自动设置筛选失败，请在页面上手动选好条件并点击查询。结果列表里的企业注册属地需要都包含当前组合的属地，才会开始逐条打开详情。

## 运行：单个组合

```bash
python collector/collect.py --qualification <资质原文> --city 济南
python collector/collect.py --qualification <资质原文> --city 济南 --county 历下区
```

每个组合独立输出 + 独立 state 文件，放在 `collector/data/`，文件名由组合生成。

## 运行：全量遍历（山东）

```bash
python collector/collect.py --plan-only   # 先看计划：几个资质 x 16 地市，不打开浏览器
python collector/collect.py --traverse   # 正式遍历
```

遍历规则（详见 `docs/COLLECTOR.md`）：

- 组合 = 资质（`collector/qualifications.json`，从平台对话框逐字枚举）× 山东 16 地市。
- 平台单组合最多展示 30 页 × 15 条 = **450 条**。跑满 30 页的组合 state 会标记 `"status": "OVERFLOW"`，**不算采全**；遍历时自动下钻到该市的区县（`collector/shandong_counties.json`）逐个再跑。
- 区县级仍跑满 30 页会打印 `[overflow-county]`，留人工决定，不静默截断。
- 某市 OVERFLOW 但区县数据尚未枚举时，打印 `[needs-county-data]` 并保留标记，不冒充完成。
- 已 `complete` 的组合再次遍历会跳过；中断重跑按各组合 state 断点续跑。

## 其他

- 进入详情页、切换栏目以及每次自动操作之间等待 0.5～0.8 秒。
- 验证码出现时通过 Bark 提醒一次：密钥放在仓库根目录的 `bark.url`（不提交），或环境变量 `BARK_URL`。
- 浏览器用户数据在仓库根目录 `playwright_profile/`（不提交）。
- `data/legacy/` 是早期实验数据（见 `docs/HANDOVER.md`），不要当正式数据导入。
