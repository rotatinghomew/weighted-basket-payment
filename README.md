# 称重订单的整篮承诺与支付放行

《[称重与资金协议](protocol.md)》记录相关业务系统已经约定的交换格式。样本是独立提供的合成输入，涉及的编号、联系代号和金额均为演练数据。每个文件可独立检查，跨文件通过格式说明中的业务引用关联。

本地使用 Python 3.11.2，资料检查使用标准库，无需安装依赖。在项目目录运行 `python3 -m unittest discover -s tests -v`。例如运行 `python3 -m json.tool --json-lines samples/catalog.ndjson` 可逐条查看一份交换资料。
