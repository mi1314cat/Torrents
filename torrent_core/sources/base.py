"""统一数据结构与 Source 插件抽象。缺失字段一律 None，禁止伪造。"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TorrentResult:
    title: Optional[str]
    info_hash: str
    magnet: str
    size: Optional[int]
    seeders: Optional[int]
    leechers: Optional[int]
    files: Optional[int]
    source: str
    category: Optional[str]
    published_at: Optional[str] = None
    quote: Optional[str] = None          # "api_field" | "tr_page"
    relevance: float = field(default=0.0)


@dataclass
class SearchFilters:
    precision: str = "balanced"          # strict / balanced / broad
    limit: int = 50
    max_per_source: int = 50
    sort: str = "relevance"


class BaseSource:
    name: str
    _runtime_cfg = {"enabled": True, "priority": 50, "timeout": 10}
    enabled: bool = True
    priority: int = 50
    # 子类覆盖：是否依赖非 ASCII（TPB 不支持中文等非 ASCII 匹配）
    supports_unicode = True

    def search(self, query, filters: SearchFilters) -> list[TorrentResult]:
        raise NotImplementedError

    def test(self) -> dict:
        raise NotImplementedError


def _http_get(url, timeout=10, ua=None, encoding=None):
    """通用 fetch，供插件复用（与 Phase 3 已验证实现一致）。"""
    import urllib.request, urllib.error, gzip, time as _t, socket
    req = urllib.request.Request(url, headers={
        "User-Agent": ua or "torrent-tool/2.0",
        "Accept-Encoding": "gzip",
    })
    t0 = _t.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            text = data.decode(encoding or "utf-8", errors="replace")
            return True, round(_t.time() - t0, 2), r.status, text
    except Exception as e:
        return False, round(_t.time() - t0, 2), getattr(e, "code", 0), str(e)
