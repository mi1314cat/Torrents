"""nyaa.si RSS。字段含 nyaa:infoHash 等；files 在 RSS 里不存在 → None。"""
from .base import BaseSource, TorrentResult, SearchFilters, _http_get
import xml.etree.ElementTree as ET, urllib.parse

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.6"
NS = {"nyaa": "https://nyaa.si/xmlns/nyaa"}


def _size_to_bytes(s):
    mul = 1024**3 if "GiB" in s else 1024**2 if "MiB" in s else 1024
    return int(float(s.split(" ")[0]) * mul)


class NyaaSource(BaseSource):
    name = "nyaa"

    def _rows(self, query, timeout=10):
        url = "https://nyaa.si/?page=rss&q=%s" % urllib.parse.quote(query)
        ok, lat, code, text = _http_get(url, timeout=timeout, ua=UA)
        if not ok:
            return ok, lat, code, []
        try:
            root = ET.fromstring(text)
        except Exception:
            return ok, lat, code, []
        items = []
        for it in root.iter("item"):
            def d(tag):
                return it.findtext("nyaa:%s" % tag, default=None, namespaces=NS)
            items.append({
                "title": it.findtext("title"),
                "infohash": d("infoHash"),
                "size": d("size"), "seeders": d("seeders"), "leechers": d("leechers"),
                "category": it.findtext("nyaa:category", namespaces=NS),
                "published_at": it.findtext("pubDate"),
            })
        return ok, lat, code, items

    def search(self, query, filters: SearchFilters):
        cfg = getattr(self, '_runtime_cfg', {'timeout': 10})
        ok, lat, code, rows = self._rows(query, cfg.get("timeout", 10))
        out = []
        for it in rows[: filters.max_per_source]:
            ih = (it.get("infohash") or "").lower()
            if len(ih) != 40:
                continue
            try:
                size = _size_to_bytes(it["size"]) if it.get("size") else None
            except Exception:
                size = None
            try:
                seeders = int(it["seeders"]) if it.get("seeders") else None
            except Exception:
                seeders = None
            try:
                leechers = int(it["leechers"]) if it.get("leechers") else None
            except Exception:
                leechers = None
            out.append(TorrentResult(
                title=it.get("title"), info_hash=ih,
                magnet="magnet:?xt=urn:btih:%s&dn=%s" % (ih, urllib.parse.quote(it.get("title") or "torrent")),
                size=size, seeders=seeders, leechers=leechers, files=None,
                source=self.name, category=it.get("category") or None,
                published_at=it.get("published_at"), quote="rss_page"))
        return out, {"ok": ok, "http": code, "latency": lat, "error": None if ok else "nyaa 不可达"}

    def test(self):
        ok, lat, code, rows = self._rows("ubuntu")
        return {"status": ok, "latency": lat, "http_status": code,
                "result_count": len(rows), "note": "nyaa.si RSS（含中文字幕内容）"}

