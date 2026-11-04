import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx


class JDUnionClient:
    """Small adapter for approved JD Union goods and promotion APIs."""

    def __init__(self, app_key: str, app_secret: str, site_id: str, client: httpx.AsyncClient):
        self.app_key = app_key
        self.app_secret = app_secret
        if not site_id.isdigit():
            raise ValueError("JD_SITE_ID must be numeric")
        self.site_id = int(site_id)
        self.client = client

    async def _call(self, method: str, payload: dict) -> dict:
        params = {
            "app_key": self.app_key,
            "method": method,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "format": "json",
            "v": "1.0",
            "sign_method": "md5",
            "360buy_param_json": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        }
        raw = self.app_secret + "".join(key + value for key, value in sorted(params.items())) + self.app_secret
        params["sign"] = hashlib.md5(raw.encode()).hexdigest().upper()
        response = await self.client.post("https://api.jd.com/routerjson", data=params)
        response.raise_for_status()
        body = response.json()
        prefix = method.replace(".", "_")
        envelope = body.get(prefix + "_responce") or body.get(prefix + "_response")
        if not isinstance(envelope, dict):
            raise ValueError("JD Union response envelope missing")
        result = next(
            (envelope[key] for key in ("queryResult", "getResult", "result") if key in envelope),
            None,
        )
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            raise ValueError("JD Union result missing")
        return result

    async def search(self, query: str) -> list[dict]:
        sku_id = jd_sku_from_url(query)
        goods_request = {"pageIndex": 1, "pageSize": 3}
        if sku_id:
            goods_request["skuIds"] = [sku_id]
        else:
            goods_request["keyword"] = query[:100]
        result = await self._call("jd.union.open.goods.query", {"goodsReqDTO": goods_request})
        if str(result.get("code")) != "200":
            raise ValueError(
                f"JD Union goods query rejected ({result.get('code')}): {result.get('message', '')}"
            )
        goods = result.get("data") or []
        if not isinstance(goods, list):
            raise ValueError("JD Union goods response was invalid")

        offers = []
        for item in goods[:3]:
            sku = str(item.get("skuId", ""))
            title = item.get("skuName")
            if not sku.isdigit() or not isinstance(title, str) or not title.strip():
                continue
            material_url = f"https://item.jd.com/{sku}.html"
            promotion_request = {"materialId": material_url, "siteId": self.site_id}
            coupon = first_coupon_url(item)
            if coupon:
                promotion_request["couponUrl"] = coupon
            promotion = await self._call(
                "jd.union.open.promotion.common.get", {"promotionCodeReq": promotion_request}
            )
            url = (promotion.get("data") or {}).get("clickURL")
            if str(promotion.get("code")) != "200" or not valid_https_url(url):
                continue
            price_label = format_price(item.get("priceInfo"))
            offers.append({"platform": "jd", "title": title.strip()[:120], "price_label": price_label, "url": url})
        return offers


def jd_sku_from_url(value: str) -> int | None:
    match = re.search(r"https?://[^\s<>]+", value)
    if not match:
        return None
    parsed = urlparse(match.group(0))
    if parsed.hostname != "item.jd.com":
        return None
    path_match = re.fullmatch(r"/(\d{5,20})\.html", parsed.path)
    return int(path_match.group(1)) if path_match else None


def valid_https_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def first_coupon_url(item: dict) -> str | None:
    coupon_info = item.get("couponInfo") or {}
    coupons = coupon_info.get("couponList") or []
    for coupon in coupons:
        link = coupon.get("link") if isinstance(coupon, dict) else None
        if valid_https_url(link):
            return link
    return None


def format_price(price_info: object) -> str:
    if not isinstance(price_info, dict):
        return "价格请以结算页为准"
    try:
        price = Decimal(str(price_info["price"]))
        if not price.is_finite() or price < 0:
            raise InvalidOperation
    except (KeyError, InvalidOperation):
        return "价格请以结算页为准"
    return f"展示价 ¥{price:.2f}"
