"""
cache.py - 有界小缓存

原则: 只缓存"被查过的那几条", 不做全量落盘。
  - 条数上限 + 字节上限双保险, 内存占用可预测
  - TTL 按端点配置(静态表 24h, 列表类 15-30min)
  - 过期条目不删除, 但在超时降级时可作为 stale 数据兜底返回
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


@dataclass
class Entry:
    value: Any
    stored_at: float
    expires_at: float
    size: int
    meta: dict[str, Any]
    hits: int = 0              # 命中次数(用于自适应 TTL: 热条目短 TTL)
    last_hit: float = 0.0


class BoundedCache:
    def __init__(self, max_entries: int = 512, max_bytes: int = 32 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, Entry] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0
        self.stale_hits = 0

    @staticmethod
    def _size(value: Any) -> int:
        try:
            import json
            return len(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return len(str(value))

    def get(self, key: str) -> tuple[Any | None, bool, int]:
        """返回 (值, 是否新鲜, 缓存年龄秒)。值可能是过期的(stale)。"""
        e = self._items.get(key)
        if e is None:
            self.misses += 1
            return None, False, 0
        self._items.move_to_end(key)
        e.hits += 1
        e.last_hit = time.time()
        age = int(time.time() - e.stored_at)
        if time.time() < e.expires_at:
            self.hits += 1
            return e.value, True, age
        self.stale_hits += 1
        return e.value, False, age

    def put(self, key: str, value: Any, ttl_s: int, meta: dict[str, Any] | None = None) -> None:
        size = self._size(value)
        if size > self.max_bytes:          # 单条就超限, 不缓存
            return
        old = self._items.pop(key, None)
        if old:
            self._bytes -= old.size
        now = time.time()
        self._items[key] = Entry(value, now, now + ttl_s, size, meta or {})
        self._bytes += size
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries or self._bytes > self.max_bytes:
            _, ev = self._items.popitem(last=False)
            self._bytes -= ev.size

    def hot_keys(self, min_hits: int = 3) -> list[str]:
        """被反复访问的条目(用于缩短 TTL, 让热数据更新更及时)。"""
        return [k for k, e in self._items.items() if e.hits >= min_hits]

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "hot_entries": sum(1 for e in self._items.values() if e.hits >= 3),
            "entries": len(self._items),
            "bytes": self._bytes,
            "max_entries": self.max_entries,
            "max_bytes": self.max_bytes,
            "hits": self.hits,
            "misses": self.misses,
            "stale_hits": self.stale_hits,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
        }

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0
