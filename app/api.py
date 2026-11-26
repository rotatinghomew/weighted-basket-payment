"""HTTP 接口：把称重回传、顾客确认、资金回执和出货放行接到可运行的服务上。"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .scales import BagError, ScaleConflict
from .service import OrderError, OrderService

ROUTES = [
    ("GET", "/health"),
    ("POST", "/orders"),
    ("GET", "/orders/{oid}"),
    ("POST", "/orders/{oid}/tickets"),
    ("POST", "/orders/{oid}/bills"),
    ("POST", "/orders/{oid}/confirm"),
    ("POST", "/orders/{oid}/reject"),
    ("POST", "/orders/{oid}/cancel"),
    ("POST", "/orders/{oid}/release"),
    ("POST", "/orders/{oid}/deliver"),
    ("POST", "/orders/{oid}/refunds"),
    ("POST", "/orders/{oid}/sync"),
    ("POST", "/offers/{offer}/end"),
    ("POST", "/funds/receipts"),
    ("POST", "/funds/query"),
]


def _compile(route: str):
    pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", route)
    return re.compile(f"^{pattern}$")


def make_server(service: OrderService, host: str = "127.0.0.1", port: int = 8073):
    lock = threading.Lock()
    routes = [(method, _compile(route)) for method, route in ROUTES]

    class Handler(BaseHTTPRequestHandler):
        server_version = "WeighFresh/1.0"

        def log_message(self, *args):
            pass

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method: str):
            try:
                body = self._body() if method == "POST" else {}
                with lock:
                    result = self._dispatch(method, body)
                if result is None:
                    self._send(404, {"error": "未找到该路径"})
                else:
                    status, obj = result
                    self._send(status, obj)
            except OrderError as exc:
                self._send(409, {"error": str(exc)})
            except (BagError, ScaleConflict) as exc:
                self._send(422, {"error": str(exc)})
            except KeyError as exc:
                self._send(400, {"error": f"缺少字段: {exc}"})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._send(400, {"error": str(exc)})

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _send(self, status: int, obj):
            data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method: str, body: dict):
            for route_method, pattern in routes:
                if route_method != method:
                    continue
                match = pattern.match(self.path)
                if match:
                    return self._call(f"{method} {self.path}", match.groupdict(), body)
            return None

        def _call(self, route: str, args: dict, body: dict):
            oid = args.get("oid")
            if route == "GET /health":
                return 200, {"ok": True}
            if route == "POST /orders":
                order = service.checkout(body)
                return 201, service.inspect(order.order)
            if oid is not None and route == f"GET /orders/{oid}":
                return 200, service.inspect(oid)
            if route == f"POST /orders/{oid}/tickets":
                return 202, service.ingest_ticket(body)
            if route == f"POST /orders/{oid}/bills":
                bill = service.build_bill(oid, body["bag"])
                return 201, {"version": bill.version,
                             "bill": service.inspect(oid)["bills"][-1],
                             "state": service.orders[oid].state}
            if route == f"POST /orders/{oid}/confirm":
                service.confirm(oid, int(body["version"]))
                return 200, service.inspect(oid)
            if route == f"POST /orders/{oid}/reject":
                service.reject_bill(oid, int(body["version"]))
                return 200, service.inspect(oid)
            if route == f"POST /orders/{oid}/cancel":
                service.cancel(oid)
                return 200, service.inspect(oid)
            if route == f"POST /orders/{oid}/release":
                return 201, service.release(oid)
            if route == f"POST /orders/{oid}/deliver":
                service.deliver(oid)
                return 200, service.inspect(oid)
            if route == f"POST /orders/{oid}/refunds":
                return 201, service.refund_line(oid, body["line"], body["grams"])
            if route == f"POST /orders/{oid}/sync":
                return 200, service.sync_order(oid)
            if route == f"POST /offers/{args.get('offer')}/end":
                service.end_offer(args["offer"])
                return 200, {"offer": args["offer"], "active": False}
            if route == "POST /funds/receipts":
                act, outcome = service.apply_receipt(body)
                return 202, {"ref": act.ref, "action": act.action,
                             "status": act.status, "outcome": outcome}
            if route == "POST /funds/query":
                act = service.query_funds(body["ref"])
                if act is None:
                    return 404, {"error": f"未知业务编号 {body['ref']}"}
                return 200, service._action_view(act)
            return None

    return ThreadingHTTPServer((host, port), Handler)
