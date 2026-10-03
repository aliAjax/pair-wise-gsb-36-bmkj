# 跨区域水资源使用权分配与转让

一个仅使用 Python 标准库实现的水权账户、计量、转让审批和干旱情景服务。SQLite 保存账户额度、取水记录、季节规则、上下游影响规则和完整审计日志。

## 结算日期模型（核心规则）

账户的许可额度 `quota` 是**原始账目，永远不被转让改写**。所有余额都从台账按**结算日期**推导：

- `结算额度(d) = quota + 生效日 ≤ d 的已批准转入 − 生效日 ≤ d 的已批准转出`
- `结算用量(d) = 计量日 ≤ d 的取水合计`
- `可用量(d) = 结算额度(d) − 结算用量(d)`

由此保证：

- 待审批转让**不再预占**；生效日之前水量仍归转出方使用，审批通过但未生效也一样。
- 转让只在 `effective_date` 当天进入双方台账；同日场景中**转让先结清、取水后入账**。
- 登记取水按 `occurred_at` 结算，额度不足时事务回滚：不写入取水记录、不改动任何汇总，只返回失败；同一计量事件重复提交不会重复扣水。
- 同一账户同日并发的审批与取水按写锁取得的先后结算，落后一方收到 `409`，已提交账目不会被改写。
- 取消未生效转让（`cancel`）或调整生效日期（`reschedule`）后，系统重放受影响区间的取水（以及季节上限），重算结果随操作返回；重算是只读校验，可随时用 `/api/recompute` **安全重试**。
- 账户列表、转让详情与干旱模拟都接受同一个 `as_of` 结算日期，显示该日期下一致的数值（不传默认当天）。

## 运行

```bash
python app.py --init
python app.py --port 8007
```

打开 <http://127.0.0.1:8007>。`--init` 会创建北区水库和河口灌区两个示例账户，并添加一条 7 月季节上限和一条最小留存规则。数据库默认是 `water_rights.db`，可用 `--db` 或 `WATER_DB` 修改。

## API

请求头 `X-User` 和 `X-Role` 用来模拟身份。角色包括 `editor`、`reviewer`、`meter`、`viewer`。

- `POST /api/accounts`：建立账户（原始许可额度、优先级、有效期）。
- `POST /api/rules/season`：设置某地区某月份的用水比例上限（按结算额度计）。
- `POST /api/rules/impact`：设置上下游转让的最小留存比例（按生效日结算后的额度计）。
- `POST /api/transfers`：发起转让；待审批不预占，按生效日做可行性提示。
- `POST /api/transfers/{id}/approve|reject`：审核；审批在写锁内按生效日台账做权威校验，发起人不能审批自己的记录。
- `POST /api/transfers/{id}/cancel`：取消待审批或已批准但未生效的转让，并立即重放双方取水。
- `POST /api/transfers/{id}/reschedule`：修改生效日期（body: `effective_date`），重放新旧日期之间的受影响取水。
- `POST /api/usage`：按计量事件登记实际取水；按 `occurred_at` 结算，同日转让已先结清。同一账户同一事件编号只会入账一次。
- `GET /api/accounts?as_of=YYYY-MM-DD`：账户列表，附该结算日期的结算额度、用量、可用量及已结算/未生效转让额。
- `GET /api/accounts/{id}/available?as_of=...`：查看某结算日期的可用额度。
- `GET /api/transfers?as_of=...` / `GET /api/transfers/{id}?as_of=...`：转让列表与详情，含双方在该结算日期的台账视图和 `settled` 标志。
- `GET /api/drought/simulate?supply=1000&reduction=0.3&as_of=...`：按结算日期的剩余额度，高优先级先行分配，同级按剩余额度比例分配。
- `POST /api/recompute`：重算全部或指定账户（body 可选 `account_id`、`since`）的取水/季节冲突，只读、可重试。
- `GET /api/audit`：完整操作审计。

审批、取水、取消与改期都在 `BEGIN IMMEDIATE` 事务内把结算判断与写入原子完成，并发提交不会绕过额度检查，失败不会改写原账目。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖：按结算日期的可用量推导、生效日前水量归属转出方、同日转让先于取水结算、审批与取水并发冲突且原账目不被改写、取水失败回滚与重复事件去重、取消/改期后的重算冲突与安全重试、账户/转让/干旱视图共享结算日期，以及季节上限与最小留存规则。
