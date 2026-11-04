import asyncio
from datetime import datetime
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import httpx
import pytest

from shadow_saver.taobao import TaobaoUnionClient


NOW = datetime(2026, 10, 27, 12, tzinfo=ZoneInfo('Asia/Shanghai'))


def activity(**changes):
    return {'page_name': '官方红包活动', 'page_start_time': '2026-10-20',
            'page_end_time': '2026-11-11', 'click_url': 'https://s.click.taobao.com/verified', **changes}


def lookup(data, token_error=False):
    def respond(request):
        form = dict(parse_qsl(request.content.decode()))
        if form['method'] == 'taobao.tbk.activity.info.get':
            assert form['adzone_id'] == '333'
            assert form['activity_material_id'] == 'campaign'
            assert 'sub_pid' not in form
            return httpx.Response(200, json={'tbk_activity_info_get_response': {'data': data}})
        if token_error:
            return httpx.Response(200, json={'error_response': {'code': 11, 'sub_code': 'no-permission'}})
        return httpx.Response(200, json={'tbk_tpwd_create_response': {'data': {'model': '￥TEST￥'}}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            return await TaobaoUnionClient('key', 'secret', 'mm_111_222_333', transport).activity('campaign', now=NOW)
    return asyncio.run(run())


def test_valid_activity_has_official_link_and_taobao_token():
    assert lookup(activity()) == [{'platform': 'taobao', 'title': '官方红包活动',
        'price_label': '官方活动；优惠资格与金额以活动页为准',
        'url': 'https://s.click.taobao.com/verified', 'tao_token': '￥TEST￥',
        'expires_at': '2026-11-11T23:59:59.999999+08:00'}]


@pytest.mark.parametrize('changes', [
    {'page_end_time': '2026-10-26'}, {'page_start_time': '2026-10-28'},
    {'page_end_time': 'invalid'}, {'page_end_time': ''},
    {'click_url': 'javascript:alert(1)'}, {'click_url': 'https://taobao.com.evil.test/x'},
    {'click_url': ''}, {'page_name': ''},
])
def test_unusable_activity_is_not_presented_as_an_offer(changes):
    assert lookup(activity(**changes)) == []


def test_token_failure_preserves_verified_activity_link():
    offers = lookup(activity(), token_error=True)
    assert len(offers) == 1
    assert offers[0]['url'] == 'https://s.click.taobao.com/verified'
    assert 'tao_token' not in offers[0]


def test_permission_error_is_not_an_empty_success():
    async def run():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
            'error_response': {'code': 11, 'sub_code': 'isv.permission-api-package-limit'}}))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError, match='11.*isv.permission-api-package-limit'):
                await TaobaoUnionClient('key', 'secret', 'mm_111_222_333', client).activity('campaign', now=NOW)
    asyncio.run(run())


@pytest.mark.parametrize(('expires', 'url', 'expected'), [
    ('2026-10-28T23:59:59+08:00', 'https://s.click.taobao.com/fixed', True),
    ('2026-10-26T23:59:59+08:00', 'https://s.click.taobao.com/fixed', False),
    ('', 'https://s.click.taobao.com/fixed', False),
    ('2026-10-28T23:59:59+08:00', 'https://evil.test/x', False),
])
def test_fixed_campaign_requires_trusted_link_and_future_cutoff(expires, url, expected):
    from shadow_saver.taobao import fixed_activity
    offers = fixed_activity('双11超级红包', url, '原样淘口令', expires, now=NOW)
    assert bool(offers) is expected
    if expected:
        assert offers[0]['tao_token'] == '原样淘口令'
        assert offers[0]['expires_at'] == expires
