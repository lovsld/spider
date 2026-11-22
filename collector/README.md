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
# 输出：collector/data/mohurd_shandong_level1.json（中断重跑会断点续跑）
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
python collector/collect.py --plan-only              # 先看计划：599 资质 x 137 区县，不打开浏览器
python collector/collect.py --plan-only --category D # 只看建筑业企业资质的计划
python collector/collect.py --traverse               # 正式遍历（默认按区县：82,063 个区县级组合）
python collector/collect.py --traverse --category 工程设计  # 只遍历一个类别（或用代码前缀 A/B/D/E）
python collector/collect.py --traverse --level city  # 旧市级模式（备查市直登记企业，日常不用）
```

遍历规则（详见 `docs/COLLECTOR.md`）：

- 组合 = 资质（`collector/qualifications.json`，平台字典 599 条，带代码与类别）× 山东 137 个区县（平台区划字典，17 市含莱芜）。区县按注册属地互不重叠，同一企业对同一资质最多采一次，不重复。
- 每个组合按其类别点平台的资质类别（建筑业企业资质 → 界面「建筑业企业」），再在对话框里按名称选资质；每条记录的 `source_query` 带资质代码与类别。
- 结果属地校验用区划字典的全名（如 `山东省-济南市-历下区`），避免同名区县（市中区等）跨市误判。
- 平台单组合最多展示 30 页 × 15 条 = **450 条**。区县级组合跑满 30 页 state 会标记 `"status": "OVERFLOW"`，**不算采全**，打印 `[overflow-county]` 留人工决定，不静默截断。（市级模式下 OVERFLOW 才下钻区县。）
- 已 `complete` 的组合再次遍历会跳过；中断重跑按各组合 state 断点续跑。

## 其他

- 进入详情页、切换栏目以及每次自动操作之间等待 0.5～0.8 秒。
- 验证码出现时通过 Bark 提醒一次：密钥放在仓库根目录的 `bark.url`（不提交），或环境变量 `BARK_URL`。
- 浏览器用户数据在仓库根目录 `playwright_profile/`（不提交）。
- `data/legacy/` 是早期实验数据（见 `docs/HANDOVER.md`），不要当正式数据导入。
