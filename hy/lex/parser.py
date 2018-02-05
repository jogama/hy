# Copyright 2018 the authors.
# This file is part of Hy, which is free software licensed under the Expat
# license. See the LICENSE.

from __future__ import unicode_literals

from functools import wraps

from rply import ParserGenerator

from hy._compat import str_type
from hy.models import (HyBytes, HyComplex, HyCons, HyDict, HyExpression,
                       HyFloat, HyInteger, HyKeyword, HyList, HySet, HyString,
                       HySymbol)
from .lexer import lexer
from .exceptions import LexException, PrematureEndOfInput


pg = ParserGenerator(
    [rule.name for rule in lexer.rules] + ['$end'],
    cache_id="hy_parser"
)


def hy_symbol_mangle(p):
    if p.startswith("*") and p.endswith("*") and p not in ("*", "**"):
        p = p[1:-1].upper()

    if "-" in p and p != "-":
        p = p.replace("-", "_")

    if p.endswith("?") and p != "?":
        p = "is_%s" % (p[:-1])

    if p.endswith("!") and p != "!":
        p = "%s_bang" % (p[:-1])

    return p


def hy_symbol_unmangle(p):
    # hy_symbol_mangle is one-way, so this can't be perfect.
    # But it can be useful till we have a way to get the original
    # symbol (https://github.com/hylang/hy/issues/360).
    p = str_type(p)

    if p.endswith("_bang") and p != "_bang":
        p = p[:-len("_bang")] + "!"

    if p.startswith("is_") and p != "is_":
        p = p[len("is_"):] + "?"

    if "_" in p and p != "_":
        p = p.replace("_", "-")

    if (all([c.isalpha() and c.isupper() or c == '_' for c in p]) and
            any([c.isalpha() for c in p])):
        p = '*' + p.lower() + '*'

    return p


def set_boundaries(fun):
    @wraps(fun)
    def wrapped(p):
        start = p[0].source_pos
        end = p[-1].source_pos
        ret = fun(p)
        ret.start_line = start.lineno
        ret.start_column = start.colno
        if start is not end:
            ret.end_line = end.lineno
            ret.end_column = end.colno
        else:
            ret.end_line = start.lineno
            ret.end_column = start.colno + len(p[0].value)
        return ret
    return wrapped


def set_quote_boundaries(fun):
    @wraps(fun)
    def wrapped(p):
        start = p[0].source_pos
        ret = fun(p)
        ret.start_line = start.lineno
        ret.start_column = start.colno
        ret.end_line = p[-1].end_line
        ret.end_column = p[-1].end_column
        return ret
    return wrapped


@pg.production("main : list_contents")
def main(p):
    return p[0]


@pg.production("main : $end")
def main_empty(p):
    return []


def reject_spurious_dots(*items):
    "Reject the spurious dots from items"
    for list in items:
        for tok in list:
            if tok == "." and type(tok) == HySymbol:
                raise LexException("Malformed dotted list",
                                   tok.start_line, tok.start_column)


@pg.production("paren : LPAREN list_contents RPAREN")
@set_boundaries
def paren(p):
    cont = p[1]

    # Dotted lists are expressions of the form
    # (a b c . d)
    # that evaluate to nested cons cells of the form
    # (a . (b . (c . d)))
    if len(cont) >= 3 and isinstance(cont[-2], HySymbol) and cont[-2] == ".":

        reject_spurious_dots(cont[:-2], cont[-1:])

        if len(cont) == 3:
            # Two-item dotted list: return the cons cell directly
            return HyCons(cont[0], cont[2])
        else:
            # Return a nested cons cell
            return HyCons(cont[0], paren([p[0], cont[1:], p[2]]))

    # Warn preemptively on a malformed dotted list.
    # Only check for dots after the first item to allow for a potential
    # attribute accessor shorthand
    reject_spurious_dots(cont[1:])

    return HyExpression(p[1])


@pg.production("paren : LPAREN RPAREN")
@set_boundaries
def empty_paren(p):
    return HyExpression([])


@pg.production("list_contents : term list_contents")
def list_contents(p):
    return [p[0]] + p[1]


@pg.production("list_contents : term")
def list_contents_single(p):
    return [p[0]]


@pg.production("list_contents : DISCARD term discarded_list_contents")
def list_contents_empty(p):
    return []


@pg.production("discarded_list_contents : DISCARD term discarded_list_contents")
@pg.production("discarded_list_contents :")
def discarded_list_contents(p):
    pass


@pg.production("term : identifier")
@pg.production("term : paren")
@pg.production("term : dict")
@pg.production("term : list")
@pg.production("term : set")
@pg.production("term : string")
def term(p):
    return p[0]


@pg.production("term : DISCARD term term")
def term_discard(p):
    return p[2]


@pg.production("term : QUOTE term")
@set_quote_boundaries
def term_quote(p):
    return HyExpression([HySymbol("quote"), p[1]])


@pg.production("term : QUASIQUOTE term")
@set_quote_boundaries
def term_quasiquote(p):
    return HyExpression([HySymbol("quasiquote"), p[1]])


@pg.production("term : UNQUOTE term")
@set_quote_boundaries
def term_unquote(p):
    return HyExpression([HySymbol("unquote"), p[1]])


@pg.production("term : UNQUOTESPLICE term")
@set_quote_boundaries
def term_unquote_splice(p):
    return HyExpression([HySymbol("unquote_splice"), p[1]])


@pg.production("term : HASHSTARS term")
@set_quote_boundaries
def term_hashstars(p):
    n_stars = len(p[0].getstr()[1:])
    if n_stars == 1:
        sym = "unpack_iterable"
    elif n_stars == 2:
        sym = "unpack_mapping"
    else:
        raise LexException(
            "Too many stars in `#*` construct (if you want to unpack a symbol "
            "beginning with a star, separate it with whitespace)",
            p[0].source_pos.lineno, p[0].source_pos.colno)
    return HyExpression([HySymbol(sym), p[1]])


@pg.production("term : HASHOTHER term")
@set_quote_boundaries
def hash_other(p):
    # p == [(Token('HASHOTHER', '#foo'), bar)]
    st = p[0].getstr()[1:]
    str_object = HyString(st)
    expr = p[1]
    return HyExpression([HySymbol("dispatch_tag_macro"), str_object, expr])


@pg.production("set : HLCURLY list_contents RCURLY")
@set_boundaries
def t_set(p):
    return HySet(p[1])


@pg.production("set : HLCURLY RCURLY")
@set_boundaries
def empty_set(p):
    return HySet([])


@pg.production("dict : LCURLY list_contents RCURLY")
@set_boundaries
def t_dict(p):
    return HyDict(p[1])


@pg.production("dict : LCURLY RCURLY")
@set_boundaries
def empty_dict(p):
    return HyDict([])


@pg.production("list : LBRACKET list_contents RBRACKET")
@set_boundaries
def t_list(p):
    return HyList(p[1])


@pg.production("list : LBRACKET RBRACKET")
@set_boundaries
def t_empty_list(p):
    return HyList([])


@pg.production("string : STRING")
@set_boundaries
def t_string(p):
    # Replace the single double quotes with triple double quotes to allow
    # embedded newlines.
    s = eval(p[0].value.replace('"', '"""', 1)[:-1] + '"""')
    return (HyString if isinstance(s, str_type) else HyBytes)(s)


@pg.production("string : PARTIAL_STRING")
def t_partial_string(p):
    # Any unterminated string requires more input
    raise PrematureEndOfInput("Premature end of input")


bracket_string_re = next(r.re for r in lexer.rules if r.name == 'BRACKETSTRING')
@pg.production("string : BRACKETSTRING")
@set_boundaries
def t_bracket_string(p):
    m = bracket_string_re.match(p[0].value)
    delim, content = m.groups()
    return HyString(content, brackets=delim)


@pg.production("identifier : IDENTIFIER")
@set_boundaries
def t_identifier(p):
    obj = p[0].value

    val = symbol_like(obj)
    if val is not None:
        return val

    if "." in obj and symbol_like(obj.split(".", 1)[0]) is not None:
        # E.g., `5.attr` or `:foo.attr`
        raise LexException(
            'Cannot access attribute on anything other than a name (in '
            'order to get attributes of expressions, use '
            '`(. <expression> <attr>)` or `(.<attr> <expression>)`)',
            p[0].source_pos.lineno, p[0].source_pos.colno)

    return HySymbol(".".join(hy_symbol_mangle(x) for x in obj.split(".")))


def symbol_like(obj):
    "Try to interpret `obj` as a number or keyword."

    try:
        return HyInteger(obj)
    except ValueError:
        pass

    if '/' in obj:
        try:
            lhs, rhs = obj.split('/')
            return HyExpression([HySymbol('fraction'), HyInteger(lhs),
                                 HyInteger(rhs)])
        except ValueError:
            pass

    try:
        return HyFloat(obj)
    except ValueError:
        pass

    if obj != 'j':
        try:
            return HyComplex(obj)
        except ValueError:
            pass

    if obj.startswith(":") and "." not in obj:
        return HyKeyword(obj)


@pg.error
def error_handler(token):
    tokentype = token.gettokentype()
    if tokentype == '$end':
        raise PrematureEndOfInput("Premature end of input")
    else:
        raise LexException(
            "Ran into a %s where it wasn't expected." % tokentype,
            token.source_pos.lineno, token.source_pos.colno)


parser = pg.build()
