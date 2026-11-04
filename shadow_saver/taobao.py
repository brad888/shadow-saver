"""Official TOP activity API; ordinary product conversion needs separate approval."""
import hashlib
import re
from datetime import datetime, time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

CHINA = ZoneInfo('Asia/Shanghai')


class TaobaoUnionClient:
    def __init__(self, app_key: str, app_secret: str, pid: str, client: httpx.AsyncClient):
        match = re.fullmatch(r'mm_(\d+)_(\d+)_(\d+)', pid)
        if not match:
            raise ValueError('Invalid TAOBAO_PID')
        self.app_key, self.app_secret = app_key, app_secret
        self.adzone_id = match.group(3)
        self.client = client

    async def _call(self, method: str, **payload: str) -> dict:
        params = {'method': method, 'app_key': self.app_key, 'format': 'json',
                  'v': '2.0', 'sign_method': 'md5',
                  'timestamp': datetime.now(CHINA).strftime('%Y-%m-%d %H:%M:%S'), **payload}
        raw = self.app_secret + ''.join(k + v for k, v in sorted(params.items())) + self.app_secret
        params['sign'] = hashlib.md5(raw.encode()).hexdigest().upper()
        response = await self.client.post('https://eco.taobao.com/router/rest', data=params)
        response.raise_for_status()
        body = response.json()
        error = body.get('error_response')
        if isinstance(error, dict):
            raise ValueError(f"Taobao API rejected ({error.get('code')}): {error.get('sub_code', '')}")
        envelope = body.get(method.removeprefix('taobao.').replace('.', '_') + '_response')
        if not isinstance(envelope, dict) or not isinstance(envelope.get('data'), dict):
            raise ValueError('Taobao API result missing')
        return envelope['data']

    async def activity(self, material_id: str, *, now: datetime | None = None) -> list[dict]:
        data = await self._call('taobao.tbk.activity.info.get',
                                activity_material_id=material_id, adzone_id=self.adzone_id)
        now = now or datetime.now(CHINA)
        start = activity_time(data.get('page_start_time'), end=False)
        end = activity_time(data.get('page_end_time'), end=True)
        title = data.get('page_name')
        url = next((data.get(k) for k in ('short_click_url', 'click_url')
                    if promotion_url(data.get(k))), None)
        if not (start and end and start <= now <= end and url and isinstance(title, str) and title.strip()):
            return []
        offer = {'platform': 'taobao', 'title': title.strip()[:120],
                 'price_label': '官方活动；优惠资格与金额以活动页为准', 'url': url,
                 'expires_at': end.isoformat()}
        try:
            token = await self._call('taobao.tbk.tpwd.create', text=offer['title'], url=url)
            if isinstance(token.get('model'), str) and token['model'].strip():
                offer['tao_token'] = token['model'].strip()[:2000]
        except (ValueError, httpx.HTTPError):
            # An optional copyable token must not hide a valid official promotion link.
            pass
        return [offer]


def fixed_activity(title: str, url: str, token: str, expires: str,
                   *, now: datetime | None = None) -> list[dict]:
    end = activity_time(expires, end=True)
    if not (title.strip() and promotion_url(url) and end and (now or datetime.now(CHINA)) <= end):
        return []
    offer = {'platform': 'taobao', 'title': title.strip()[:120], 'url': url,
             'price_label': '官方活动；优惠资格与金额以活动页为准', 'expires_at': expires}
    if token.strip():
        offer['tao_token'] = token.strip()[:2000]
    return [offer]


def promotion_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        host = parsed.hostname or ''
        return parsed.scheme == 'https' and not parsed.username and not parsed.password and any(
            host == domain or host.endswith('.' + domain) for domain in ('taobao.com', 'tmall.com', 'tb.cn'))
    except ValueError:
        return False


def activity_time(value: object, *, end: bool) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
        if len(value.strip()) == 10:
            parsed = datetime.combine(parsed.date(), time.max if end else time.min)
        return parsed.replace(tzinfo=CHINA) if parsed.tzinfo is None else parsed.astimezone(CHINA)
    except ValueError:
        return None
