"""www.tokyotosho.info — Tokyotosho（动漫社区）RSS 搜索。含 base32 magnet + size；无 seeders 数值。"""
from .base import BaseSource, TorrentResult, SearchFilters, _http_get
import xml.etree.ElementTree as ET, urllib.parse, re, base64, binascii, email.utils

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.6"


def _b32_to_hex(b32):
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


class TokyoToshoSource(BaseSource):
    name = "tokyotosho"
    priority = 75   # 动漫补充召回；RSS 无 seeders

    def _rows(self, query, timeout=10):
        url = "https://www.tokyotosho.info/rss.php?search=%s" % urllib.parse.quote(query)
        ok, lat, code, text = _http_get(url, timeout=timeout, ua=UA)
        if not ok:
            return ok, lat, code, []
        try:
            root = ET.fromstring(text)
        except Exception:
            return ok, lat, code, []
        out_rows = []
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            if not title:
                continue
            desc = it.findtext("description") or ""
            m = re.search(r"magnet:\?xt=urn:btih:([A-Za-z2-7]{32}|[a-fA-F0-9]{40})", desc)
            if not m:
                continue
            ih = m.group(1)
            if len(ih) == 32:
                ih = _b32_to_hex(ih)
                if not ih:
                    continue
            # size： RSS 里形如 Size: 31.55MB
            sm = re.search(r"Size:\s*([\d.]+)\s*(TB|GB|MB)", desc, re.I)
            size = None
            if sm:
                mul = {"TB": 1024**4, "GB": 1024**3, "MB": 1024**2}[sm.group(2).upper()]
                size = int(float(sm.group(1)) * mul)
            pub = it.findtext("pubDate")
            published = None
            if pub:
                try:
                    published = email.utils.parsedate_to_datetime(pub).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    published = pub
            cat = (it.findtext("category") or "Anime").strip()
            out_rows.append({"title": title, "hash": ih.lower(), "size": size,
                             "published": published, "category": cat})
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
                seeders=None, leechers=None,
                files=None, source=self.name, category=it["category"],
                published_at=it.get("published"), quote="rss_page"))
        return out, {"ok": ok, "http": code, "latency": lat, "error": None if ok else "tokyotosho 不可达"}

    def test(self):
        ok, lat, code, rows = self._rows("frieren")
        return {"status": ok, "latency": lat, "http_status": code, "result_count": len(rows),
                "note": "tokyotosho RSS（动漫，无 seeders，排序权重较低）"}
