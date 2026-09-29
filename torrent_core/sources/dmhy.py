"""-share.dmhy.org 動漫花園（中文/动漫社区）— RSS 搜索。
RSS item 有 magnet（base32 btih）⇒ 转 hex；无 seeders/size 数值 → None（不伪造）。
对中文资源召回新增不可替代性；目前优先级缺省（priority 参见类属性）。"""
from .base import BaseSource, TorrentResult, SearchFilters, _http_get
import xml.etree.ElementTree as ET, urllib.parse, re, base64, binascii, email.utils

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.6"


def _b32_to_hex(b32):
    """dmhy magnet 用 base32 btih → 转 40 位 hex"""
    b32 = b32.upper()
    pad = "=" * ((8 - len(b32) % 8) % 8)
    try:
        raw = base64.b32decode(b32 + pad)
        hexs = binascii.hexlify(raw) if hasattr(binascii, "hexlify") else None
        if hexs is None:
            import binascii as _b
            hexs = _b.hexlify(raw)
        hexs = hexs.decode()
        return hexs if len(hexs) == 40 else None
    except Exception:
        return None


class DmhySource(BaseSource):
    name = "dmhy"
    priority = 70   # 其它：中文/动漫补充召回；缺 seeders → 排序靠后

    def _rows(self, query, timeout=10):
        url = "https://share.dmhy.org/topics/rss/sort_id/0/keyword/%s" % urllib.parse.quote(query)
        ok, lat, code, text = _http_get(url, timeout=timeout, ua=UA)
        if not ok:
            return ok, lat, code, []
        try:
            root = ET.fromstring(text)
        except Exception:
            return ok, lat, code, []
        out_rows = []
        for it in root.iter("item"):
            title = it.findtext("title") or ""
            link = it.findtext("link") or ""
            enc = it.find("enclosure")
            raw = (enc.get("url") if enc is not None else "") or ""
            m = re.search(r"magnet:\?xt=urn:btih:([A-Za-z2-7]{32}|[a-fA-F0-9]{40})", raw)
            if not m:
                continue
            ih = m.group(1)
            if len(ih) == 32:
                ih = _b32_to_hex(ih)
                if not ih:
                    continue
            pub = it.findtext("pubDate")
            published = None
            if pub:
                try:
                    published = email.utils.parsedate_to_datetime(pub).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    published = pub
            # size 从 title 中常见的 [699MB] 提取（.dmhy 特点，不精确时留 None）
            size_match = re.search(r"\[(\d+(?:\.\d+)?)\s?(GB|MB|TB|GiB|MiB)\]", title, re.I)
            size = None
            if size_match:
                mul = {"TB": 1024**4, "GB": 1024**3, "MB": 1024**2}.get(size_match.group(2).upper(), 0)
                if mul:
                    size = int(float(size_match.group(1)) * mul)
            out_rows.append({
                "title": title, "hash": ih.lower(), "size": size, "published": published,
                "category": "Anime/Chinese"})
        return ok, lat, code, out_rows

    def search(self, query, filters: SearchFilters):
        cfg = getattr(self, '_runtime_cfg', {'timeout': 10})
        ok, lat, code, rows = self._rows(query, cfg.get("timeout", 10))
        out = []
        for it in rows[: filters.max_per_source]:
            out.append(TorrentResult(
                title=it["title"], info_hash=it["hash"],
                magnet="magnet:?xt=urn:btih:%s&dn=%s" % (it["hash"], urllib.parse.quote(it["title"])),
                size=it.get("size"),
                seeders=None,      # dmhy RSS 不提供（不伪造）
                leechers=None,
                files=None, source=self.name, category=it["category"],
                published_at=it.get("published"), quote="rss_page"))
        return out, {"ok": ok, "http": code, "latency": lat, "error": None if ok else "dmhy 不可达"}

    def test(self):
        ok, lat, code, rows = self._rows("bleach")
        return {"status": ok, "latency": lat, "http_status": code, "result_count": len(rows),
                "note": "dmhy RSS（中文/动漫社区，无 seeders 字段，排序权重较低）"}
