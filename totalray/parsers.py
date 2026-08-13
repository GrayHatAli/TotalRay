"""Parse subscription links and convert them into sing-box outbounds.

Supported protocols: vmess / vless / trojan / shadowsocks / hysteria2 / tuic
Each function returns an outbound dict matching the sing-box 1.13 schema
(without a tag).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import urllib.parse

log = logging.getLogger(__name__)

SUPPORTED_SCHEMES = ("vmess://", "vless://", "trojan://", "ss://",
                     "hysteria2://", "hy2://", "tuic://")


class ParseError(Exception):
    """Invalid link, or an unsupported protocol/feature."""


def _b64decode(data: str) -> bytes:
    data = (data.strip()
            .replace("\n", "").replace("\r", "").replace(" ", ""))
    data += "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data)
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"invalid base64: {exc}") from exc


def _b64str(data: str) -> str:
    return _b64decode(data).decode("utf-8", "replace")


def _qs(query: str) -> dict:
    return urllib.parse.parse_qs(query, keep_blank_values=True)


def _first(params: dict, key: str, default: str = "") -> str:
    vals = params.get(key)
    return vals[0] if vals else default


def _truthy(value: str) -> bool:
    return value.lower() in ("1", "true", "yes")


def _unquote(value: str) -> str:
    return urllib.parse.unquote(value or "")


def fingerprint(outbound: dict) -> str:
    clean = {k: v for k, v in outbound.items() if k != "tag"}
    raw = json.dumps(clean, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _build_tls(params: dict, default_sni: str = "", force: bool = False):
    security = _first(params, "security").lower()
    insecure = (_truthy(_first(params, "allowInsecure"))
                or _truthy(_first(params, "insecure")))
    if security in ("tls", "reality") or force:
        tls: dict = {"enabled": True}
        sni = _first(params, "sni") or _first(params, "peer") or default_sni
        if sni:
            tls["server_name"] = sni
        if insecure:
            tls["insecure"] = True
        fp = _first(params, "fp")
        if fp or security == "reality":
            # sing-box requires uTLS for Reality clients. Subscription links
            # often omit fp, so use the widely compatible Chrome fingerprint.
            tls["utls"] = {"enabled": True,
                            "fingerprint": fp or "chrome"}
        alpn = _first(params, "alpn")
        if alpn:
            tls["alpn"] = [a for a in alpn.split(",") if a]
        if security == "reality":
            pbk = _first(params, "pbk")
            if not pbk:
                raise ParseError("reality without a public_key (pbk)")
            tls["reality"] = {"enabled": True, "public_key": pbk}
            sid = _first(params, "sid")
            if sid:
                tls["reality"]["short_id"] = sid
        return tls
    return None


def _build_transport(network: str, params: dict, host: str = "", path: str = ""):
    network = (network or "tcp").lower()
    if network in ("tcp", "raw", "none", ""):
        header_type = _first(params, "headerType")
        if header_type == "http":
            tr = {"type": "http", "method": "GET",
                  "path": path or _first(params, "path", "/")}
            h = host or _first(params, "host")
            if h:
                tr["headers"] = {"Host": h.split(",")}
            return tr
        return None
    if network == "ws":
        tr = {"type": "ws", "path": _first(params, "path") or path or "/"}
        h = _first(params, "host") or host
        if h:
            tr["headers"] = {"Host": h.split(",")[0]}
        ed = _first(params, "ed")
        if ed.isdigit():
            tr["max_early_data"] = int(ed)
            tr["early_data_header_name"] = _first(
                params, "eh", "Sec-WebSocket-Protocol")
        return tr
    if network == "grpc":
        service = _first(params, "serviceName") or path
        if not service:
            raise ParseError("grpc without serviceName")
        return {"type": "grpc", "service_name": service}
    if network in ("h2", "http"):
        tr: dict = {"type": "http"}
        h = _first(params, "host") or host
        if h:
            tr["host"] = h.split(",")
        p = _first(params, "path") or path
        if p:
            tr["path"] = p
        return tr
    if network == "httpupgrade":
        tr = {"type": "httpupgrade"}
        h = _first(params, "host") or host
        if h:
            tr["host"] = h
        p = _first(params, "path") or path
        if p:
            tr["path"] = p
        return tr
    raise ParseError(f"unsupported transport: {network}")


def _parse_vmess(link: str):
    try:
        data = json.loads(_b64str(link[len("vmess://"):]))
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"invalid vmess JSON: {exc}") from exc

    try:
        port = int(data.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    ob = {
        "type": "vmess",
        "server": str(data.get("add") or ""),
        "server_port": port,
        "uuid": str(data.get("id") or ""),
        "security": str(data.get("scy") or "auto"),
    }
    if not ob["server"] or not ob["server_port"] or not ob["uuid"]:
        raise ParseError("incomplete vmess (add/port/id)")
    try:
        aid = int(data.get("aid") or 0)
    except (TypeError, ValueError):
        aid = 0
    if aid:
        ob["alter_id"] = aid

    tls_mode = str(data.get("tls") or "").lower()
    if tls_mode in ("tls", "reality"):
        params = {
            "security": [tls_mode],
            "sni": [str(data.get("sni") or "")],
            "alpn": [str(data.get("alpn") or "")],
            "fp": [str(data.get("fp") or "")],
        }
        tls = _build_tls(params, default_sni=str(data.get("host") or ""))
        if tls:
            ob["tls"] = tls

    host = str(data.get("host") or "")
    path = str(data.get("path") or "")
    tparams = {
        "headerType": [str(data.get("type") or "")],
        "host": [host],
        "path": [path],
        "serviceName": [path],
    }
    tr = _build_transport(str(data.get("net") or "tcp"), tparams,
                          host=host, path=path)
    if tr:
        ob["transport"] = tr
    return ob, str(data.get("ps") or "")


def _parse_vless(link: str):
    try:
        url = urllib.parse.urlsplit(link)
        port = url.port
    except ValueError as exc:
        raise ParseError(f"invalid vless: {exc}") from exc
    params = _qs(url.query)
    ob = {
        "type": "vless",
        "server": url.hostname or "",
        "server_port": port or 0,
        "uuid": _unquote(url.username),
    }
    if not ob["server"] or not ob["server_port"] or not ob["uuid"]:
        raise ParseError("incomplete vless (server/port/uuid)")
    flow = _first(params, "flow")
    if flow:
        ob["flow"] = flow
    tls = _build_tls(params, default_sni=ob["server"])
    if tls:
        ob["tls"] = tls
    tr = _build_transport(_first(params, "type", "tcp"), params)
    if tr:
        ob["transport"] = tr
    return ob, _unquote(url.fragment)


def _parse_trojan(link: str):
    try:
        url = urllib.parse.urlsplit(link)
        port = url.port
    except ValueError as exc:
        raise ParseError(f"invalid trojan: {exc}") from exc
    params = _qs(url.query)
    ob = {
        "type": "trojan",
        "server": url.hostname or "",
        "server_port": port or 0,
        "password": _unquote(url.username),
    }
    if not ob["server"] or not ob["server_port"] or not ob["password"]:
        raise ParseError("incomplete trojan (server/port/password)")
    ob["tls"] = _build_tls(params, default_sni=ob["server"], force=True)
    tr = _build_transport(_first(params, "type", "tcp"), params)
    if tr:
        ob["transport"] = tr
    return ob, _unquote(url.fragment)


def _parse_ss(link: str):
    rest = link[len("ss://"):]
    name = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
        name = _unquote(frag)
    plugin = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)
        plugin = _first(_qs(query), "plugin")
    if plugin:
        raise ParseError(f"shadowsocks plugin not supported: {plugin}")

    if "@" in rest:
        userinfo_raw, hostport = rest.rsplit("@", 1)
        userinfo = userinfo_raw if ":" in userinfo_raw else _b64str(userinfo_raw)
    else:
        decoded = _b64str(rest)
        if "@" not in decoded:
            raise ParseError("invalid shadowsocks link")
        userinfo, hostport = decoded.rsplit("@", 1)

    method, sep, password = userinfo.partition(":")
    if not sep:
        raise ParseError("shadowsocks without method:password")
    host, sep, port_s = hostport.rpartition(":")
    host = host.strip("[]")
    if not host or not port_s.isdigit():
        raise ParseError(f"invalid shadowsocks address: {hostport}")
    ob = {
        "type": "shadowsocks",
        "server": host,
        "server_port": int(port_s),
        "method": method,
        "password": _unquote(password),
    }
    return ob, name


def _parse_hysteria2(link: str):
    scheme = "hysteria2://" if link.startswith("hysteria2://") else "hy2://"
    try:
        url = urllib.parse.urlsplit(scheme + link[len(scheme):])
        port = url.port
    except ValueError as exc:
        raise ParseError(f"invalid hysteria2: {exc}") from exc
    params = _qs(url.query)
    ob = {
        "type": "hysteria2",
        "server": url.hostname or "",
        "server_port": port or 0,
        "password": _unquote(url.username),
    }
    if not ob["server"] or not ob["server_port"] or not ob["password"]:
        raise ParseError("incomplete hysteria2 (server/port/password)")
    tls: dict = {"enabled": True,
                 "server_name": _first(params, "sni") or ob["server"]}
    if _truthy(_first(params, "insecure")):
        tls["insecure"] = True
    alpn = _first(params, "alpn")
    if alpn:
        tls["alpn"] = [a for a in alpn.split(",") if a]
    ob["tls"] = tls
    obfs = _first(params, "obfs")
    if obfs:
        if obfs != "salamander":
            raise ParseError(f"unsupported hysteria2 obfs: {obfs}")
        ob["obfs"] = {"type": "salamander",
                      "password": _first(params, "obfs-password")}
    return ob, _unquote(url.fragment)


def _parse_tuic(link: str):
    try:
        url = urllib.parse.urlsplit(link)
        port = url.port
    except ValueError as exc:
        raise ParseError(f"invalid tuic: {exc}") from exc
    params = _qs(url.query)
    ob = {
        "type": "tuic",
        "server": url.hostname or "",
        "server_port": port or 0,
        "uuid": _unquote(url.username),
        "password": _unquote(url.password),
        "congestion_control": _first(params, "congestion_control", "cubic"),
    }
    if not ob["server"] or not ob["server_port"] or not ob["uuid"]:
        raise ParseError("incomplete tuic (server/port/uuid)")
    tls: dict = {"enabled": True,
                 "server_name": _first(params, "sni") or ob["server"]}
    if _truthy(_first(params, "insecure")) or _truthy(_first(params, "allow_insecure")):
        tls["insecure"] = True
    alpn = _first(params, "alpn")
    if alpn:
        tls["alpn"] = [a for a in alpn.split(",") if a]
    ob["tls"] = tls
    if _first(params, "udp_relay_mode"):
        ob["udp_relay_mode"] = _first(params, "udp_relay_mode")
    return ob, _unquote(url.fragment)


# Outbound "type" values we know how to test/build against. A full
# sing-box client config's "outbounds" array also contains selector/
# urltest/direct/block/dns entries describing the client's own local
# routing rather than an actual server, so this set doubles as the
# filter for "is this entry a real proxy leg".
SINGBOX_OUTBOUND_TYPES = {"vmess", "vless", "trojan", "shadowsocks", "hysteria2", "tuic"}


def is_singbox_config(text: str) -> bool:
    """Cheap check for whether a subscription returned a full sing-box
    client config (some panels serve this directly, for Hiddify/NekoBox-
    style clients) instead of a plain list of vmess://.../vless://...
    links."""
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return False
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and isinstance(data.get("outbounds"), list)


def parse_singbox_config(text: str) -> tuple[list, int, dict]:
    """Pull proxy outbounds directly out of a full sing-box client
    config's "outbounds" array, instead of expecting a link list.

    Each outbound is already in sing-box's own JSON shape - unlike the
    vmess://.../vless://... parsers above, there's no protocol-specific
    decoding to do. We just filter down to entries that are an actual
    proxy leg (a known outbound type with a server/server_port) and
    drop the rest (selector/urltest/direct/block/dns/etc., which
    describe the client's own local routing, not a server to test).
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return [], 0, {}
    obs = data.get("outbounds") if isinstance(data, dict) else None
    if not isinstance(obs, list):
        return [], 0, {}

    items, bad = [], 0
    reasons: dict = {}
    seen = set()
    for ob in obs:
        if not isinstance(ob, dict):
            continue
        typ = ob.get("type")
        if typ not in SINGBOX_OUTBOUND_TYPES:
            continue  # not a proxy leg (selector/urltest/direct/block/dns/...)
        if "server" not in ob or "server_port" not in ob:
            bad += 1
            category = f"{typ} outbound missing server/server_port"
            entry = reasons.setdefault(category, {"count": 0, "example": category})
            entry["count"] += 1
            continue
        outbound = {k: v for k, v in ob.items() if k != "tag"}
        fp = fingerprint(outbound)
        if fp in seen:
            continue
        seen.add(fp)
        name = ob.get("tag") or f"{ob['server']}:{ob['server_port']}"
        items.append({
            "fingerprint": fp,
            "name": str(name)[:120],
            "link": "",  # no URI form - this came from a JSON config
            "outbound": outbound,
        })
    return items, bad, reasons


_PARSERS = {
    "vmess://": _parse_vmess,
    "vless://": _parse_vless,
    "trojan://": _parse_trojan,
    "ss://": _parse_ss,
    "hysteria2://": _parse_hysteria2,
    "hy2://": _parse_hysteria2,
    "tuic://": _parse_tuic,
}


def parse_link(link: str) -> dict:
    link = link.strip()
    # Some subscription sources embed links inside HTML (or a template
    # that HTML-escapes them) without decoding entities back out, so the
    # query string arrives as "...&amp;pbk=...&amp;sni=..." instead of
    # "...&pbk=...&sni=...". Since that's not a real '&' separator,
    # urllib silently folds every later param into the first key's
    # value - most visibly, reality links losing "pbk" and getting
    # rejected as "reality without a public_key" even though the key is
    # right there in the raw text. Unescape before parsing so this
    # doesn't masquerade as corrupt/unsupported data.
    if "&amp;" in link:
        link = link.replace("&amp;", "&")
    for scheme, parser in _PARSERS.items():
        if link.lower().startswith(scheme):
            outbound, name = parser(link)
            return {
                "fingerprint": fingerprint(outbound),
                "name": (name or f"{outbound['server']}:{outbound['server_port']}")[:120],
                "link": link,
                "outbound": outbound,
            }
    raise ParseError(f"unsupported scheme: {link[:32]}")


def parse_many(links: list) -> tuple[list, int, dict]:
    """Returns (items, bad_count, reasons).

    reasons groups rejections by category (the part of the ParseError
    message before the first ':' - e.g. "invalid vless: missing uuid"
    groups under "invalid vless") so a caller can tell at a glance
    whether 23 rejected links were 23 different one-off malformed
    entries, or e.g. 23 copies of "unsupported hysteria2 obfs: salamander"
    (a parser limitation, not corrupt data). Each category keeps a count
    and one example message.
    """
    items, bad = [], 0
    reasons: dict = {}
    seen = set()
    for link in links:
        try:
            item = parse_link(link)
        except ParseError as exc:
            bad += 1
            msg = str(exc)
            category = msg.split(":", 1)[0].strip()
            entry = reasons.setdefault(category, {"count": 0, "example": msg})
            entry["count"] += 1
            log.debug("rejected link: %s", msg)
            continue
        except Exception as exc:  # noqa: BLE001
            bad += 1
            msg = f"unexpected error: {exc}"
            entry = reasons.setdefault("unexpected error", {"count": 0, "example": msg})
            entry["count"] += 1
            log.warning("unexpected error parsing a link, skipping it: %s", exc)
            continue
        if item["fingerprint"] in seen:
            continue
        seen.add(item["fingerprint"])
        items.append(item)
    return items, bad, reasons
