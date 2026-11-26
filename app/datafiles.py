"""读取交换资料（UTF-8 NDJSON，每行独立）。"""
import json
from pathlib import Path

from .pricing import CatalogEntry, OfferTerms


def read_ndjson(path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def load_catalog(path) -> dict[str, CatalogEntry]:
    return {r["sku"]: CatalogEntry(sku=r["sku"], per_kg=int(r["per_kg"]), group=r["group"])
            for r in read_ndjson(path)}


def load_offers(path) -> dict[str, OfferTerms]:
    return {r["offer"]: OfferTerms.from_dict(r) for r in read_ndjson(path)}
