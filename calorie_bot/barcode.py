"""Barcodes: read one from a photo, look the product up in Open Food Facts.

Open Food Facts (openfoodfacts.org) is a free, open database of packaged food with the
nutrition label data for millions of products. No API key is needed. Lookups are cached
in the bot's database, so a product you scan again is instant and doesn't ask again.
"""

from __future__ import annotations

import io
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from .db import Recipe, Store

log = logging.getLogger(__name__)

API = "https://world.openfoodfacts.org/api/v2/product/{code}.json?fields=" + ",".join([
    "product_name", "brands", "quantity", "serving_size", "serving_quantity",
    "product_quantity", "nutriments"])
# Open Food Facts asks clients to identify themselves.
USER_AGENT = "calorie-bot/1.0 (self-hosted Telegram calorie tracker)"
BARCODE_TEXT = re.compile(r"^\s*(\d{8}|\d{12,14})\s*$")   # EAN-8, UPC-A, EAN-13, GTIN-14


@dataclass
class Product:
    barcode: str
    name: str
    brand: str
    unit: str                 # "g" or "ml": what the per-100 values are per
    kcal_100: float
    protein_100: float
    carbs_100: float
    fat_100: float
    serving_qty: float = 0    # grams or ml in one serving, if known
    serving_text: str = ""
    package_qty: float = 0    # grams or ml in the whole package, if known
    package_text: str = ""

    @property
    def title(self) -> str:
        return f"{self.name} ({self.brand})" if self.brand else self.name

    def as_recipe(self) -> Recipe:
        """A stand-in recipe of exactly 100 g/ml, so portions are grams x per-100 values."""
        return Recipe(id=0, name=self.title, ingredients=[], total_weight_g=100, servings=0,
                      calories=self.kcal_100, protein_g=self.protein_100,
                      carbs_g=self.carbs_100, fat_g=self.fat_100)

    def label_for_claude(self) -> str:
        sizes = []
        if self.serving_qty:
            sizes.append(f"1 serving = {self.serving_qty:g} {self.unit}")
        if self.package_qty:
            sizes.append(f"whole package = {self.package_qty:g} {self.unit}")
        size_text = "; ".join(sizes) or "serving and package size unknown"
        return f"{self.title} (scanned product, amounts in {self.unit}; {size_text})"


def barcode_in_text(text: str) -> str | None:
    m = BARCODE_TEXT.match(text or "")
    return m.group(1) if m else None


def decode(image: bytes) -> str | None:
    """The first product barcode (EAN/UPC) in a photo, or None."""
    import zxingcpp
    from PIL import Image, ImageOps

    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image))).convert("L")
    except Exception:
        return None
    formats = (zxingcpp.BarcodeFormat.EAN13, zxingcpp.BarcodeFormat.EAN8,
               zxingcpp.BarcodeFormat.UPCA, zxingcpp.BarcodeFormat.UPCE)
    for candidate in (img, img.rotate(90, expand=True)):
        found = zxingcpp.read_barcodes(candidate, formats=formats)
        if found:
            return found[0].text
    return None


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse(code: str, data: dict) -> Product | None:
    """A Product from an Open Food Facts response, or None if it lacks calories."""
    if data.get("status") != 1 or not data.get("product"):
        return None
    p = data["product"]
    n = p.get("nutriments") or {}
    kcal = _num(n.get("energy-kcal_100g"))
    if not kcal and n.get("energy_100g"):
        kcal = _num(n.get("energy_100g")) / 4.184  # kJ to kcal
    if not kcal and not any(_num(n.get(k)) for k in ("proteins_100g", "carbohydrates_100g", "fat_100g")):
        return None
    sizes = f"{p.get('quantity', '')} {p.get('serving_size', '')}".lower()
    unit = "ml" if re.search(r"\d\s*(ml|cl|l)\b|fl\.? ?oz", sizes) else "g"
    return Product(
        barcode=code,
        name=(p.get("product_name") or "Unnamed product").strip(),
        brand=(p.get("brands") or "").split(",")[0].strip(),
        unit=unit,
        kcal_100=round(kcal, 1),
        protein_100=_num(n.get("proteins_100g")),
        carbs_100=_num(n.get("carbohydrates_100g")),
        fat_100=_num(n.get("fat_100g")),
        serving_qty=_num(p.get("serving_quantity")),
        serving_text=(p.get("serving_size") or "").strip(),
        package_qty=_num(p.get("product_quantity")),
        # "400 g e": the trailing e (℮) is the EU "estimated quantity" mark, not part of it
        package_text=re.sub(r"\s*[e℮]$", "", (p.get("quantity") or "").strip()),
    )


def fetch(code: str) -> dict | None:
    req = urllib.request.Request(API.format(code=code), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": 0}
        log.warning("Open Food Facts lookup for %s failed: %s", code, e)
    except Exception as e:  # network trouble: say so, don't cache
        log.warning("Open Food Facts lookup for %s failed: %s", code, e)
    return None


async def lookup(store: Store, code: str) -> tuple[Product | None, bool]:
    """(product or None, reachable). Cached answers, found or not, are reused.

    Only the network request runs in a worker thread: the database connection belongs to the
    bot's own thread."""
    import asyncio

    cached = store.cached_product(code)
    if cached is not None:
        return parse(code, cached), True
    data = await asyncio.to_thread(fetch, code)
    if data is None:
        return None, False
    store.cache_product(code, data)
    return parse(code, data), True


def card(p: Product) -> str:
    u = p.unit
    lines = [f"📦 <b>{_esc(p.title)}</b>"]
    if p.package_text:
        lines[0] += f", {_esc(p.package_text)}"
    lines.append(f"Per 100 {u}: {p.kcal_100:.0f} kcal · P {p.protein_100:g}g · "
                 f"C {p.carbs_100:g}g · F {p.fat_100:g}g")
    if p.serving_qty:
        lines.append(f"Per serving ({p.serving_qty:g} {u}): "
                     f"{p.kcal_100 * p.serving_qty / 100:.0f} kcal")
    examples = ["“1 serving”" if p.serving_qty else None, f"“40{u}”",
                "“half the pack”" if p.package_qty else None, "“2 of them”"]
    lines += ["", "How much did you have? e.g. " + ", ".join(e for e in examples if e)]
    return "\n".join(lines)


def _esc(text: str) -> str:
    import html
    return html.escape(text)
