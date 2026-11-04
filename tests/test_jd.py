import asyncio
import json

import httpx
import pytest

from shadow_saver.jd import JDUnionClient


@pytest.mark.parametrize(
    ("suffix", "goods_field", "promotion_field"),
    [("response", "result", "result"), ("responce", "queryResult", "getResult")],
)
def test_jd_keyword_search_returns_only_signed_promotion_links(suffix, goods_field, promotion_field):
    methods = []

    def respond(request: httpx.Request) -> httpx.Response:
        form = dict(__import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl(request.content.decode()))
        methods.append(form["method"])
        assert form["app_key"] == "app-key"
        assert len(form["sign"]) == 32
        if form["method"] == "jd.union.open.goods.query":
            goods = {"code": 200, "data": [{"skuId": 123456, "skuName": "索尼耳机", "priceInfo": {"price": 999.0}}]}
            return httpx.Response(200, json={f"jd_union_open_goods_query_{suffix}": {"code": "0", goods_field: json.dumps(goods)}})
        assert form["method"] == "jd.union.open.promotion.common.get"
        promotion = {"code": 200, "data": {"clickURL": "https://u.jd.com/affiliate-123"}}
        return httpx.Response(200, json={f"jd_union_open_promotion_common_get_{suffix}": {"code": "0", promotion_field: json.dumps(promotion)}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    offers = asyncio.run(JDUnionClient("app-key", "secret", "12345", client).search("索尼耳机"))
    asyncio.run(client.aclose())
    assert methods == ["jd.union.open.goods.query", "jd.union.open.promotion.common.get"]
    assert offers == [{"platform": "jd", "title": "索尼耳机", "price_label": "展示价 ¥999.00", "url": "https://u.jd.com/affiliate-123"}]


def test_jd_does_not_return_unconverted_product_url():
    def respond(request: httpx.Request) -> httpx.Response:
        form = dict(__import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl(request.content.decode()))
        if form["method"] == "jd.union.open.goods.query":
            goods = {"code": 200, "data": [{"skuId": 123456, "skuName": "索尼耳机", "priceInfo": {"price": 999.0}}]}
            return httpx.Response(200, json={"jd_union_open_goods_query_response": {"result": json.dumps(goods)}})
        return httpx.Response(200, json={"jd_union_open_promotion_common_get_response": {"result": json.dumps({"code": 400, "data": {}})}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    offers = asyncio.run(JDUnionClient("app-key", "secret", "12345", client).search("索尼耳机"))
    asyncio.run(client.aclose())
    assert offers == []


def test_jd_reports_real_permission_rejection_instead_of_envelope_error():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "jd_union_open_goods_query_responce": {
                "code": "0",
                "queryResult": json.dumps({"code": 403, "message": "无访问权限", "requestId": "test-request"}),
            },
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            with pytest.raises(ValueError, match="403.*无访问权限"):
                await JDUnionClient("app-key", "secret", "12345", transport).search("纸巾")

    asyncio.run(run())
