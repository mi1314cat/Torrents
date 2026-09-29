"""therarbg.to — RARBG 后继社区（影视质量高）。JSON API 带种子 hash/seeders/leechers/发布时间/IMDb。
GET https://therarbg.to/get-posts/posting:all/?format=json&q=<query>&limit=..."""
from .base import BaseSource, TorrentResult, SearchFilters, _http_get
import json, urllib.parse

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.6"


class TheRarBgSource(BaseSource):
    name = "therarbg"
    priority = 30

    def _rows(self, query, timeout=10):
        url = "https://therarbg.to/get-posts/posting:all/?format=json&q=%s&limit=100" % urllib.parse.quote(query)
        ok, lat, code, text = _http_get(url, timeout=timeout, ua=UA)
        if not ok:
            return ok, lat, code, []
        try:
            d = json.loads(text)
        except Exception:
            return ok, lat, code, []
        return ok, lat, code, d.get("results") or []

    def search(self, query, filters: SearchFilters):
        cfg = getattr(self, '_runtime_cfg', {'timeout': 10})
        ok, lat, code, rows = self._rows(query, cfg.get("timeout", 10))
        out = []
        for it in rows[: filters.max_per_source]:
            ih = (it.get("h") or "").strip().lower()
            if len(ih) != 40 or not it.get("n"):
                continue
            title = it["n"]
            cat = it.get("c") or None
            ep = it.get("a")
            published = _strftime(ep) if ep else None
            out.append(TorrentResult(
                title=title, info_hash=ih,
                magnet="magnet:?xt=urn:btih:%s&dn=%s" % (ih, urllib.parse.quote(title)),
                size=it.get("s"),
                seeders=it.get("se") if it.get("se") is not None else None,
                leechers=it.get("le") if it.get("le") is not None else None,
                files=None, source=self.name, category=cat,
                published_at=published,
                quote="api_field",
                ))
        return out, {"ok": ok, "http": code, "latency": lat, "error": None if ok else "therarbg 不可达"}

    def test(self):
        ok, lat, code, rows = self._rows("ubuntu")
        return {"status": ok, "latency": lat, "http_status": code, "result_count": len(rows),
                "note": "therarbg.to JSON API（RARBG 继任社区）"}


def _strftime(ep):
    import time
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(ep)))
    except Exception:
        return None
