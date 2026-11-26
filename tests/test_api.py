"""HTTP 接口测试：称重回传、顾客确认、资金回执与出货放行的路由。"""
import json
import threading
import unittest
import urllib.request
from pathlib import Path

from app.api import make_server
from app.datafiles import load_catalog, load_offers
from app.service import OrderService

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        service = OrderService(catalog=load_catalog(SAMPLES / "catalog.ndjson"),
                               offers=load_offers(SAMPLES / "offers.ndjson"))
        cls.server = make_server(service, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self):
        status, _ = self.call("GET", "/health")
        self.assertEqual(status, 200)
        status, created = self.call("POST", "/orders", {
            "basket": "wb-http", "store": "f1", "customer": "u-t", "offer": "week47",
            "max_pay": 3400,
            "items": [{"line": "L-a", "sku": "pear", "min": 1100, "max": 1500,
                       "allow_group": True},
                      {"line": "L-b", "sku": "greens", "min": 1200, "max": 1450,
                       "allow_group": False}]})
        self.assertEqual(status, 201)
        self.assertEqual(created["quote"]["total"], 3007)
        for ticket in [
                {"store": "f1", "scale": "s1", "ticket": "w61", "revision": 1,
                 "bag": "bag-61", "line": "L-a", "sku": "pear", "grams": 1300,
                 "by": "picker-1"},
                {"store": "f1", "scale": "s1", "ticket": "w62", "revision": 1,
                 "bag": "bag-61", "line": "L-b", "sku": "greens", "grams": 1300,
                 "by": "picker-1"}]:
            status, _ = self.call("POST", "/orders/wb-http/tickets", ticket)
            self.assertEqual(status, 202)
        status, billed = self.call("POST", "/orders/wb-http/bills", {"bag": "bag-61"})
        self.assertEqual((status, billed["bill"]["total"]), (201, 3011))
        status, voucher = self.call("POST", "/orders/wb-http/release")
        self.assertEqual((status, voucher["bag"]), (201, "bag-61"))
        # 重复实扣回执：duplicate，不重复发货
        capture = voucher["capture_ref"]
        status, receipt = self.call("POST", "/funds/receipts", {
            "channel": "wallet-a", "ref": capture, "version": 1,
            "action": "capture", "status": "succeeded", "amount": 3011,
            "parent": "wb-http-hold-1"})
        self.assertEqual(receipt["outcome"], "duplicate")
        _, again = self.call("POST", "/orders/wb-http/release")
        self.assertEqual(again["voucher"], voucher["voucher"])
        # 按 ref 脱离会话查询资金结论
        status, found = self.call("POST", "/funds/query", {"ref": capture})
        self.assertEqual((status, found["status"]), (200, "succeeded"))
        # 客服视图
        status, view = self.call("GET", "/orders/wb-http")
        self.assertEqual(view["deliverable_bag"], "bag-61")
        self.assertEqual(view["money"]["captured"], 3011)

    def test_unknown_order_and_bad_body(self):
        status, _ = self.call("GET", "/orders/none")
        self.assertEqual(status, 409)
        status, _ = self.call("POST", "/orders", {"basket": "x"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
