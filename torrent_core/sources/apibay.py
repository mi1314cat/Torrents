"""apibay（TPB 官方前端 API）。仅可靠匹配 ASCII 关键词；非 ASCII 查询由核心层跳过。
缺失字段一律 None，禁止伪造。"""
from .base import BaseSource, TorrentResult, SearchFilters, _http_get
import json, urllib.parse

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.6"


class ApibaySource(BaseSource):
    name = "apibay"
    supports_unicode = False

    def _rows(self, query, timeout=10):
        url = "https://apibay.org/q.php?q=%s&cat=" % urllib.parse.quote(query)
        ok, lat, code, text = _http_get(url, timeout=timeout, ua=UA)
        rows = []
        if ok:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    rows = parsed
                else:
                    ok = False
            except Exception:
                pass
        return ok, lat, code, rows

    def search(self, query, filters: SearchFilters):
        cfg = getattr(self, '_runtime_cfg', {'timeout': 10})
        ok, lat, code, rows = self._rows(query, cfg.get("timeout", 10))
        out = []
        for r in rows[: filters.max_per_source]:
            try:
                ih = (r.get("info_hash") or "").lower()
                if len(ih) != 40:
                    continue
                def _int(k): return int(r[k]) if str(r.get(k) or "").lstrip("-").isdigit() else None
                out.append(TorrentResult(
                    title=r.get("name") or None, info_hash=ih,
                    magnet="magnet:?xt=urn:btih:%s&dn=%s" % (ih, urllib.parse.quote(r.get("name") or "torrent")),
                    size=_int("size"), seeders=_int("seeders"), leechers=_int("leechers"),
                    files=_int("num_files"), source=self.name,
                    category=str(r.get("category") or "") or None,
                    published_at=str(r.get("added") or "") or None,
                    quote="api_field"))
            except Exception:
                continue
        return out, {"ok": ok, "http": code, "latency": lat, "error": None if ok else "apibay 不可达"}

    def test(self):
        ok, lat, code, rows = self._rows("ubuntu")
        return {"status": ok, "latency": lat, "http_status": code,
                "result_count": len(rows), "note": "TPB 官方前端 API（仅 ASCII）"}

