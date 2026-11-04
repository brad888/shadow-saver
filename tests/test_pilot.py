from decimal import Decimal
import pytest
from shadow_saver.pilot import normalize, validate_url, create_pilot_app
from fastapi.testclient import TestClient


def test_future_coupon_is_not_available():
    assert normalize({'itemid':'a','itemtitle':'耳机','itemprice':'100','itemendprice':'80','couponmoney':'20','couponstarttime':'9999999999'}, '淘宝')['price'] is None


def test_rebate_uses_commission_and_rounds_to_cents():
    item = normalize({'itemid':'a','goodsname':'耳机','itemprice':'1399','itemendprice':'1399','commission':'13.99'}, '京东')
    assert item['rebate'] == '2.80'
    assert item['price'] == '1399.00'


@pytest.mark.parametrize('url',['http://item.jd.com/1.html','https://127.0.0.1','https://jd.com.evil.test/','https://user:pass@item.jd.com/'])
def test_unsafe_source_urls_rejected(url):
    with pytest.raises(ValueError): validate_url(url)


def test_claim_requires_owned_promotion_and_cannot_credit_money(tmp_path):
    app = create_pilot_app(str(tmp_path/'pilot.db'), config={})
    with TestClient(app) as a, TestClient(app) as b:
        a.get('/'); b.get('/')
        assert a.post('/api/claims',json={'promotion_id':'missing','order_no':'123456789'}).status_code == 404
        with app.state.db() as db:
            owner = a.cookies.get('pilot_visitor')
            db.execute('INSERT INTO promotions(id,owner,platform,title,url,rebate) VALUES (?,?,?,?,?,?)',('p1',owner,'京东','耳机','https://u.jd.com/a','2.80'))
        assert b.post('/api/claims',json={'promotion_id':'p1','order_no':'123456789'}).status_code == 404
        result=a.post('/api/claims',json={'promotion_id':'p1','order_no':'123456789','amount':999}).json()
        assert result['status']=='待人工核账'
        assert 'amount' not in result
        assert a.post('/api/claims',json={'promotion_id':'p1','order_no':'123456789'}).status_code == 409
        assert b.get('/api/claims').json()==[]


def test_partial_search_failure_is_safe_and_other_platform_survives(tmp_path):
    class PartialProvider:
        async def search(self,client,keyword,platform):
            if platform=='淘宝': raise RuntimeError('secret-must-not-leak')
            return [normalize({'itemid':'sku','goodsname':'耳机','itemprice':'100','commission':'5'},platform)]
        async def convert(self,client,item,tracking): return 'https://u.jd.com/example',''
    app=create_pilot_app(str(tmp_path/'partial.db'),{'HAODANKU_API_KEY':'test'},PartialProvider())
    with TestClient(app) as a, TestClient(app) as b:
        response=a.post('/api/search',json={'query':'耳机'})
        assert response.status_code==200
        assert 'secret-must-not-leak' not in response.text
        data=response.json()
        assert len(data['items'])==1
        assert data['platforms'][0]['error']
        args={'search_id':data['id'],'item_id':data['items'][0]['id']}
        assert b.post('/api/promotions',json=args).status_code==404
        assert a.post('/api/promotions',json=args).status_code==200
        assert a.post('/api/claims',headers={'Origin':'https://evil.test'},json={}).status_code==403


def test_link_resolver_rejects_redirect_to_private_network():
    import asyncio, httpx
    from shadow_saver.pilot import resolve_title
    seen=[]
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302,headers={'location':'https://127.0.0.1/private'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError): await resolve_title(client,'https://item.jd.com/1.html')
    asyncio.run(run())
    assert seen==['https://item.jd.com/1.html']


def test_upgrade_model_is_excluded_even_when_old_model_in_ad_copy():
    import asyncio
    from shadow_saver.pilot import Provider
    class SampleProvider(Provider):
        async def call(self,*args,**kwargs):
            return [{'itemid':'wrong','goodsname':'索尼 WH-1000XM6 耳机 WH-1000XM5升级版','itemprice':'3800'}, {'itemid':'right','goodsname':'索尼 WH-1000XM5 耳机','itemprice':'1399'}]
    rows=asyncio.run(SampleProvider({}).search(None,'索尼 WH-1000XM5','京东'))
    assert [r['item_id'] for r in rows]==['right']


def test_pilot_rejects_unexpected_host(tmp_path):
    with TestClient(create_pilot_app(str(tmp_path/'host.db'),{}),base_url='http://evil.test') as client:
        assert client.get('/').status_code==400
