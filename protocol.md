# 称重与资金协议

示例使用整数分和克。`baskets` 以行申报允许重量、是否接受同组替代和最高承诺金额。`catalog` 中 per_kg 表示每公斤分，数量价格按行乘重量后四舍五入到分。`offers` 中 single_sku 券先扣该 SKU 行，扣减不超过该行金额，单篮用一次。剩余商品小计达到 basket_min 才享 basket_off，按各行券后价值比例分；余分按小数余量从大到小分，余量相同按行标识排序。运费在优惠后商品净额达到 shipping_min 时免收。若部分重量退款，按原行净额和实际成交重量的比例计算累计可退款、逐次扣已退，整行退清时处理余分。未配送的整单另退原实际运费。

`weights` 由 (store, scale, ticket) 标识秤单，包含 bag、sku、line、grams 及称重者，秤单 revision 递增，同一 revision 内容冲突需店员更正。装袋记录由秤单集合标识；重称和换货要形成可辨认的候选。

`funds` 消息有 channel、ref、version、action、status 及金额。渠道号在 channel 内唯一，提交使用业务编号 ref 去重。action 为 hold、capture、void、refund，状态 accepted 或 unknown 均不能证明钱已扣，succeeded 和 rejected 为可对账结果。每项新动作有自己的编号，capture 和 void 引用 hold，refund 引用 capture，资金查询可用 ref 重取已知结论。渠道通知可按本地渠道协议接入或按样本回传。交付凭证从库房生成，引用已确认的 bag。

交换样本为 UTF-8 NDJSON，每行独立。文件中的行顺序保留这组资料的原始提供顺序。
