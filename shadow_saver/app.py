import xml.etree.ElementTree as ET
import logging
from datetime import datetime

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from wechatpy.crypto import PrpCrypto, WeChatCrypto

from .config import Settings
from .jd import JDUnionClient
from .taobao import CHINA, TaobaoUnionClient, activity_time, fixed_activity
from .store import QueryStore
from .wechat import parse_message, text_reply, valid_signature
from .vision import identify_wechat_image

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, shopping_entry=None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = QueryStore(settings.database_path)
    app = FastAPI(title="ShadowSaver")
    if settings.wechat_encoding_aes_key and not settings.wechat_app_id:
        raise RuntimeError("WECHAT_APP_ID is required for encrypted WeChat messages")
    crypto = (
        WeChatCrypto(settings.wechat_token, settings.wechat_encoding_aes_key, settings.wechat_app_id)
        if settings.wechat_encoding_aes_key else None
    )

    def check_signature(timestamp: str, nonce: str, signature: str) -> None:
        if not valid_signature(settings.wechat_token, timestamp, nonce, signature):
            raise HTTPException(403, "Invalid WeChat signature")

    @app.get("/wechat", response_class=PlainTextResponse)
    def verify_wechat(
        timestamp: str = "", nonce: str = "", signature: str = "", echostr: str = "",
        msg_signature: str = ""
    ) -> str:
        if crypto and msg_signature:
            try:
                return crypto._check_signature(
                    msg_signature, timestamp, nonce, echostr, PrpCrypto
                )
            except Exception:
                raise HTTPException(403, "Invalid encrypted WeChat challenge") from None
        check_signature(timestamp, nonce, signature)
        return echostr

    @app.post("/wechat")
    async def receive_wechat(
        request: Request, background_tasks: BackgroundTasks,
        timestamp: str = "", nonce: str = "", signature: str = "",
        msg_signature: str = "", encrypt_type: str = ""
    ) -> Response:
        body = await request.body()
        if crypto:
            if not msg_signature or encrypt_type != "aes" or not timestamp or not nonce:
                raise HTTPException(403, "Encrypted WeChat message required")
            try:
                body = crypto.decrypt_message(body, msg_signature, timestamp, nonce).encode()
            except Exception:
                raise HTTPException(403, "Invalid encrypted WeChat message") from None
        else:
            check_signature(timestamp, nonce, signature)
        try:
            incoming = parse_message(body)
        except (ValueError, ET.ParseError):
            raise HTTPException(400, "Invalid message") from None
        if incoming is None:
            return PlainTextResponse("success")
        if shopping_entry and incoming.input_value.strip() not in {
            "红包", "淘宝红包", "领红包", "活动", "淘宝活动",
        }:
            if incoming.input_type == "image":
                content = "识图暂未开放，请发商品链接，或品牌、型号和规格，例如：索尼 WH-1000XM5 深夜蓝。"
            else:
                entry = shopping_entry(incoming)
                content = f"点击查询商品优惠：\n{settings.public_base_url}{entry}\n入口15分钟内有效，请勿转发。优惠以平台结算页为准，返利需佣金结算后人工核实。"
            xml = text_reply(incoming.from_user, incoming.to_user, content)
            if crypto:
                xml = crypto.encrypt_message(xml, nonce, timestamp)
            return Response(xml, media_type="text/xml; charset=utf-8")
        activity_request = incoming.input_type == "text" and incoming.input_value.strip() in {
            "红包", "淘宝红包", "领红包", "活动", "淘宝活动",
        }
        fixed_offers = fixed_activity(settings.taobao_activity_title, settings.taobao_activity_url,
                                      settings.taobao_activity_token, settings.taobao_activity_expires)
        activity_ready = bool(fixed_offers) or all((settings.taobao_app_key, settings.taobao_app_secret,
                                                   settings.taobao_pid, settings.taobao_activity_id))
        if activity_request and not activity_ready:
            xml = text_reply(incoming.from_user, incoming.to_user, "活动入口暂未开放，请稍后再试。")
            if crypto:
                xml = crypto.encrypt_message(xml, nonce, timestamp)
            return Response(xml, media_type="text/xml; charset=utf-8")
        query_id = store.create(
            incoming.msg_id, incoming.input_type, incoming.input_value, incoming.source_platform
        )
        jd_ready = all((settings.jd_app_key, settings.jd_app_secret, settings.jd_site_id))
        if activity_request:
            if fixed_offers:
                store.set_offers(query_id, fixed_offers)
            else:
                background_tasks.add_task(_lookup_taobao_activity, query_id, store, settings)
        elif incoming.input_type == "image":
            if all((settings.wechat_app_id, settings.wechat_app_secret, settings.deepseek_api_key)):
                background_tasks.add_task(_identify_image, query_id, incoming.input_value, store, settings)
            else:
                store.set_unavailable(query_id)
        elif jd_ready and (incoming.input_type == "text" or incoming.source_platform == "jd"):
            background_tasks.add_task(_lookup_jd, query_id, incoming.input_value, store, settings)
        else:
            store.set_unavailable(query_id)
        result_url = f"{settings.public_base_url}/result/{query_id}"
        content = f"已收到，点击查看本次查询结果：\n{result_url}\n商品及优惠请以平台结算页为准。"
        xml = text_reply(incoming.from_user, incoming.to_user, content)
        if crypto:
            xml = crypto.encrypt_message(xml, nonce, timestamp)
        return Response(xml, media_type="text/xml; charset=utf-8")

    @app.get("/api/result/{query_id}")
    def get_result(query_id: str) -> dict:
        result = store.get(query_id)
        if result is None:
            raise HTTPException(404, "Query not found")
        now = datetime.now(CHINA)
        result['offers'] = [offer for offer in result['offers'] if 'expires_at' not in offer or
                            ((end := activity_time(offer['expires_at'], end=True)) is not None and now <= end)]
        return result

    @app.get("/result/{query_id}", response_class=HTMLResponse)
    def show_result(query_id: str) -> HTMLResponse:
        if store.get(query_id) is None:
            raise HTTPException(404, "Query not found")
        return HTMLResponse(_result_page(query_id))

    return app


async def _lookup_taobao_activity(query_id: str, store: QueryStore, settings: Settings) -> None:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            offers = await TaobaoUnionClient(
                settings.taobao_app_key, settings.taobao_app_secret, settings.taobao_pid, client,
            ).activity(settings.taobao_activity_id)
        store.set_offers(query_id, offers)
    except Exception:
        logger.exception("Taobao activity lookup failed for query %s", query_id)
        store.set_failed(query_id)


async def _identify_image(query_id: str, media_id: str, store: QueryStore, settings: Settings) -> None:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            query = await identify_wechat_image(media_id, settings, client)
        store.set_search_query(query_id, query)
        if all((settings.jd_app_key, settings.jd_app_secret, settings.jd_site_id)):
            await _lookup_jd(query_id, query, store, settings)
        else:
            store.set_unavailable(query_id)
    except Exception:
        logger.exception("Image identification failed for query %s", query_id)
        store.set_failed(query_id)


async def _lookup_jd(query_id: str, query: str, store: QueryStore, settings: Settings) -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            offers = await JDUnionClient(
                settings.jd_app_key, settings.jd_app_secret, settings.jd_site_id, client
            ).search(query)
        store.set_offers(query_id, offers)
    except Exception:
        logger.exception("JD lookup failed for query %s", query_id)
        store.set_failed(query_id)


def _result_page(query_id: str) -> str:
    # query_id is generated by token_urlsafe and is never interpreted as HTML.
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>省钱影子查询</title>
<style>body{{font-family:system-ui,sans-serif;margin:0;background:#f6f8f7;color:#17231d}}
main{{max-width:560px;margin:40px auto;padding:24px;background:white;border-radius:18px}}
h1{{font-size:24px}} .muted{{color:#64726a}} .card{{border:1px solid #e2e9e5;border-radius:12px;padding:16px;margin-top:12px}}
a{{color:#167345}}</style></head><body><main><h1>省钱影子</h1>
<p id="status">正在读取查询结果…</p><div id="offers"></div>
<p class="muted">优惠和价格可能变化，请以购物平台结算页为准。推广链接可能产生佣金。</p></main>
<script>async function refresh() {{ try {{ const r=await fetch('/api/result/{query_id}'); const data=await r.json();
const names={{jd:'京东',taobao:'淘宝',pdd:'拼多多'}};
const status={{awaiting_vision:'正在识别图片…',awaiting_provider:'正在查询优惠…',ready:'查询完成。',failed:'查询失败，请稍后重试。',unavailable:'暂时无法查询该内容。可发送“红包”查看已开放的官方活动。'}};
document.getElementById('status').textContent=status[data.status]||'查询状态未知。';
const box=document.getElementById('offers');
box.replaceChildren();
if(data.status==='ready' && data.offers.length===0) document.getElementById('status').textContent='暂未找到可验证的推广优惠。';
for(const offer of data.offers){{const card=document.createElement('div');card.className='card';
const title=document.createElement('strong');title.textContent=offer.title;card.appendChild(title);
const line=document.createElement('p');line.textContent=(names[offer.platform]||offer.platform)+' · '+offer.price_label;card.appendChild(line);
const link=document.createElement('a');link.textContent=offer.tao_token?'打开官方活动':'查看优惠';link.href=offer.url;link.rel='nofollow noopener';card.appendChild(link);
if(offer.tao_token){{const hint=document.createElement('p');hint.textContent='在微信中无法打开时，可复制下方淘口令后打开淘宝：';card.appendChild(hint);const token=document.createElement('textarea');token.readOnly=true;token.value=offer.tao_token;token.setAttribute('aria-label','淘口令');token.style.width='100%';card.appendChild(token);}}
box.appendChild(card);}}
if(data.status==='awaiting_vision'||data.status==='awaiting_provider') setTimeout(refresh, 3000);
}} catch(e) {{document.getElementById('status').textContent='暂时无法读取结果，请刷新重试。';}} }}
refresh();</script></body></html>"""


