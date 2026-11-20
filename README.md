# 全国建筑市场监管公共服务平台采集

从 [全国建筑市场监管公共服务平台](https://jzsc.mohurd.gov.cn/data/company) 读取已公开的企业信息。只使用页面上的公开筛选和详情，不绕过验证码。出现验证时，脚本会停住，等你在可见浏览器里处理完再继续。

当前筛选条件：

- 资质名称：建筑工程施工总承包一级
- 企业注册属地：山东

每家企业保存基础信息，以及这些栏目里的表格：企业资质资格、注册人员、工程项目、业绩技术指标、不良行为、黑名单记录、失信联合惩戒记录、变更记录。资质表有合并单元格，同一证书号下的多条资质会带上对应的证书号、发证日期、有效期和发证机关。

进入详情页、切换栏目以及每次自动操作之间会等待 0.5～0.8 秒。出现验证码时会通过 Bark 提醒一次，密钥放在仓库根目录的 `bark.url`（不提交），也可以用环境变量 `BARK_URL`。

## 运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r spider/requirements.txt
playwright install chromium
cd spider
python spider.py
```

脚本会打开可见的 Chrome。若自动设置筛选失败，请在页面上手动选好上述条件并点击查询。结果列表里的企业注册属地需要都包含「山东」，才会开始逐条打开详情。

已完成的企业写入 `spider/mohurd_shandong_level1.json`，进度写入同目录的 `mohurd_shandong_level1.state.json`。中断后再次运行会跳过已经采集完整的企业。平台列表最多 30 页。

常用参数：

```bash
python spider.py --max-pages 30
```

浏览器用户数据放在项目下的 `playwright_profile/`，该目录不会提交到仓库。
