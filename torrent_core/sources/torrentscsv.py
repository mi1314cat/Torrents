"""torrents-csv.com — 社区维护的开源种子库（自带 seeders/leechers/发布时间，Reddit/spn 版本）。
JSON API：GET /service/search?q=... → {"torrents":[{infohash,name,size_bytes,created_unix,seeders,leechers}...]}
"""
from .base import BaseSource, TorrentResult, SearchFilters, _http_get
import json, urllib.parse, time as _t

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.6"


class TorrentsCsvSource(BaseSource):
    name = "torrentscsv"
    priority = 40

    def _rows(self, query, timeout=10):
        url = "https://torrents-csv.com/service/search?q=%s" % urllib_parse_quote(query)
        ok, lat, code, text = _http_get(url, timeout=timeout, ua=UA)
        if not ok:
            return ok, lat, code, []
        try:
            d = json.loads(text)
        except Exception:
            return ok, lat, code, []
        return ok, lat, code, d.get("torrents") or []

    def search(self, query, filters: SearchFilters):
        cfg = getattr(self, '_runtime_cfg', {'timeout': 10})
        ok, lat, code, rows = self._rows(query, cfg.get("timeout", 10))
        out = []
        for it in rows[: filters.max_per_source]:
            ih = (it.get("infohash") or "").strip().lower()
            if len(ih) != 40:
                continue
            created = it.get("created_unix")
            published = _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime(created)) if isinstance(created, (int, float)) else None
            out.append(TorrentResult(
                title=it.get("name"), info_hash=ih,
                magnet="magnet:?xt=urn:btih:%s&dn=%s" % (ih, urllib_parse_quote(it.get("name") or "torrent")),
                size=it.get("size_bytes"),
                seeders=it.get("seeders") if it.get("seeders") is not None else None,
                leechers=it.get("leechers") if it.get("leechers") is not None else None,
                files=None, source=self.name, category=None,
                published_at=published, quote="api_field"))
        return out, {"ok": ok, "http": code, "latency": lat, "error": None if ok else "torrents-csv 不可达"}

    def test(self):
        ok, lat, code, rows = self._rows("ubuntu")
        return {"status": ok, "latency": lat, "http_status": code, "result_count": len(rows),
                "note": "torrents-csv.com service/search（开源社区聚合索引）"}


def urllib_parse_quote(s):
    import urllib.parse
    return urllib.parse.quote(s or "")
