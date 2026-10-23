#!/usr/bin/env python3
"""容器健康检查:探测本机网关的 /livez。

退出码 0 = 存活,1 = 不存活。

失败时只往 stderr 打一行摘要,不抛栈 —— 健康检查每 30s 跑一次,
抛栈会把 docker inspect 的健康日志刷满噪音。

环境变量:
    A_GATEWAY_HEALTHCHECK_URL      默认 http://127.0.0.1:8848/livez
    A_GATEWAY_HEALTHCHECK_TIMEOUT  单次探测超时秒数,默认 2
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("A_GATEWAY_HEALTHCHECK_URL", "http://127.0.0.1:8848/livez")
try:
    TIMEOUT = float(os.environ.get("A_GATEWAY_HEALTHCHECK_TIMEOUT", "2"))
except ValueError:
    TIMEOUT = 2.0


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=TIMEOUT) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        print(f"healthcheck: {URL} -> HTTP {exc.code}", file=sys.stderr)
        return 1
    except Exception as exc:  # 连接被拒 / 超时 / DNS 失败
        print(f"healthcheck: {URL} -> {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if code != 200:
        print(f"healthcheck: {URL} -> HTTP {code}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
