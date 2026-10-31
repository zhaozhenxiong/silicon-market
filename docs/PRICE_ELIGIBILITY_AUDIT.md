# 挂牌价格资格审计

`app/price_audit.py` 对原始挂牌观察增加独立的资格判断，不重写 `items/observations` 的标题、价格、状态或观测时间，也不除以数量猜单价。Service 总览、列表、价格走势以及指数输入统一使用“原始 excluded OR 当前审计排除”作为有效样本条件。被剔除的原商品仍可通过显示已排除样本查看，返回具体 `price_audit_flags`、版本及原始质量标记。

当前规则 `listing-price-eligibility-v2` 主要剔除明确售卖的主机/组合配置、批量且单位不明的报价、套条型号却报单条/单个价格的冲突。不会因价格高低猜测商品类型；拆机来源、用途/兼容说明、明确仅卖CPU及已明确单件或单套报价的库存不因此新增排除。

本轮预览见 `output/price_audit_preview.json`，实际应用摘要见 `output/price_audit_applied.json`。v1的原预览/应用摘要另存 `_v1.json`；两个版本的逐观察判断及应用事件保留在派生表中。应用事务验证原始两表哈希前后相同。

```bash
# 只预览；连接现有库，不重新初始化业务数据库或任务
uv run python -m app.price_audit preview

# 显式应用当前规则并查看审计状态
uv run python -m app.price_audit apply
uv run python -m app.price_audit state

# 新建指数build，保存当前审计版本、输入哈希与精确已筛选输入
uv run python -m app.price_audit rebuild-index --days 180 --category all

# 关闭派生资格层，保留全部审计记录及原始记录
uv run python -m app.price_audit rollback
```

指数重算默认写 `output/indices_after_price_audit.json`。也可在加载新版代码的服务中调用原有 `POST /api/indices/rebuild`。旧build通过 `IndexService.replay(build_id)` 使用其冻结输入复算，不跟随未来审计切换改写。审计版本变化时，GET不会把新资格价格套在旧build名下，而是显示待重建。

规则不清除原 normalization 已经写入的旧 `excluded`。因此历史“整机拆下内存”等既有误排不会被本轮自动恢复；需要另一个有证据的质量修订流程。目录型号及手机App成交读数也不由该层改分类。当前旧服务不会因文件更新自动热替换业务方法；按正常发布步骤加载新版代码后才对HTTP请求生效，本轮审计应用没有重启服务。
