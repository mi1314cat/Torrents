"""yts.lt — YTS（电影为主）官方 JSON v2 API，带 seeds/peers/quality/hash。电影专用召回十分优质。"""
from .base import BaseSource, TorrentResult, SearchFilters, _http_get
import json, urllib.parse

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.6"


class YtsSource(BaseSource):
    name = "yts"
    priority = 35
    supports_unicode = False

    def _rows(self, query, timeout=10):
        url = "https://yts.lt/api/v2/list_movies.json?query_term=%s&limit=50&sort_by=seeds" % urllib.parse.quote(query)
        ok, lat, code, text = _http_get(url, timeout=timeout, ua=UA)
        if not ok:
            return ok, lat, code, []
        try:
            d = json.loads(text)
        except Exception:
            return ok, lat, code, []
        if d.get("status") != "ok":
            return ok, lat, code, []
        movies = (d.get("data") or {}).get("movies") or []
        rows = []
        for m in movies_fix(movies):   # 行形状统一化
            for t in m.get("torrents") or []:
                rows.append({"title": m.get("title_long") or m.get("title"),
                             "hash": t.get("hash"), "size": t.get("size_bytes") if isinstance(t.get("size_bytes"), int) else None,
                             "seeds": t.get("seeds"), "peers": t.get("peers"),
                             "quality": "%s%s" % (t.get("quality") or "", t.get("video_codec") or ""),
                             "date_uploaded": t.get("date_uploaded"),
                             "category": "movies"})
        return ok, lat, code, rows

    def search(self, query, filters: SearchFilters):
        cfg = getattr(self, '_runtime_cfg', {'timeout': 10})
        ok, lat, code, rows = self._rows(query, cfg.get("timeout", 10))
        out = []
        for it in rows[: filters.max_per_source]:
            ih = (it.get("hash") or "").strip().lower()
            if len(ih) != 40:
                continue
            title = "%s [%s]" % (it.get("title") or "movie", it["quality"]) if it.get("quality") else (it.get("title") or "movie")
            out.append(TorrentResult(
                title=title, info_hash=ih,
                magnet="magnet:?xt=urn:btih:%s&dn=%s" % (ih, urllib.parse.quote(title)),
                size=int(it["size"]) if isinstance(it.get("size"), int) else None,
                seeders=it.get("seeds") if it.get("seeds") is not None else None,
                leechers=it.get("peers") if it.get("peers") is not None else None,
                files=None, source=self.name, category="Movies",
                published_at=it.get("date_uploaded"), quote="api_field"))
        return out, {"ok": ok, "http": code, "latency": lat, "error": None if ok else "yts 不可达"}

    def test(self):
        ok, lat, code, rows = self._rows("inception")
        return {"status": ok, "latency": lat, "http_status": code, "result_count": len(rows),
                "note": "yts.lt 官方 v2 API（电影/影视）。不覆盖非电影类检索。"}


def movies_fix(movies):
    return movies if isinstance(movies, list) else []
