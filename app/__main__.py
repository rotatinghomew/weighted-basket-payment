"""python3 -m app [--host H] [--port P] [--samples DIR] — 启动称重订单服务。"""
import argparse
from pathlib import Path

from .api import make_server
from .datafiles import load_catalog, load_offers
from .service import OrderService


def build_service(samples_dir: str) -> OrderService:
    samples = Path(samples_dir)
    return OrderService(catalog=load_catalog(samples / "catalog.ndjson"),
                        offers=load_offers(samples / "offers.ndjson"))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="app", description="称重订单的整篮承诺与支付放行")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8073)
    parser.add_argument("--samples", default="samples", help="交换资料目录")
    args = parser.parse_args(argv)
    service = build_service(args.samples)
    httpd = make_server(service, args.host, args.port)
    print(f"称重订单服务监听 http://{args.host}:{args.port}")
    print("接口：POST /orders，POST /orders/{id}/tickets|bills|confirm|cancel|release|deliver|refunds，")
    print("      POST /funds/receipts，POST /funds/query，GET /orders/{id}（客服视图）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
