# 称重订单的整篮承诺与支付放行

《[称重与资金协议](protocol.md)》记录相关业务系统已经约定的交换格式。样本是独立提供的合成输入，涉及的编号、联系代号和金额均为演练数据。每个文件可独立检查，跨文件通过格式说明中的业务引用关联。

本地使用 Python 3.11.2，资料检查使用标准库，无需安装依赖。在项目目录运行 `python3 -m unittest discover -s tests -v`。例如运行 `python3 -m json.tool --json-lines samples/catalog.ndjson` 可逐条查看一份交换资料。

## 可运行服务

`app/` 把称重回传、顾客确认、资金回执和出货放行接成可运行的服务：

- `app/pricing.py` — 计价：单品券先扣该行（不超过行额、单篮一次），小计达 `basket_min` 才享 `basket_off` 并按券后价值比例分摊（余分按小数余量从大到小、相同按行标识），净额达 `shipping_min` 免运费；部分重量退款按成交行净额比例累计、逐次扣已退、整行退清处理余分。
- `app/scales.py` — 秤单台账：`(store, scale, ticket)` 标识、revision 递增；同一 revision 内容冲突需店员更正；装袋候选按 revision 钉住秤单，两位店员先后重称时各篮各用各的秤单。
- `app/funds.py` — 资金台账：提交按业务编号 `ref` 去重；`accepted`/`unknown` 不能证明钱已扣，`succeeded`/`rejected` 为可对账结果；乱序回执按版本落地、迟到低版本搁置、重复回执幂等；可按 `ref` 脱离会话重取结论。
- `app/service.py` — 订单状态机：结账冻结上限；按已认可重量与锁定条款重算账单（不沿用预估、不因活动结束重定价）；超上限或换货超允诺范围须顾客对该版账单重新确认（上一版的同意不能批准新账单）；任何实扣未知时不再扣一笔也不放行；确知已收且货不再交付时退款、仅冻结时撤销冻结；交付只认最后确认的装袋记录与对应资金结果。
- `app/api.py` — HTTP 接口；`app/demo.py` — 按样本端到端回放。

### 启动与回放

```bash
python3 -m app --port 8073        # 启动服务（默认加载 samples/ 的目录与活动）
python3 -m app.demo               # 回放样本故事：重新确认期间取消退款、交付后部分退款
python3 -m unittest discover -s tests -v
```

### 接口

| 方法与路径 | 用途 |
| --- | --- |
| `POST /orders` | 结账：锁定活动条款、给出预估与最高应付、冻结上限 |
| `POST /orders/{id}/tickets` | 电子秤回传（revision、冲突、装袋候选） |
| `POST /orders/{id}/bills` | 按装袋候选组账：`{"bag": "bag-31"}` |
| `POST /orders/{id}/confirm` | 顾客确认指定版本账单：`{"version": 2}` |
| `POST /orders/{id}/reject` | 顾客拒绝最新待确认账单 |
| `POST /orders/{id}/cancel` | 取消：仅冻结撤冻结，已实扣整单退款，未知则等对账 |
| `POST /orders/{id}/release` | 库房放行：生成引用已确认装袋的交付凭证（幂等） |
| `POST /orders/{id}/deliver` | 确认交付 |
| `POST /orders/{id}/refunds` | 交付后按行退部分重量：`{"line": "L-a", "grams": 650}` |
| `POST /orders/{id}/sync` | 对账后重推资金编排（如实扣被拒后重试） |
| `POST /offers/{offer}/end` | 结束活动：新下单拒绝，已确认订单不重定价 |
| `POST /funds/receipts` | 渠道异步回执（可乱序、可重复） |
| `POST /funds/query` | 按业务编号 `ref` 重取渠道已知结论 |
| `GET /orders/{id}` | 客服视图：哪版在哪一步被替换、钱实际冻结/实扣/退到哪里、库房可交出哪一袋 |

### 示例

```bash
curl -X POST localhost:8073/orders -d '{"basket":"wb-1","store":"f1","customer":"u-w","offer":"week47","max_pay":3400,"items":[{"line":"L-a","sku":"pear","min":1100,"max":1500,"allow_group":true},{"line":"L-b","sku":"greens","min":1200,"max":1450,"allow_group":false}]}'
curl -X POST localhost:8073/orders/wb-1/tickets -d '{"store":"f1","scale":"s1","ticket":"w31","revision":1,"bag":"bag-31","line":"L-a","sku":"pear","grams":1300,"by":"picker-1"}'
curl -X POST localhost:8073/orders/wb-1/bills -d '{"bag":"bag-31"}'
curl -X POST localhost:8073/orders/wb-1/release
curl localhost:8073/orders/wb-1
```
