# 跨区域水资源使用权分配与转让

一个仅使用 Python 标准库实现的水权账户、计量、转让审批和干旱情景服务。SQLite 保存账户额度、取水记录、季节规则、上下游影响规则和完整审计日志。

## 结算日期模型（核心）

审批不再直接改写账户额度。`accounts.quota` 始终是原始许可量，任意结算日 `D` 的账面值都由台账推导：

```
settled_quota(A, D) = 原始许可额度
    + 生效日 ≤ D 的已批准转入
    − 生效日 ≤ D 的已批准转出
available(A, D) = settled_quota(A, D) − 发生日 ≤ D 的累计取水
```

- **转让到生效日才影响两方**：未审批或已批准但未生效的转让不动用任何一方的水量，生效日之前水仍归转出方使用。待审批转让只作为 `pending_outgoing` 展示，并在发起/改期时做远期可承诺性校验，防止同一水量重复转卖。
- **同日结算顺序**：同一天转让先结清、取水后入账。登记生效日取水时若转让结清后额度不足（含月份季节上限），登记整体失败，冲突明细随 409 返回，已有账目不被改写。
- **并发冲突**：审批与取水并发提交时由 `BEGIN IMMEDIATE` 串行化，后落库的一方收到 409，先落库的账目保留。
- **取消 / 改期**：可取消待审批或已批准但未生效的转让；改期会对双方全部受影响取水重算（含季节上限）。重算失败则原日期、原账目不变，可调整后重试。取消幂等，计量事件按 `(账户,事件编号)` 幂等，重复提交不重复扣水。
- **统一结算日期视图**：`GET /api/accounts`、`GET /api/transfers`、`GET /api/transfers/{id}`、`GET /api/drought/simulate` 与 `available` 均接受 `as_of=YYYY-MM-DD`（缺省为今天），响应回显同一 `as_of`，三个视图的 `settled_quota / used / available` 必然一致。

## 运行

```bash
python app.py --init
python app.py --port 8007
```

打开 <http://127.0.0.1:8007>。`--init` 会创建北区水库和河口灌区两个示例账户，并添加一条 7 月季节上限和一条最小留存规则。数据库默认是 `water_rights.db`，可用 `--db` 或 `WATER_DB` 修改。旧数据库会自动迁移出 `cancelled_at` 列。

## API

请求头 `X-User` 和 `X-Role` 用来模拟身份。角色包括 `editor`、`reviewer`、`meter`、`viewer`。

- `POST /api/accounts`：建立账户（原始许可额度、优先级、有效期）。
- `POST /api/rules/season`：设置某地区某月份的用水比例上限（按结算日已结清额度计算）。
- `POST /api/rules/impact`：设置上下游转让的最小留存比例（按转出方原始许可额度计算）。
- `POST /api/transfers`：发起转让；待审批金额只做远期可承诺性预占，不扣减账面额度。
- `POST /api/transfers/{id}/approve|reject`：审核；发起人不能审批自己的记录。批准时按生效日重放双方台账，与既有取水冲突则整笔拒绝。
- `POST /api/transfers/{id}/cancel`：取消待审批或未生效的已批准转让（幂等；支持 `?as_of=` 指定当前结算日）。
- `POST /api/transfers/{id}/reschedule`：修改未生效转让的生效日期，重算双方受影响取水和最小留存；失败可重试。
- `POST /api/usage`：按计量事件登记取水；同日已批准转让先结清，不足则保留原账目并提示失败；同一账户同一事件编号只会入账一次。
- `GET /api/accounts?as_of=...`：账户列表，含结算日的 `settled_quota / used / available / pending_outgoing`。
- `GET /api/accounts/{id}/available?as_of=...`：查看某结算日的可用额度。
- `GET /api/transfers?as_of=...` / `GET /api/transfers/{id}?as_of=...`：转让列表与详情；详情含转出/转入两方在同一结算日的账面值和 `settlement_state`（`pending_approval` / `approved_pending_effect` / `settled` / `rejected` / `cancelled`）。
- `GET /api/drought/simulate?supply=1000&reduction=0.3&as_of=...`：按高优先级先行分配，同级账户按结算日剩余额度比例分配；只含结算日有效的账户。
- `GET /api/audit`：完整操作审计。

所有额度判断与写入都在 `BEGIN IMMEDIATE` 事务内完成；台账重放保证并发提交不会绕过额度检查。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖：待审批/未生效不动账、生效日双方变动、生效日前取水仍归转出方、同日转让先结清导致取水失败且原账目保留、审批与取水并发仅一方成功且最终账面一致、取消幂等与重算、改期冲突与重试不重复扣水、重复计量事件、季节上限按已结清额度计算、预占超卖拦截、最小留存和发起人自审冲突，以及三个视图在同一结算日期的数值一致性。
