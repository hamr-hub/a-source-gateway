"""
compat.py - 跨 Python 版本的小兼容层

项目声明支持 Python 3.10+, 但 ``asyncio.timeout`` 是 3.11 才有的。
这里提供一个行为一致的回退实现(基于 call_later + 取消当前任务)。
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

if sys.version_info >= (3, 11):  # pragma: no cover - 取决于运行版本
    from asyncio import timeout as timeout_ctx  # noqa: F401
else:  # pragma: no cover
    @asynccontextmanager
    async def timeout_ctx(seconds: float):
        """3.10 上的 asyncio.timeout 回退实现。"""
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        handle = loop.call_later(seconds, task.cancel)   # type: ignore[union-attr]
        try:
            yield
        except asyncio.CancelledError as exc:
            raise TimeoutError from exc
        finally:
            handle.cancel()


__all__ = ["timeout_ctx"]
