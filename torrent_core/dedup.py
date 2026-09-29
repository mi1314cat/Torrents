"""InfoHash 去重（严格 info_hash 键，不按 title）。多源 seed 数据并存，绝不相加。"""
from dataclasses import asdict


def dedup_merge(results):
    seen = {}
    for r in results:
        ih = (r.info_hash or "").lower()
        if len(ih) != 40:
            continue
        u = seen.setdefault(ih, {
            "title": r.title,
            "info_hash": ih,
            "magnet": "magnet:?xt=urn:btih:%s&dn=%s" % (ih, _q(r.title or "torrent")),
            "size": r.size,
            "files": r.files,
            "category": r.category,
            "published_at": r.published_at,
            "sources": [],
            "max_api_seeders": 0,
        })
        u["sources"].append({
            "name": r.source, "seeders": r.seeders, "leechers": r.leechers,
        })
        u["max_api_seeders"] = max(u["max_api_seeders"], r.seeders or 0)
        if r.size is not None and u["size"] is None:
            u["size"] = r.size
        if r.files is not None and u["files"] is None:
            u["files"] = r.files
        if not u["title"] and r.title:
            u["title"] = r.title
    return list(seen.values())


def _q(s):
    import urllib.parse
    return urllib.parse.quote(s)
