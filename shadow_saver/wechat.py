import hashlib
import hmac
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class IncomingMessage:
    to_user: str
    from_user: str
    msg_id: str
    input_type: str
    input_value: str
    source_platform: str | None


def valid_signature(token: str, timestamp: str, nonce: str, signature: str) -> bool:
    if not all((token, timestamp, nonce, signature)):
        return False
    expected = hashlib.sha1("".join(sorted((token, timestamp, nonce))).encode()).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_message(body: bytes) -> IncomingMessage | None:
    if len(body) > 2_000_000 or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("Invalid XML payload")
    root = ET.fromstring(body)
    field = lambda name: root.findtext(name, default="").strip()
    kind = field("MsgType")
    if kind == "text":
        value = field("Content")
        if not value:
            return None
        match = re.search(r"https?://[^\s<>]+", value)
        host = (urlparse(match.group(0)).hostname or "").lower() if match else ""
        platform = platform_from_host(host)
        input_type = "link" if match else "text"
    elif kind == "link":
        value = field("Url")
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        platform = platform_from_host(parsed.hostname.lower())
        input_type = "link"
    elif kind == "image":
        value = field("MediaId")
        if not value:
            return None
        platform = None
        input_type = "image"
    else:
        return None
    if not field("ToUserName") or not field("FromUserName") or not field("MsgId"):
        raise ValueError("Missing required message fields")
    return IncomingMessage(field("ToUserName"), field("FromUserName"), field("MsgId"), input_type, value, platform)


def platform_from_host(host: str) -> str | None:
    domains = {
        "jd": ("jd.com", "3.cn"),
        "taobao": ("taobao.com", "tmall.com", "tb.cn"),
        "pdd": ("pinduoduo.com", "yangkeduo.com"),
    }
    for name, suffixes in domains.items():
        if any(host == suffix or host.endswith("." + suffix) for suffix in suffixes):
            return name
    return None


def text_reply(to_user: str, from_user: str, content: str) -> bytes:
    root = ET.Element("xml")
    for name, value in (
        ("ToUserName", to_user), ("FromUserName", from_user),
        ("CreateTime", str(__import__("time").time_ns() // 1_000_000_000)),
        ("MsgType", "text"), ("Content", content),
    ):
        ET.SubElement(root, name).text = value
    return ET.tostring(root, encoding="utf-8")

