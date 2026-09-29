from .sources.base import TorrentResult, SearchFilters, BaseSource  # noqa: F401
from .parser import parse_query                                      # noqa: F401
from .dedup import dedup_merge                                       # noqa: F401
from .ranking import apply_ranking, precision_filter                 # noqa: F401
import pkgutil, importlib


import os as _os

def load_sources():
    """自动注册 sources/ 下的所有插件（文件 -> BaseSource 子类）。核心零修改。"""
    src_path = _os.path.join(_os.path.dirname(__file__), "sources")
    out = []
    for m in pkgutil.iter_modules([src_path]):
        mod = importlib.import_module(".sources." + m.name, __name__)
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if isinstance(obj, type) and issubclass(obj, BaseSource) and obj is not BaseSource:
                out.append(obj())
    return out
