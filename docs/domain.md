# 批零价格涨跌传导的时滞资料约定

线路限定同品种、同规格、同片区的两端市场。报价单位可能是一市斤或一公斤，成交价全部为元/公斤。日期按中国标准时间归日，available_at 是当日资料实际可用时刻。缺报没有隐含价格，取消交易仍保留在证据表。批零匹配不保证同一条货流，地域与季节标签只描述观察背景。

## 数据表

### `routes`

同品规的批零匹配线路；市场代号仅为本地代号，单位重量是每报价单位的公斤数。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `species` | `text` |
| `grade` | `text` |
| `region` | `text` |
| `wholesale_market` | `text` |
| `retail_market` | `text` |
| `unit_kg` | `number` |

### `quotes`

报价不保证成交；price 按线路报价单位计价。缺行是未观测，挂牌与询价不可不加区分混合。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `route_id` | `text` |
| `day` | `date` |
| `side` | `text` |
| `price` | `money` |
| `quote_kind` | `text` |
| `available_at` | `time` |

### `trades`

成交单独以公斤计价，取消记录不产生真实成交；与报价没有逐笔一一关系。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `route_id` | `text` |
| `day` | `date` |
| `side` | `text` |
| `quantity_kg` | `number` |
| `unit_price_kg` | `money` |
| `cancelled` | `bool` |

### `calendar`

日期历表；季节是运营标记，不是已估计出的结构断点。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `date` |
| `weekday` | `int` |
| `holiday` | `bool` |
| `season` | `text` |

## 文件和数值语义

空单元格只在带问号的类型中表示缺失；零和空值不同。金额由十进制定点字符串保存，浮点数仅用于探索性统计。所有数据均为本地生成的虚构经营记录，不对应真实公司、客户或住址。数值剖面只反映观察数据，不能代替总体推断。

研究备注的参数为业务自由对象，不代表已执行了任何统计模型。原始表与已有视图可单独查询。
