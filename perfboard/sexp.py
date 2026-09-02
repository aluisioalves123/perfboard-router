"""Parser generico de S-expressions no dialeto do KiCad."""
from __future__ import annotations


class SexpError(ValueError):
    pass


def parse(text: str):
    """Retorna a lista aninhada correspondente ao primeiro no do texto."""
    nodes, pos = _parse_list(text, _skip_ws(text, 0))
    return nodes


def _skip_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i += 1
        elif c == "#":  # comentario de linha (raro, mas inofensivo)
            while i < n and s[i] != "\n":
                i += 1
        else:
            break
    return i


def _parse_list(s: str, i: int):
    if i >= len(s) or s[i] != "(":
        raise SexpError("esperava '(' na posicao %d" % i)
    i += 1
    out = []
    n = len(s)
    while True:
        i = _skip_ws(s, i)
        if i >= n:
            raise SexpError("fim inesperado do arquivo")
        c = s[i]
        if c == ")":
            return out, i + 1
        if c == "(":
            sub, i = _parse_list(s, i)
            out.append(sub)
        elif c == '"':
            tok, i = _parse_string(s, i)
            out.append(tok)
        else:
            j = i
            while j < n and s[j] not in ' \t\r\n()"':
                j += 1
            out.append(s[i:j])
            i = j


def _parse_string(s: str, i: int):
    i += 1  # pula a aspa inicial
    buf = []
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            i += 2
            continue
        if c == '"':
            return "".join(buf), i + 1
        buf.append(c)
        i += 1
    raise SexpError("string nao terminada")


def head(node) -> str:
    """Nome do no (primeiro elemento), ou '' se nao for lista."""
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return ""


def children(node, name: str):
    """Sub-nos com o nome dado."""
    if not isinstance(node, list):
        return []
    return [c for c in node[1:] if head(c) == name]


def child(node, name: str):
    cs = children(node, name)
    return cs[0] if cs else None


def value(node, name: str, default=None):
    """Valor escalar de (name "valor")."""
    c = child(node, name)
    if c is None or len(c) < 2:
        return default
    return c[1] if isinstance(c[1], str) else default
