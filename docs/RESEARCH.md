# 开源调研与实现取舍

核验日期：2026-10-30。通过 GitHub 项目页面、API、README 与相关解析器源码核实。热度不作为是否可稳定获取数据的保证。

| 项目 | 许可证 | 适用点 | 本系统的取舍 |
| --- | --- | --- | --- |
| [Usagi-org/ai-goofish-monitor](https://github.com/Usagi-org/ai-goofish-monitor) | MIT | Playwright、任务管理、登录态、价格快照、SQLite | 主要架构参考；从找低价商品扩展为硬件样本市场的历史研究 |
| [superboyyy/xianyu_spider](https://github.com/superboyyy/xianyu_spider) | 核验时未发现许可证 | FastAPI、搜索过滤、去重、登录 | 仅调研，不直接复制代码 |
| [shaxiu/XianyuAutoAgent](https://github.com/shaxiu/XianyuAutoAgent) | GPL-3.0 | 卖家客服、会话管理 | 与市场研究目标差异大，未引入其代码或客服功能 |
| [GitLin123/xianyu_python](https://github.com/GitLin123/xianyu_python) | MIT | 私有登录态、任务持久化、采集错误处理 | 项目体量较小，作为补充参考 |

本仓库原创实现业务层、界面、存储与采集适配器；没有整仓复制第三方代码，也不需要大模型 API 密钥。

## 从项目中学到什么

1. **先保证样本与历史。** 商品 ID 去重，商品与观察快照分开；相同商品重复采集不算新增发布。
2. **采集与统计分离。** 即使平台暂不可访问，已有数据仍可浏览、分析、导出，也可通过标准格式导入第三方采集结果。
3. **硬件是不同规格的市场。** 显卡型号后缀、显存与内存容量/频率/套条影响可比性，应单独分组。规则未识别或标题缺规格时不能假装精确识别。
4. **状态必须有证据。** 搜索列表不包含完整订单数据。搜索中消失、卖家历史已售数、想要人数都不能推断为该商品成交。
5. **失效要可见。** 登录失效、验证码、结果 JSON 变动应显示错误，不能当作成功抓到 0 件。

## 已核实的页面响应结构

参考上游 [parsers.py](https://github.com/Usagi-org/ai-goofish-monitor/blob/master/src/parsers.py)、[scraper.py](https://github.com/Usagi-org/ai-goofish-monitor/blob/master/src/scraper.py)、[config.py](https://github.com/Usagi-org/ai-goofish-monitor/blob/master/src/config.py)。这是当次研究得到的适配信息，平台可随时变化。

搜索响应路径 `data.resultList[].data.item.main.exContent` 提供 `itemId`、`title` 与 `price` 文本片段，`main.clickParam.args.publishTime` 可能提供毫秒时间戳。上游监听网页自身请求 `mtop.taobao.idlemtopsearch.pc.search`。本系统也只监听正常页面响应，不自行构造签名请求。

上游用户主页列表解析器将 `cardData.itemStatus` 的 `0/1` 解释为在售/已售，但这不能推广成所有接口的通用状态协议。本系统不猜测详情接口数字状态，也不以任意页面文本中出现“已售”判定当前商品售出。详情复查仅尝试读取商品顶部明确状态组件；未识别则记为未知。该适配器仍需要用用户自己的有效登录进行真实端到端验证。

## 统计边界

- 搜索是有排名、分页和账号差异的抽样，不是闲鱼全市场全量。
- 初次采集只能记为首次发现。即使商品昨天发布，今天首次采到也只是今天新增观测。
- 同一首见窗口的明确售出商品数 / 该窗口首次观测商品数，称为**样本售出占比**。不同首见时间有不同跟踪长度，比较时应考虑观察时长。
- 当日首次确认售出 / 当日新增发现，是另一种流量比，可能大于 100%，不能叫成交转化率。本版展示两条每日数量曲线，不把这个商当转化率。
- 卖家标记已售只表示页面证据，不能验证支付完成、最终议价、退款或真实成交金额。

## 后续优先级

1. 使用实际账号逐项核验搜索和详情页面，建立脱敏 fixture，维护适配器版本。
2. 扩展卖家主页商品状态验证，并绑定商品 ID；仍不能当作订单成交证明。
3. 增加固定 7/14/30 日观察期的 cohort 对比和可量化的采样覆盖记录。
4. 扩展规格规则、地区/成色/保修/卖家类型，做更细分的价格比较。
5. 真有多设备或多用户需求再引入身份验证、PostgreSQL 与独立任务队列。
