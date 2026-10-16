"""Experiment filters: a small expression language plus saved, named filters.

A filter selects experiments. The same expression string works everywhere:
saved under a name by a human in the dashboard, or passed ad hoc by anyone
(``where=`` on the read endpoints).

Expressions
-----------
    meta.MAX_WALL_SECONDS >= 9000
    tag = smoke or tag = bench
    idea under 1169 and status = completed and not meta.SEED in [5, 6]
    metric.score > 20 and description ~ "sampling"
    @long and label != 1050.1

Fields
    meta.<KEY>          experiment settings (exact key; falls back to a
                        case-insensitive match; dots reach into nested dicts)
    metric.<KEY>        final metrics (``metrics.`` also accepted; dotted
                        paths reach nested metrics)
    tag                 the experiment's tags (``tag = x`` means "has tag x")
    idea                idea id; ``idea under N`` = idea N or any descendant
    label, id, status, description, attempt, runtime,
    created_at, started_at, finished_at
Operators
    = (or ==), !=, >, >=, <, <=, ~ (contains, case-insensitive), !~,
    in [a, b, ...], not in [...], exists, missing
Combinators
    and, or, not (also &&, ||, !), parentheses; @name refers to a saved filter.

Comparisons are numeric when both sides are numbers, else string
comparisons. A field the experiment does not have makes every comparison
false (use ``missing`` / ``exists`` to test for it); ``tag != x`` is true
when the experiment lacks tag x.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .jsonio import write_json

MAX_EXPR_LEN = 4000
MAX_REF_DEPTH = 8


class FilterError(ValueError):
    """A filter expression that does not parse, or refers to an unknown filter."""

    def __init__(self, message: str, pos: int | None = None):
        super().__init__(message if pos is None else f"{message} (at position {pos})")
        self.pos = pos


# ── Tokenizer ────────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<op>==|!=|>=|<=|!~|&&|\|\||[=<>~!()\[\],])
  | (?P<ref>@[A-Za-z0-9_.:-]+)
  | (?P<word>[A-Za-z0-9_.:/+-][A-Za-z0-9_.:/+*-]*)
""", re.VERBOSE)

_KEYWORDS = {"and", "or", "not", "in", "under", "exists", "missing"}


@dataclass
class _Tok:
    kind: str   # str | op | ref | word | kw | end
    value: str
    pos: int


def _tokenize(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    i = 0
    while i < len(text):
        m = _TOKEN_RE.match(text, i)
        if not m:
            raise FilterError(f"unexpected character {text[i]!r}", i)
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "str":
            val = re.sub(r"\\(.)", r"\1", val[1:-1])
        elif kind == "word" and val.lower() in _KEYWORDS:
            kind, val = "kw", val.lower()
        elif kind == "op":
            val = {"&&": "and", "||": "or", "!": "not", "==": "="}.get(val, val)
            if val in ("and", "or", "not"):
                kind = "kw"
        if kind != "ws":
            out.append(_Tok(kind, val, m.start()))
        i = m.end()
    out.append(_Tok("end", "", len(text)))
    return out


# ── Parser → small AST of tuples ─────────────────────────────────────────────
#   ("and", a, b) ("or", a, b) ("not", a) ("cmp", field, op, value)
#   ("in", field, [values]) ("exists", field) ("under", idea_id) ("ref", name) ("all",)

_COMPARE_OPS = {"=", "!=", ">", ">=", "<", "<=", "~", "!~"}
_FIELDS = {"tag", "tags", "idea", "label", "id", "status", "description",
           "attempt", "runtime", "created_at", "started_at", "finished_at"}


class _Parser:
    def __init__(self, text: str):
        self.toks = _tokenize(text)
        self.i = 0

    def peek(self) -> _Tok:
        return self.toks[self.i]

    def take(self) -> _Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, kind: str, value: str | None = None) -> _Tok:
        t = self.take()
        if t.kind != kind or (value is not None and t.value != value):
            want = value or kind
            got = t.value or "end of expression"
            raise FilterError(f"expected {want!r}, got {got!r}", t.pos)
        return t

    def parse(self):
        if self.peek().kind == "end":
            return ("all",)
        node = self.or_()
        t = self.peek()
        if t.kind != "end":
            raise FilterError(f"unexpected {t.value!r}", t.pos)
        return node

    def or_(self):
        node = self.and_()
        while self.peek().kind == "kw" and self.peek().value == "or":
            self.take()
            node = ("or", node, self.and_())
        return node

    def and_(self):
        node = self.not_()
        while self.peek().kind == "kw" and self.peek().value == "and":
            self.take()
            node = ("and", node, self.not_())
        return node

    def not_(self):
        t = self.peek()
        if t.kind == "kw" and t.value == "not":
            self.take()
            return ("not", self.not_())
        return self.atom()

    def atom(self):
        t = self.take()
        if t.kind == "op" and t.value == "(":
            node = self.or_()
            self.expect("op", ")")
            return node
        if t.kind == "ref":
            return ("ref", t.value[1:])
        if t.kind != "word":
            raise FilterError(f"expected a field, '(' or @filter, got {t.value or 'end of expression'!r}", t.pos)
        field = _field(t)
        nxt = self.take()
        if nxt.kind == "kw" and nxt.value in ("exists", "missing"):
            node = ("exists", field)
            return node if nxt.value == "exists" else ("not", node)
        if nxt.kind == "kw" and nxt.value == "under":
            if field != ("idea",):
                raise FilterError("'under' only works with idea (idea under N)", nxt.pos)
            v = self.take()
            if v.kind != "word" or not v.value.isdigit():
                raise FilterError("idea under needs an idea id", v.pos)
            return ("under", int(v.value))
        negate = False
        if nxt.kind == "kw" and nxt.value == "not":
            after = self.take()
            if not (after.kind == "kw" and after.value == "in"):
                raise FilterError("expected 'in' after 'not'", after.pos)
            nxt, negate = after, True
        if nxt.kind == "kw" and nxt.value == "in":
            values = self.value_list()
            node = ("in", field, values)
            return ("not", node) if negate else node
        if nxt.kind == "op" and nxt.value in _COMPARE_OPS:
            return ("cmp", field, nxt.value, self.value())
        raise FilterError(f"expected an operator after {t.value!r}, got {nxt.value or 'end of expression'!r}", nxt.pos)

    def value(self):
        t = self.take()
        if t.kind in ("str", "word"):
            return _literal(t)
        raise FilterError(f"expected a value, got {t.value or 'end of expression'!r}", t.pos)

    def value_list(self) -> list:
        self.expect("op", "[")
        values = []
        if self.peek().kind == "op" and self.peek().value == "]":
            self.take()
            return values
        while True:
            values.append(self.value())
            t = self.take()
            if t.kind == "op" and t.value == "]":
                return values
            if not (t.kind == "op" and t.value == ","):
                raise FilterError("expected ',' or ']' in a list", t.pos)


def _field(t: _Tok) -> tuple:
    name = t.value
    low = name.lower()
    for prefix, kind in (("meta.", "meta"), ("metric.", "metric"), ("metrics.", "metric")):
        if low.startswith(prefix) and len(name) > len(prefix):
            return (kind, name[len(prefix):])
    if low in _FIELDS:
        return ("tag",) if low == "tags" else (low,)
    raise FilterError(
        f"unknown field {name!r} (use meta.<KEY>, metric.<KEY>, tag, idea, label, status, "
        f"description, attempt, runtime, created_at, started_at, finished_at)", t.pos)


def _literal(t: _Tok):
    if t.kind == "str":
        return t.value
    v = t.value
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    return v  # numbers stay strings until compared, so labels like 1050.1 keep their text


def parse(expr: str):
    """Parse *expr*; raises FilterError. An empty expression selects everything."""
    if len(expr or "") > MAX_EXPR_LEN:
        raise FilterError(f"expression longer than {MAX_EXPR_LEN} characters")
    return _Parser(expr or "").parse()


def refs(node) -> set[str]:
    """Names of the saved filters an expression refers to."""
    if node[0] == "ref":
        return {node[1]}
    if node[0] in ("and", "or"):
        return refs(node[1]) | refs(node[2])
    if node[0] == "not":
        return refs(node[1])
    return set()


# ── Evaluation ───────────────────────────────────────────────────────────────

_MISSING = object()


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _dig(obj, path: str):
    """Value at a dotted *path* in nested dicts; exact keys first, then a
    case-insensitive match per segment."""
    if not isinstance(obj, dict):
        return _MISSING
    if path in obj:
        return obj[path]
    head, _, rest = path.partition(".")
    for key in (head, *[k for k in obj if isinstance(k, str) and k.lower() == head.lower() and k != head]):
        if key in obj:
            return obj[key] if not rest else _dig(obj[key], rest)
    # whole-path case-insensitive (keys that themselves contain dots)
    for k, v in obj.items():
        if isinstance(k, str) and k.lower() == path.lower():
            return v
    return _MISSING


def _get(exp: dict, field: tuple):
    kind = field[0]
    if kind == "meta":
        return _dig(exp.get("meta") or {}, field[1])
    if kind == "metric":
        return _dig(exp.get("metrics") or {}, field[1])
    if kind == "label":
        return exp.get("label") or str(exp.get("id"))
    if kind == "idea":
        return exp.get("idea_id", _MISSING)
    v = exp.get(kind, _MISSING)
    return _MISSING if v is None else v


def _compare(actual, op: str, expected) -> bool:
    if actual is _MISSING:
        return False
    if op in ("~", "!~"):
        hit = str(expected).lower() in _text(actual).lower()
        return hit if op == "~" else not hit
    a, e = _num(actual), _num(expected)
    if a is not None and e is not None:
        return {"=": a == e, "!=": a != e, ">": a > e, ">=": a >= e, "<": a < e, "<=": a <= e}[op]
    if isinstance(expected, bool) or isinstance(actual, bool):
        av = actual if isinstance(actual, bool) else str(actual).lower() == "true"
        ev = expected if isinstance(expected, bool) else str(expected).lower() == "true"
        if op in ("=", "!="):
            return (av == ev) if op == "=" else (av != ev)
        return False
    if expected is None:
        return (actual is None) if op == "=" else (actual is not None) if op == "!=" else False
    sa, se = _text(actual), str(expected)
    return {"=": sa == se, "!=": sa != se, ">": sa > se, ">=": sa >= se, "<": sa < se, "<=": sa <= se}[op]


def _text(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True)
    return str(v)


Predicate = Callable[[dict], bool]


@dataclass
class Context:
    """What evaluation needs beyond the experiment itself."""
    children: dict[int, list[int]]           # idea id -> child idea ids
    saved: Callable[[str], dict | None]       # name/id -> saved filter record


def compile_expr(expr: str, ctx: Context, *, exclude: list[str] | None = None,
                 _depth: int = 0, _seen: tuple = ()) -> Predicate:
    """A predicate for *expr* (and an optional list of excluded labels)."""
    if _depth > MAX_REF_DEPTH:
        raise FilterError("saved filters refer to each other too deeply")
    node = parse(expr)
    under_cache: dict[int, set[int]] = {}

    def descendants(root: int) -> set[int]:
        if root not in under_cache:
            seen, stack = {root}, [root]
            while stack:
                for c in ctx.children.get(stack.pop(), []):
                    if c not in seen:
                        seen.add(c)
                        stack.append(c)
            under_cache[root] = seen
        return under_cache[root]

    ref_preds: dict[str, Predicate] = {}
    for name in refs(node):
        rec = ctx.saved(name)
        if rec is None:
            raise FilterError(f"unknown saved filter @{name}")
        key = rec["id"]
        if key in _seen:
            raise FilterError(f"saved filter @{name} refers to itself")
        ref_preds[name] = compile_expr(rec.get("expr") or "", ctx, exclude=rec.get("exclude"),
                                       _depth=_depth + 1, _seen=(*_seen, key))

    def ev(n, exp: dict) -> bool:
        op = n[0]
        if op == "all":
            return True
        if op == "and":
            return ev(n[1], exp) and ev(n[2], exp)
        if op == "or":
            return ev(n[1], exp) or ev(n[2], exp)
        if op == "not":
            return not ev(n[1], exp)
        if op == "ref":
            return ref_preds[n[1]](exp)
        if op == "under":
            return exp.get("idea_id") in descendants(n[1])
        field = n[1]
        if op == "exists":
            if field == ("tag",):
                return bool(exp.get("tags"))
            return _get(exp, field) is not _MISSING
        if field == ("tag",):
            tags = [str(t) for t in (exp.get("tags") or [])]
            if op == "in":
                return any(str(v) in tags for v in n[2])
            cmp_op, value = n[2], n[3]
            if cmp_op == "=":
                return str(value) in tags
            if cmp_op == "!=":
                return str(value) not in tags
            return any(_compare(t, cmp_op, value) for t in tags)
        actual = _get(exp, field)
        if op == "in":
            return any(_compare(actual, "=", v) for v in n[2])
        return _compare(actual, n[2], n[3])

    excluded = {str(x) for x in (exclude or [])}

    def pred(exp: dict) -> bool:
        if excluded and str(exp.get("label") or exp.get("id")) in excluded:
            return False
        return ev(node, exp)

    return pred


def idea_children(ideas: list[dict]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for idea in ideas:
        for pid in idea.get("parent_ids") or []:
            out.setdefault(pid, []).append(idea["id"])
    return out


# ── Saved filters ────────────────────────────────────────────────────────────

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:+-]{0,63}$")
_lock = threading.Lock()


def _path(repo_dir: Path) -> Path:
    return Path(repo_dir) / ".the_lab" / "filters.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(repo_dir: Path) -> list[dict]:
    p = _path(repo_dir)
    if not p.exists():
        return []
    data = json.loads(p.read_text() or "{}")
    return list(data.get("filters") or [])


def _save(repo_dir: Path, filters: list[dict]) -> None:
    p = _path(repo_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_json(p, {"filters": filters})


def list_filters(repo_dir: Path, *, include_archived: bool = False) -> list[dict]:
    with _lock:
        out = _load(repo_dir)
    if not include_archived:
        out = [f for f in out if not f.get("archived")]
    return sorted(out, key=lambda f: (not f.get("shared"), f.get("name", "").lower()))


def get_filter(repo_dir: Path, ref: str, *, include_archived: bool = False) -> dict | None:
    """A saved filter by id or (case-insensitive) name."""
    ref = (ref or "").strip().lstrip("@")
    # In expressions a name can't contain spaces: @legacy_hidden finds "legacy hidden".
    loose = ref.lower().replace("_", " ")
    for f in list_filters(repo_dir, include_archived=include_archived):
        name = f.get("name", "").lower()
        if f["id"] == ref or name == ref.lower() or name.replace("_", " ") == loose:
            return f
    return None


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise FilterError("name: 1-64 characters, letters, digits, space, _ . : + -, starting with a letter or digit")
    return name


def create_filter(repo_dir: Path, *, name: str, expr: str, owner: str, description: str = "",
                  shared: bool = False, exclude: list[str] | None = None,
                  copied_from: str | None = None) -> dict:
    name = validate_name(name)
    parse(expr)
    with _lock:
        filters = _load(repo_dir)
        if any(f.get("name", "").lower() == name.lower() and not f.get("archived") for f in filters):
            raise FilterError(f"a filter named {name!r} already exists")
        rec = {
            "id": "f_" + uuid.uuid4().hex[:8],
            "name": name,
            "expr": expr or "",
            "exclude": sorted({str(x) for x in (exclude or [])}),
            "description": (description or "").strip(),
            "owner": owner,
            "shared": bool(shared),
            "copied_from": copied_from,
            "created_at": _now(),
            "updated_at": _now(),
            "archived": False,
        }
        filters.append(rec)
        _save(repo_dir, filters)
    return rec


def update_filter(repo_dir: Path, filter_id: str, **changes) -> dict:
    allowed = {"name", "expr", "description", "shared", "exclude", "archived"}
    with _lock:
        filters = _load(repo_dir)
        for i, f in enumerate(filters):
            if f["id"] != filter_id:
                continue
            rec = dict(f)
            for k, v in changes.items():
                if k not in allowed or v is None:
                    continue
                if k == "name":
                    v = validate_name(v)
                    if any(o["id"] != filter_id and o.get("name", "").lower() == v.lower()
                           and not o.get("archived") for o in filters):
                        raise FilterError(f"a filter named {v!r} already exists")
                if k == "expr":
                    parse(v)
                if k == "exclude":
                    v = sorted({str(x) for x in v})
                rec[k] = v
            rec["updated_at"] = _now()
            filters[i] = rec
            _save(repo_dir, filters)
            return rec
    raise KeyError(filter_id)
