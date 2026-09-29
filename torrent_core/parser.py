"""轻量 Query 解析 + 相关度计算（纯 Python，无 AI）。只提取明确元信息，不改用户意图。"""
import re

_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
_SEASON_EP = re.compile(r"\bS(\d{1,2})E(\d{1,3})\b", re.I)
_SEASON = re.compile(r"\bS(\d{1,2})\b", re.I)
_EP = re.compile(r"\bE(\d{1,3})\b")
_QUALITY = re.compile(r"\b(2160p|1080p|720p|480p|4K|8K|WEBRip|WEB-?DL|BluRay|BDRip|HDTV|x264|x265|HEVC)\b", re.I)


def parse_query(raw: str) -> dict:
    raw = (raw or "").strip()
    meta = {"raw": raw, "base": raw, "tokens": [], "year": None,
            "season": None, "episode": None, "quality": None,
            "lang_hint": None, "has_meta": False}
    if not raw:
        return meta

    parts = raw
    m = _YEAR.search(parts)
    if m:
        meta["year"] = int(m.group(1))
        parts = parts.replace(m.group(1), " ")

    m = _SEASON_EP.search(parts)
    if m:
        meta["season"], meta["episode"] = int(m.group(1)), int(m.group(2))
        parts = parts.replace(m.group(0), " ")
    else:
        m = _SEASON.search(parts)
        if m:
            meta["season"] = int(m.group(1))
            parts = parts.replace(m.group(0), " ")
        m = _EP.search(parts)
        if m:
            meta["episode"] = int(m.group(1))
            parts = parts.replace(m.group(0), " ")

    m = _QUALITY.search(parts)
    if m:
        meta["quality"] = m.group(1).lower()
        parts = parts.replace(m.group(1), " ")

    for lang, pat in [("zh", r"中文|简体|繁体|chinese|cantonese|mandarin"),
                      ("ja", r"\bjp\b|japanese|日本語|日语"),
                      ("en", r"\beng(lish)\b")]:
        if re.search(pat, parts, re.I):
            meta["lang_hint"] = lang
            break

    base = " ".join(parts.split())
    if base:
        meta["base"] = base
        tokens = [t.lower().strip(".,()[]'\"") for t in base.split()]
        meta["tokens"] = [t for t in tokens if t]
    if not meta["tokens"]:
        meta["base"] = raw
        meta["tokens"] = [w.lower() for w in raw.split()]
    meta["has_meta"] = any([meta["year"], meta["season"], meta["episode"],
                            meta["quality"], meta["lang_hint"]])
    return meta


def title_relevance(parsed: dict, title: str) -> float:
    """0~1 相关度：全部 token 命中率 + base 子串加分 + 元数据匹配加分。"""
    if not title:
        return 0.0
    tl = title.lower()
    base = (parsed.get("base") or "").lower()
    tokens = parsed.get("tokens") or ([base] if base else [])
    tokens = [t for t in tokens if t]
    if not tokens:
        return 0.5
    hit = sum(1 for t in tokens if t in tl)
    score = 0.35 * (hit / len(tokens))
    b = base.lower()
    if b and b in tl:
        score += 0.40
    if parsed.get("year") and str(parsed["year"]) in tl:
        score += 0.12
    if parsed["quality"] and parsed["quality"] in tl:
        score += 0.08
    if parsed.get("lang_hint") and lang_match(parsed["lang_hint"], tl):
        score += 0.08
    if parsed.get("season") and _season_in(title, parsed["season"]):
        score += 0.06
    return min(score, 1.0)


def _season_in(title, season):
    return bool(re.search(r"\bS0*%d\b|第%s季|Season\s*%s" % (season, season, season),
                          title, re.I))
