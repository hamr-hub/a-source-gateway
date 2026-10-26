"""两个平面的传输失败类型词表必须一致。

背景: 调用方是按 ``errors[].type`` 分支的("这是连不上, 还是连上了但中途断了")。
早先 Go 网关报 httpx 的异常类名(ConnectError / ReadError / UpstreamError),
Python 参照面报自己的异常类名(RemoteProtocolError / ConnectError …),
同一个失败在两个平面上给出不同的 type —— 调用方只能写两套判断, 或者干脆
在分支里漏掉一半。现在两个平面共用一套词表。

词表(见 Go 侧 gateway/internal/server/resolver.go 的 upstreamErrorType 与
Python 侧 python/src/a_source_gateway/resolver.py 的 _transport_error_type):

    connect_error —— 域名解析失败 / 连接被拒 / 证书问题(根本没连上)
    read_error    —— 连上了但中途断了(连接重置 / 服务端提前关闭 / EOF)
    upstream_error —— 其余传输层失败
"""
from __future__ import annotations

import httpx
import pytest

from a_source_gateway.resolver import _transport_error_type

VOCABULARY = {"connect_error", "read_error", "upstream_error"}


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("connection refused"),
    httpx.ConnectError("[Errno -2] Name or service not known"),
])
def test_connect_failures_use_connect_error(exc):
    assert _transport_error_type(exc) == "connect_error"


@pytest.mark.parametrize("exc", [
    httpx.ReadError("connection reset by peer"),
    httpx.RemoteProtocolError("Server disconnected without sending a response."),
])
def test_mid_stream_failures_use_read_error(exc):
    assert _transport_error_type(exc) == "read_error"


@pytest.mark.parametrize("exc", [
    ValueError("boom"),
    httpx.UnsupportedProtocol("bad scheme"),
])
def test_other_failures_use_upstream_error(exc):
    assert _transport_error_type(exc) == "upstream_error"


def test_every_mapped_name_is_in_the_documented_vocabulary():
    """词表外的名字会漏掉调用方的分支 —— 这里守住它不外扩。"""
    for exc in (httpx.ConnectError("x"), httpx.ReadError("x"), ValueError("x")):
        assert _transport_error_type(exc) in VOCABULARY
