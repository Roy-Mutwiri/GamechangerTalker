"""The condition DSL.

A template's `when` string decides whether that line is appropriate right
now. It is a tiny expression language over the fact dict.

This does NOT use eval(). The expression is parsed with ast.parse(mode=
'eval') and walked; anything outside the whitelist raises at load time, when
the operator can see it, rather than at 3am mid-stream.

Permitted:
    names ............ any fact in the fact registry
    comparisons ...... <  <=  >  >=  ==  !=
    boolean .......... and  or  not
    arithmetic ....... +  -  *  /  and unary minus
    literals ......... numbers, strings, true/false, None
    membership ....... x in ["a", "b"]   /   x not in ["a", "b"]

Everything else -- calls, attributes, subscripts, lambdas, comprehensions,
walrus, f-strings -- is rejected.

Missing data: any fact may be None (not enough history yet, feed down). A
comparison involving None evaluates to False rather than raising, so a
template simply does not fire until its inputs exist.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["ALWAYS", "Condition", "ConditionError", "compile_condition"]


class ConditionError(ValueError):
    """Raised at load time for a malformed or unsafe condition."""


_COMPARE_OPS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}

_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}

_NODE_DESCRIPTIONS = {
    ast.Call: "function calls",
    ast.Attribute: "attribute access",
    ast.Subscript: "indexing",
    ast.Lambda: "lambdas",
    ast.ListComp: "comprehensions",
    ast.DictComp: "comprehensions",
    ast.SetComp: "comprehensions",
    ast.GeneratorExp: "generator expressions",
    ast.JoinedStr: "f-strings",
    ast.NamedExpr: "walrus assignment",
    ast.Starred: "star unpacking",
    ast.IfExp: "conditional expressions",
    ast.Dict: "dict literals",
    ast.Set: "set literals",
}


@dataclass(frozen=True)
class Condition:
    """A compiled, validated `when` expression."""

    source: str
    names: frozenset[str]
    _tree: ast.expr

    def evaluate(self, facts: dict[str, Any]) -> bool:
        return bool(_truthy(_eval(self._tree, facts)))

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return self.source


def compile_condition(
    expr: str,
    known_facts: set[str] | frozenset[str],
    *,
    where: str = "<condition>",
) -> Condition:
    """Parse, validate and return a Condition, or raise ConditionError.

    `where` is quoted in error messages -- pass "file.json:template.id" so
    the operator knows exactly which line to fix.
    """
    if expr is None or not str(expr).strip():
        return ALWAYS

    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError as exc:
        raise ConditionError(
            f"{where}: cannot parse condition {expr!r}: {exc.msg}"
        ) from exc

    names: set[str] = set()
    _validate(tree.body, expr, where, names)

    unknown = sorted(n for n in names if n not in known_facts)
    if unknown:
        raise ConditionError(
            f"{where}: condition {expr!r} refers to unknown fact(s) "
            f"{', '.join(unknown)}. "
            f"{_suggest(unknown[0], known_facts)}"
        )
    return Condition(source=str(expr), names=frozenset(names), _tree=tree.body)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(node: ast.AST, expr: str, where: str, names: set[str]) -> None:
    for bad, description in _NODE_DESCRIPTIONS.items():
        if isinstance(node, bad):
            raise ConditionError(
                f"{where}: {description} are not allowed in conditions ({expr!r})"
            )

    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):  # pragma: no cover
            raise ConditionError(f"{where}: unsupported boolean operator")
        for child in node.values:
            _validate(child, expr, where, names)
        return

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
            raise ConditionError(f"{where}: unsupported unary operator in {expr!r}")
        _validate(node.operand, expr, where, names)
        return

    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS:
            raise ConditionError(
                f"{where}: only + - * / are allowed in conditions ({expr!r})"
            )
        _validate(node.left, expr, where, names)
        _validate(node.right, expr, where, names)
        return

    if isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _COMPARE_OPS and not isinstance(op, (ast.In, ast.NotIn)):
                raise ConditionError(
                    f"{where}: unsupported comparison operator in {expr!r} "
                    "(is/is not are not allowed; use == and !=)"
                )
        _validate(node.left, expr, where, names)
        for comparator in node.comparators:
            _validate(comparator, expr, where, names)
        return

    if isinstance(node, (ast.List, ast.Tuple)):
        for element in node.elts:
            if not isinstance(element, ast.Constant):
                raise ConditionError(
                    f"{where}: list literals may only contain constants ({expr!r})"
                )
        return

    if isinstance(node, ast.Name):
        if not isinstance(node.ctx, ast.Load):
            raise ConditionError(f"{where}: assignment is not allowed ({expr!r})")
        if node.id in ("true", "false", "null"):
            raise ConditionError(
                f"{where}: use Python spelling True/False/None, not "
                f"{node.id!r} ({expr!r})"
            )
        names.add(node.id)
        return

    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, str, bool, type(None))):
            raise ConditionError(f"{where}: unsupported literal in {expr!r}")
        return

    raise ConditionError(
        f"{where}: {type(node).__name__} is not allowed in conditions ({expr!r})"
    )


def _suggest(name: str, known: set[str] | frozenset[str]) -> str:
    import difflib

    close = difflib.get_close_matches(name, sorted(known), n=3, cutoff=0.6)
    if close:
        return f"Did you mean {' or '.join(close)}?"
    return "Run --list-facts to see every available fact."


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _truthy(value: Any) -> bool:
    return False if value is None else bool(value)


def _eval(node: ast.AST, facts: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        return facts.get(node.id)

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for child in node.values:
                value = _eval(child, facts)
                if not _truthy(value):
                    return False
                result = value
            return result
        for child in node.values:
            value = _eval(child, facts)
            if _truthy(value):
                return value
        return False

    if isinstance(node, ast.UnaryOp):
        value = _eval(node.operand, facts)
        if isinstance(node.op, ast.Not):
            return not _truthy(value)
        if value is None:
            return None
        return -value if isinstance(node.op, ast.USub) else +value

    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, facts), _eval(node.right, facts)
        if left is None or right is None:
            return None
        try:
            return _BIN_OPS[type(node.op)](left, right)
        except ZeroDivisionError:
            return None

    if isinstance(node, ast.Compare):
        left = _eval(node.left, facts)
        for op, comparator_node in zip(node.ops, node.comparators, strict=False):
            right = _eval(comparator_node, facts)
            if isinstance(op, (ast.In, ast.NotIn)):
                container = right if right is not None else ()
                inside = left in container
                ok = inside if isinstance(op, ast.In) else not inside
            else:
                if left is None or right is None:
                    # Missing data never satisfies a comparison, except for
                    # explicit equality checks against None.
                    if isinstance(op, ast.Eq):
                        ok = left is None and right is None
                    elif isinstance(op, ast.NotEq):
                        ok = not (left is None and right is None)
                    else:
                        return False
                else:
                    try:
                        ok = _COMPARE_OPS[type(op)](left, right)
                    except TypeError:
                        return False
            if not ok:
                return False
            left = right
        return True

    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, facts) for e in node.elts]

    raise ConditionError(  # pragma: no cover - _validate rejects these first
        f"{type(node).__name__} is not evaluable"
    )


ALWAYS = Condition(
    source="True", names=frozenset(), _tree=ast.parse("True", mode="eval").body
)
