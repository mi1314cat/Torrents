"""SolidTorrents（bitsearch.eu JSON API）。真中文索引、无需登录、无验证码。
需要浏览器 UA；bitsearch 限速严格，失败延迟 1.5s 重试一次。"""
from .base import BaseSource, TorrentResult, SearchFilters, _http_get
import json, urllib.parse, time

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.6"


class SolidTorrentsSource(BaseSource):
    name = "solidtorrents"

    def _raw(self, query, timeout=10):
        url = ("https://bitsearch.eu/api/v1/search?q=%s&limit=%d"
               % (urllib.parse.quote(query), min(20, max(1, 25))))
        for attempt in range(2):  # 一文未覆盖的秒级限流补偿
            ok, lat, code, text = _http_get(url, timeout=timeout, ua=UA)
            if ok:
                break
            if attempt == 0:
                time.sleep(1.5)
        rows = []
        if ok:
            try:
                data = json.loads(text)
                rows = data.get("results", [])
            except Exception as e:
                return False, lat, 0, []
        return ok, lat, code, rows

    def search(self, query, filters: SearchFilters):
        cfg = getattr(self, '_runtime_cfg', {'timeout': 10})
        ok, lat, code, rows = self._raw(query, cfg.get("timeout", 10))
        out = []
        for r in rows[: filters.max_per_source]:
            ih = (r.get("infohash") or "").lower()
            if len(ih) != 40:
                continue
            def _int(k):
                try:
                    return int(r[k])
                except Exception:
                    return None
            out.append(TorrentResult(
                title=r.get("title") or None, info_hash=ih,
                magnet="magnet:?xt=urn:btih:%s&dn=%s" % (ih, urllib.parse.quote(r.get("title") or "torrent")),
                size=_int("size"), seeders=_int("seeders"), leechers=_int("leechers"),
                files=None, source=self.name,
                category=str(r.get("category") or "") or None,
                published_at=r.get("updatedAt") or None,
                quote="api_field"))
        return out, {"ok": ok, "http": code, "latency": lat,
                     "error": None if ok else "bitsearch.eu 不可达/限流"}

    def test(self):
        ok, lat, code, rows = self._raw("ubuntu")
        return {"status": ok, "latency": lat, "http_status": code,
                "result_count": len(rows), "note": "bitsearch.eu JSON API（中文覆盖强）"}

