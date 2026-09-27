"""URI 组件检查的**私有**实现。

供 `storage.validate_object_uri` 与 `collector` 的来源 URI / network origin 校验复用。

不是契约：本模块的名字不进入任何公共 `__all__`、注册表或 Schema，可随实现调整；公共行为只由
`validate_object_uri`、`validate_source_uri` 与相关 DTO 的校验结果定义。

先按 RFC 3986 附录 B 的参考正则把 URI 拆成 scheme / authority / path / query / fragment 五个组件，
拆分本身不做校验；全部规则写在各检查函数里。`core/` 的依赖白名单不含 `urllib`（架构边界测试），且
`urlsplit` 对端口等也是惰性宽松的，因此不用它。
"""

from __future__ import annotations

import re
from typing import NamedTuple

__all__: list[str] = []

_RFC3986_SPLIT = re.compile(r"^(?:([^:/?#]+):)?(?://([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?$")
_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*")
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PORT_RE = re.compile(r"[1-9][0-9]{0,4}")
_HEX = frozenset("0123456789abcdefABCDEF")
#: 解码后不得出现的字节：路径分隔符、反斜杠、NUL 与其它 C0 控制字符、DEL。
_FORBIDDEN_DECODED = frozenset({0x2F, 0x5C, 0x7F, *range(0x20)})


class UriParts(NamedTuple):
    """RFC 3986 的五个组件；`None` 表示该组件不存在（与存在但为空不同）。"""

    scheme: str | None
    authority: str | None
    path: str
    query: str | None
    fragment: str | None


def split(value: str) -> UriParts:
    match = _RFC3986_SPLIT.fullmatch(value)
    if match is None:  # pragma: no cover - 附录 B 的正则匹配任意字符串
        raise ValueError(f"无法拆分 URI：{value!r}")
    scheme, authority, path, query, fragment = match.groups()
    return UriParts(scheme, authority, path, query, fragment)


def is_visible_ascii(value: str) -> bool:
    """只含 ASCII 可见字符：无空白、无控制字符、无反斜杠。"""
    return value.isascii() and value.isprintable() and " " not in value and "\\" not in value


def is_scheme(value: str | None) -> bool:
    return value is not None and _SCHEME_RE.fullmatch(value) is not None


def is_host_port(authority: str) -> bool:
    """非空的小写 DNS 主机名 + 可选端口（1 ~ 65535，无前导零）；不接受凭据。"""
    host, colon, port = authority.partition(":")
    if colon and (_PORT_RE.fullmatch(port) is None or int(port) > 65535):
        return False
    if not host or len(host) > 253:
        return False
    return all(_HOST_LABEL_RE.fullmatch(label) is not None for label in host.split("."))


def _decoded_segment(segment: str) -> str | None:
    """解码一个路径段的 percent escape；畸形 escape 或解码出禁止字节时返回 `None`。

    解码结果只用于判断 `.` / `..`；非 ASCII 字节（例如 UTF-8 百分号编码）保留为占位字符。
    """
    out: list[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char != "%":
            out.append(char)
            index += 1
            continue
        escape = segment[index + 1 : index + 3]
        if len(escape) != 2 or not set(escape) <= _HEX:
            return None
        byte = int(escape, 16)
        if byte in _FORBIDDEN_DECODED:
            return None
        out.append(chr(byte) if byte < 0x80 else "�")
        index += 3
    return "".join(out)


def is_object_path(path: str) -> bool:
    """非空的绝对对象路径：以 `/` 开头；每段非空（无 `//`、不以 `/` 结尾）；每个 `%` 都是合法的
    两位十六进制 escape，且不解码为 `/`、`\\`、控制字符或 DEL；解码后不是 `.` / `..` 段。"""
    if not path.startswith("/"):
        return False
    for segment in path[1:].split("/"):
        decoded = _decoded_segment(segment)
        if not segment or decoded is None or decoded in {".", ".."}:
            return False
    return True


def is_https_path(path: str) -> bool:
    """https 来源路径：空、单独 `/`，或满足 `is_object_path` 的绝对路径。"""
    return path in {"", "/"} or is_object_path(path)
