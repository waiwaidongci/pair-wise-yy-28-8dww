# 影视拍摄连续性管理

项目使用 Python 标准库、SQLite 和 `http.server` 管理非线性拍摄中的连续性。场次与镜头分别记录叙事顺序和拍摄顺序，角色、服装、道具、伤痕状态按叙事链检查，冲突可由调整方案或正式豁免处理，镜头只有在无未处理冲突时才能锁定。

## 运行与测试

```bash
python app.py
python -m unittest discover -s tests -v
```

默认端口 `8115`，页面 <http://127.0.0.1:8115>。首次启动创建“雨夜追踪”示例，其中拍摄顺序与叙事顺序相反，并生成一个伤痕回退冲突。数据库和端口可分别用 `CONTINUITY_DB`、`PORT` 指定。

## 连续性算法

每个元素选择一种规则：

- `stable`：沿叙事顺序状态必须一致。
- `monotonic`：使用 `numeric_value` 比较，数值不能下降，适合伤痕、污损或破坏程度。
- `allowed`：只有预先登记的状态转移才能通过。

检测按叙事顺序执行，与剪辑和拍摄顺序无关。调整方案必须由制片人或场记提出、由另一位审片人批准；批准后写入镜头状态并重新检查。也可以为确实需要保留的冲突写入豁免理由。锁定会再次检查场次，豁免之外的活跃冲突会阻止锁定。

## 补拍版本与锁定规则

- **状态版本化**：每次 `POST /api/shots/{id}/states` 都会保留补拍版本，必须写清 `shoot_date`（拍摄日，YYYY-MM-DD）、`user_id`（操作人）、`reason`（替换原因）；同一镜头同一元素的版本号逐次 +1，旧版本置为 `superseded` 留档但不参与判断。
- **只读最新版本**：连续性检查只读取每个镜头每个元素最新 `active` 版本，冲突记录及其报告标注两端所依据的版本号（如 `S01-01 v3 → S01-02 v1`）。
- **审核冻结**：场次存在待审调整方案时，该场次所有镜头拒绝接收新版本，也不能锁定；审片通过后才按方案内容生成新版本（驳回不生成版本）。
- **锁定绑定版本**：锁定时记录当前镜头版本号及当时各元素启用版本快照。锁定镜头若又收到补拍新版本，当前锁定作废，同场次所有已锁定镜头一起退回 `relock_pending`（附退回原因）；冲突清零后可重新锁定。旧锁定记录保留在 `shot_locks` 中并标明作废原因，随时可查。

## 主要接口

- `POST /api/users`、`POST /api/productions`
- `POST /api/productions/{id}/scenes`、`POST /api/scenes/{id}/shots`
- `POST /api/productions/{id}/elements`、`POST /api/elements/{id}/transitions`
- `POST /api/shots/{id}/states`（版本化状态提交，需 `shoot_date`、`reason`）
- `GET /api/shots/{id}/versions`（含作废版本的完整版本历史）
- `GET /api/shots/{id}/locks`、`GET /api/scenes/{id}/locks`（锁定历史，含旧锁定与作废原因）
- `POST /api/scenes/{id}/check`
- `POST /api/conflicts/{id}/plans`（可带 `shoot_date`）、`POST /api/plans/{id}/review`
- `POST /api/conflicts/{id}/exemptions`
- `POST /api/shots/{id}/lock`（返回 `locked_version`）
- `GET /api/productions/{id}/continuity`（报告逐镜头列出所依据版本、拍摄日、操作人、替换原因及锁定历史）
