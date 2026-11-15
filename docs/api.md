# 资料服务接口

服务只在本机提供 JSON，响应编码为 UTF-8。查询接口不修改资料。

| 方法和路径 | 当前行为 |
| --- | --- |
| `GET /health` | 数据指纹和存活状态 |
| `GET /tables` | 表、字段类型和记录数 |
| `GET /tables/{name}?offset=0&limit=50` | 原始资料分页；偏移非负，每页一至五百行 |
| `GET /view` | 当前领域经营资料摘要 |
| `GET /profiles` | 数值字段的观察数、缺失数、中位数及绝对中位差 |
| `GET /transmission/dimensions` | 传导分析可选的品种、规格、片区与线路代号 |
| `GET /transmission/series` | 按品规地区构建的可比批零序列剖面与缺报统计 |
| `POST /transmission/analyze` | 批零传导估计：滞后累计关联、不确定性、留时段与稳定性检验 |
| `POST /cases` | 保存研究备注和当前完整资料副本；返回新记录 |
| `GET /cases` | 研究记录索引 |
| `GET /cases/{id}` | 校验完整性并回读记录 |

创建研究备注的字段为 `label`、`options` 和 `notes`，分别是名称、参数对象与说明文字。名称一至一百二十字，后两项可省略。不接受其他字段，不允许覆盖旧记录。记录 ID 由服务生成。资料指纹由原始数据和结构共同形成，不表示计算结果的统计可信度。

源表未找到或记录 ID 无效返回 404；字段或分页无效返回 400。默认监听 127.0.0.1:8080。服务当前提供资料浏览、数值探索和研究备注，不将保存备注视为已经完成经营分析。

## 批零传导接口

### `GET /transmission/dimensions`

返回可筛选的 `species`、`grade`、`region` 与 `route_ids`，供研究员先确认线路口径。

### `GET /transmission/series`

查询参数（均可重复或省略）：`species`、`grade`、`region`、`route_id`、`quote_kind`（`挂牌`、`询价` 或 `any`，默认挂牌）、`match`（`same_day`、`window`、`trade_day`，默认同日）、`window`（窗口匹配半径一至七天，默认 2）、`evidence=1` 时附带成交证据。报价先按线路 `unit_kg` 折算为元/公斤；缺报位置保持未观测，不做填充。返回每条线路的配对日数、相邻配对比例、缺报单元数、窗口对齐偏移和报价类型计数。

### `POST /transmission/analyze`

请求对象字段（除筛选条件外均有默认值）：

| 字段 | 含义与约束 |
| --- | --- |
| `species` / `grade` / `region` / `route_ids` | 线路筛选；`route_ids` 为文本数组，与品规条件可合并使用 |
| `start_day` / `end_day` | 自定义日期窗口，`YYYY-MM-DD`，可只给一端 |
| `max_lag` | 最大滞后天数，0 至 7，默认 4 |
| `models` | 子集：`plain`（仅常数与滞后冲击）、`controlled`（加入趋势、星期、月份、节假日与同品种跨片区共振）、`asymmetric`（批发涨跌拆分，含非对称检验） |
| `windows` | 子集：`full`、`pre_season`（开海前）、`post_season`（开海后）、`recent_90d` |
| `match` / `window` / `quote_kind` | 与序列接口同义；敏感性矩阵会另跑六种匹配与口径组合 |
| `holdout_fraction` | 留时段比例 0.1 至 0.4，默认 0.2，按时间顺序留后段 |
| `bootstrap_replications` | 移动块自助次数 40 至 2000，默认 300 |
| `block_length` | 自助块长 2 至 21 天，默认 7 |
| `seed` | 随机种子，固定后结果可复现 |

响应要点：

- `horizon_models`：逐滞后期回归的 HC3 系数、Ljung-Box/Breusch–Godfrey/ARCH/Jarque–Bera 残差诊断与留时段 RMSE 对照；`joint_distributed_lag` 在完整行删除后秩充足时给出联合分布滞后模型。
- `cumulative` / `up_cumulative` / `down_cumulative`：各期累计关联、保守解析区间与移动块自助 95% 区间；区间覆盖零即该期关联不具统计辨识度。
- `sample_quality`：候选目标日数、因缺报缺口剔除的日数与逐滞后期覆盖率。
- `long_run`：Engle–Granger 协整检验（含 MacKinnon 近似临界值）；不支持长期关系时不返回 ECM，只保留有边界的短期关联。
- `season_stability`：开海前后分段估计、季节交互项 Wald 检验与核心面板递归 CUSUM。
- `sensitivity`：挂牌/询价/混合口径、同日/窗口/成交日匹配及不同自助块长下的结论对照。
- `trade_evidence`：两端成交记录数、取消记录、成交量加权成交价、报价-成交价差。
- `interpretation`：固定的解释边界声明。所有结果是观察口径下的统计关联，不是零售商加价的因果证据；报价不等于成交，缺报不代表零涨跌。
