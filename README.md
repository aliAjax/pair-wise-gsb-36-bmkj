# 跨区域水资源使用权分配与转让

一个仅使用 Python 标准库实现的水权账户、计量、转让审批和干旱情景服务。SQLite 保存账户额度、取水记录、季节规则、上下游影响规则和完整审计日志。

## 运行

```bash
python app.py --init
python app.py --port 8007
```

打开 <http://127.0.0.1:8007>。`--init` 会创建北区水库和河口灌区两个示例账户，并添加一条 7 月季节上限和一条最小留存规则。数据库默认是 `water_rights.db`，可用 `--db` 或 `WATER_DB` 修改。

## API

请求头 `X-User` 和 `X-Role` 用来模拟身份。角色包括 `editor`、`reviewer`、`meter`、`viewer`。

- `POST /api/accounts`：建立账户（额度、优先级、有效期）。
- `POST /api/rules/season`：设置某地区某月份的用水比例上限。
- `POST /api/rules/impact`：设置上下游转让的最小留存比例。
- `POST /api/transfers`：发起转让；待审批金额立即预占，避免同一额度被重复转卖。
- `POST /api/transfers/{id}/approve|reject`：审核；发起人不能审批自己的记录。
- `POST /api/usage`：按计量事件登记实际取水，同一账户同一事件编号只会入账一次。
- `GET /api/accounts/{id}/available`：查看扣减实际用量和待审批预占后的可用额度。
- `GET /api/drought/simulate?supply=1000&reduction=0.3`：按高优先级先行分配，同级账户按剩余额度比例分配。
- `GET /api/audit`：完整操作审计。

余额计算和审批使用 `BEGIN IMMEDIATE`，把余额判断与写入放在同一事务中；因此并发提交不会绕过额度检查。最小留存比例按转出账户的当前许可额度计算。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖转让审批与实际计量、重复计量事件、季节/最小留存规则、预占导致余额不足和发起人自审冲突。
