"""Recall→Rank 分离：Ranking 纯 Python 可配置权重。
- Relevance: parser.title_relevance 预填
- Seeders: log 缩放（max_api_seeders 优先，L2 实测值若存在则取实时值覆盖）
- Freshness: published_at 距今衰减（90 天线性）
- SourceQuality: 来自 sources表的 0~1 强度指标（成功率*延迟*管理员 priority）
- SizeMatch / HealthEvidence: 调用方按过滤条件或实际 L1 结果填充 _size_match/_health
不允许通过 AI 或外部库。"""
import math, time, re, datetime
from email.utils import parsedate_to_datetime


def apply_ranking(rows, settings, now=None):
    now = now or time.time()
    for t in rows:
        rel = t.get("_rel", 0.0)
        seeds = max(t.get("max_api_seeders") or 0, (t.get("_health_data") or {}).get("max_seeders") or 0)
        # gl(fruit) log scaling: 10000 seeds 不会压满（公式 lu(log1p(se)/ log1p(500))）
        seed_score = min(1.0, math.log1p(seeds) / math.log1p(150.0)) if seeds > 0 else 0.0
        fresh = _freshness(t.get("published_at"), now)
        sq = 0.5 + 0.5 * float(t.get("_source_quality_hint", 0.5))
        t["_seed_norm"] = seed_score
        t["_fresh_norm"] = fresh
        t["_rel_norm"] = rel
        b = {
            "relevance": w(t, "w_relevance", 0.40) * rel,
            "seeders": w(t, "w_seed", 0.20) * seed_score,
            "freshness": w(t, "w_fresh", 0.15) * fresh,
            "source_quality": w(t, "w_source_quality", 0.10) * float(t.get("_source_quality", 0.5)),
            "size": w(t, "w_size", 0.10) * float(t.get("_size_match", 0.5)),
            "health_evidence": w(t, "w_health", 0.05) * float(t.get("_health", 0.0)),
        }
        t["score_breakdown"] = {k: round(v, 4) for k, v in b.items()}
        t["_score"] = sum(b.values())
    rows.sort(key=lambda x: x["_score"], reverse=True)
    return rows


def w(t, key, default):
    s = t.get("_weights") or {}
    return float(s.get(key, default))


def _freshness(published, now=None):
    age = _age_seconds(published, now)
    if age is None:
        return 0.0
    if age <= 0:
        return 1.0
    if age < 90 * 86400:
        return 1.0 - (age / (90 * 86400))
    return max(0.0, 1.0 - (age - 90 * 86400) / (365 * 86400 * 0.5))

def _age_seconds(published, now):
    if published is None:
        return None
    s = str(published)
    if re.fullmatch(r"\d+", s):
        try:
            return max(0, (now or time.time()) - int(s))
        except Exception:
            return None
    try:
        dt = parsedate_to_datetime(s)
        return max(0, (now or time.time()) - dt.timestamp())
    except Exception:
        pass
    for pat in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(s.strip(), pat).replace(tzinfo=datetime.timezone.utc)
            return max(0, (now or time.time()) - dt.timestamp())
        except Exception:
            continue
    return None


def precision_filter(parsed, rows, mode="balanced"):
    """Recall→Rank 分离：strict/balanced 过滤，broad 全保留交由 Ranking。"""
    b = (parsed.get("base") or "").strip().lower()
    tokens = [t for t in (parsed.get("tokens") or []) if t]
    if mode == "broad" or not b:
        return rows
    def keep(t):
        title = (t.get("title") or "").lower()
        if not title:
            return False
        if b in title:
            return True
        hit = sum(1 for tok in tokens if tok and tok in title)
        if mode == "strict":
            return b in title
        return hit >= max(1, len(tokens) - 1)
    return [t for t in rows if keep(t)]
