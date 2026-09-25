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

## 补拍版本与锁定

- 每次状态提交生成一个补拍版本，必须写清拍摄日（`YYYY-MM-DD`）、操作人和替换原因；同一镜头同一元素的版本号逐次增加，被替换的旧版本作废留档，不再参与连续性判断。
- 连续性检查只读每个镜头最新启用的版本；冲突记录标注所依据的状态版本（`from_version`/`to_version`）。
- 锁定镜头时绑定当时的镜头版本，锁定结果返回该版本。此后场次内任何镜头出现新版本（补拍或方案生成），同场次镜头一起退回待锁定并写明原因；旧锁定记录保留，可随时查询。
- 调整方案审核期间，对应镜头元素不接收新版本；审片通过后按方案生成版本（`source=plan`），驳回则恢复接收。
- 连续性报告中每个镜头标出当前版本、启用状态版本和生效锁定所绑定的版本。

## 主要接口

- `POST /api/users`、`POST /api/productions`
- `POST /api/productions/{id}/scenes`、`POST /api/scenes/{id}/shots`
- `POST /api/productions/{id}/elements`、`POST /api/elements/{id}/transitions`
- `POST /api/shots/{id}/states`（需 `shoot_date`、`reason`）、`POST /api/scenes/{id}/check`
- `GET /api/shots/{id}/versions`、`GET /api/shots/{id}/locks`
- `POST /api/conflicts/{id}/plans`（需 `shoot_date`）、`POST /api/plans/{id}/review`
- `POST /api/conflicts/{id}/exemptions`
- `POST /api/shots/{id}/lock`（返回绑定的版本号）
- `GET /api/productions/{id}/continuity`
