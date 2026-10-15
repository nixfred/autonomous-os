"""Keep user data out of remote logs.

Log calls stay as they are: the console and the rotating file on the device get
every record in full, for debugging there. Only the copy sent to GELF is
rewritten (gelf_message), and GELF is default-deny:

- The message ships only where it comes from a string literal in the source:
  the call site is looked up from the record's file and line (HAL runs from
  source). A %-style template ships as is; in an f-string or a ``+`` chain the
  literal parts ship and each value goes through the rules below. A message the
  source does not show as a literal ships as ``<redacted N chars>``.
- A value ships when it is a number, None, an enum member or an exception, or
  when its key is in SAFE_KEYS: the name of the expression passed
  (``self.model``, ``e``), or for an unnamed expression (a literal, a call) the
  word right before it in the template (``provider=%s``). Anything else becomes
  ``<redacted N chars>``. In an f-string a value has already been rendered, so a
  number ships when the format spec is numeric or the text is a short number.
- ``private(value)`` redacts a value even under a safe key.
- Telegram bot tokens and URL query strings are scrubbed from whatever ships.
"""

from __future__ import annotations

import ast
import enum
import functools
import logging
import numbers
import re
import tokenize
import warnings
from typing import Any

# A Telegram bot token (<bot id>:<secret>). Errors from Bot API calls quote the
# request URL, which embeds it.
_TELEGRAM_BOT_TOKEN = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
# The query string of a URL ("?name=Alice&q=..."): request errors quote the URL.
_URL_QUERY = re.compile(r"\?[^\s#'\"]*=[^\s#'\"]*")


def scrub_secrets(text: str) -> str:
    """Remove credentials and URL query strings before a record leaves the device."""
    text = _TELEGRAM_BOT_TOKEN.sub("<redacted-token>", text)
    return _URL_QUERY.sub("?<redacted>", text)


class Private:
    """A log argument that may be written locally but never shipped off the device."""

    __slots__ = ("value",)

    def __init__(self, value: Any):
        self.value = value

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return repr(self.value)

    def __format__(self, spec: str) -> str:
        return format(self.value, spec)


def private(value: Any) -> Private:
    """Redact a log argument in GELF even when its key is in SAFE_KEYS."""
    return value if isinstance(value, Private) else Private(value)


def _redacted(value: Any) -> str:
    if isinstance(value, Private):
        value = value.value
    if isinstance(value, str):
        return f"<redacted {len(value)} chars>"
    if isinstance(value, (bytes, bytearray)):
        return f"<redacted {len(value)} bytes>"
    return "<redacted>"


class _Redacted:
    __slots__ = ("text",)

    def __init__(self, value: Any):
        self.text = _redacted(value)

    def __str__(self) -> str:
        return self.text

    __repr__ = __str__

    def __format__(self, spec: str) -> str:
        return self.text


# Keys whose string values may ship to GELF: technical ids, enumerations,
# providers and hardware names. Never add a key that can hold what a person
# said, who they are or what the device observed about them.
SAFE_KEYS = frozenset({
    # errors and status
    "error", "err", "exc", "e", "ex", "exception", "__name__",
    "status", "status_code", "code", "reason", "stop_reason",
    # ids and enumerations
    "component", "interaction", "interaction_id", "call_id", "turn_id", "user_turn_id",
    "latest_id", "request", "gen", "owner", "text_key", "event", "type", "kind",
    "mode", "phase", "state", "origin", "policy", "direction", "route",
    # providers, models and endpoints
    "provider", "model", "backend", "voice", "lang", "api",
    "base_url", "endpoint", "endpoint_method", "method", "content-type",
    "content-encoding",
    # hardware and audio routing
    "driver", "device", "board", "bus", "media", "sink", "pa_sink", "pa_source",
    "alsa_in", "output_device", "sound_device_id", "axis", "servo", "scene",
    "effect", "color",
})

# Expression names that hold user data: a value passed under one of these never
# ships, whatever the template calls it ("status: %s", transcript).
_USER_DATA_NAMES = frozenset({
    "transcript", "combined", "final_msg", "delegate_msg", "sensing_msg", "delta",
    "text", "sentence", "speech", "query", "message", "msg", "person", "person_id",
    "face_id", "user", "norm_user", "last_user", "new_user", "name", "resolved_name",
    "best_name", "telegram_username", "telegram_id", "agent_name", "wake_words",
    "_wake_words", "label", "target_label", "vp_hash", "voiceprint_hash",
})

_SPEC = re.compile(
    r"%(?:\((?P<name>[^)]*)\))?[#0\- +]*(?P<width>\*|\d+)?"
    r"(?:\.(?P<prec>\*|\d+))?[hlL]?(?P<conv>[diouxXeEfFgGcrsa%])"
)
_KEY_BEFORE = re.compile(r"([A-Za-z_][\w.\-]*)\s*[=:]\s*['\"(\[]?$")
# A short number rendered into an f-string: longer digit runs may be phone
# numbers or codes a person said.
_SHORT_NUMBER = re.compile(r"-?\d{1,6}(?:\.\d+)?|-?\d*\.\d+|True|False|None")
_NUMBER = re.compile(r"[-+]?(?:\d[\d,_]*)?(?:\.\d+)?(?:[eE][-+]?\d+)?%?|[-+]?(?:inf|nan)")
_NUMERIC_SPEC = re.compile(r".*[bdeEfFgGnoxX%]$")
_NUMERIC_CALLS = frozenset({"len", "int", "float", "round", "abs", "sum", "min", "max"})
_MAX_SPLIT_LEN = 8192


def _expr_name(node: ast.AST) -> str | None:
    """The name a value is passed under: ``x`` → x, ``self.model`` → model."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _expr_name(node.value)
    if isinstance(node, ast.Call):
        func = _expr_name(node.func)
        if func in _NUMERIC_CALLS:
            return "#"
        if func == "private":
            return "<private>"
    return None


def _key_for(template_key: str | None, name: str | None) -> str | None:
    """The key a value ships under, or None when it must be redacted. The name
    of the expression passed describes the value best; the word before it in
    the template counts only for an unnamed expression (a literal, a call)."""
    key = name if name is not None else template_key
    if key in _USER_DATA_NAMES:
        return None
    return key if key in SAFE_KEYS else None


def _shippable(key: str | None, name: str | None, value: Any) -> Any:
    """The value to format into a GELF message under the default-deny policy."""
    if isinstance(value, Private):
        return _Redacted(value)
    if value is None or isinstance(value, (numbers.Number, enum.Enum, BaseException)):
        return value
    if _key_for(key, name) is not None:
        return value
    return _Redacted(value)


def _positional_keys(fmt: str) -> list[str | None]:
    """The key naming each positional argument the template consumes, in order."""
    keys: list[str | None] = []
    last = 0
    for spec in _SPEC.finditer(fmt):
        if spec.group("conv") == "%":
            last = spec.end()
            continue
        for star in (spec.group("width"), spec.group("prec")):
            if star == "*":
                keys.append("*")  # a width/precision argument: always a number
        before = _KEY_BEFORE.search(fmt[last:spec.start()])
        keys.append(before.group(1) if before else None)
        last = spec.end()
    return keys


@functools.lru_cache(maxsize=1024)
def _calls_by_line(path: str) -> dict[int, tuple[ast.Call, ...]]:
    """Every call in ``path``, indexed by the lines a log record can report for it."""
    try:
        with tokenize.open(path) as f:
            source = f.read()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source, path)
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        return {}
    calls: dict[int, list[ast.Call]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            # A record reports the line of the method name: "logger\n.info(" is
            # the second line.
            for line in {node.lineno, node.func.end_lineno}:
                calls.setdefault(line, []).append(node)
    return {line: tuple(nodes) for line, nodes in calls.items()}


class _Hole:
    __slots__ = ("name", "numeric")

    def __init__(self, node: ast.AST, numeric: bool = False):
        self.name = _expr_name(node)
        self.numeric = numeric or self.name == "#"


def _template_parts(node: ast.AST) -> list[str | _Hole] | None:
    """A message expression as literal text and value holes, or None when the
    message is not built from literals (a variable, a call)."""
    parts = _raw_parts(node)
    if parts is None:
        return None
    merged: list[str | _Hole] = []
    for part in parts:
        if isinstance(part, str) and merged and isinstance(merged[-1], str):
            merged[-1] += part
        elif part != "":
            merged.append(part)
    return merged


def _raw_parts(node: ast.AST) -> list[str | _Hole] | None:
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts: list[str | _Hole] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(value.value)
            else:
                spec = ast.unparse(value.format_spec) if value.format_spec else ""
                parts.append(_Hole(value.value, numeric=bool(_NUMERIC_SPEC.match(spec))))
        return parts
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _raw_parts(node.left), _raw_parts(node.right)
        if left is None and right is None:
            return None
        return (left or [_Hole(node.left)]) + (right or [_Hole(node.right)])
    return None


def _split(parts: list[str | _Hole], text: str, lazy: bool) -> list[str] | None:
    group = "(.*?)" if lazy else "(.*)"
    pattern = "".join(group if isinstance(p, _Hole) else re.escape(p) for p in parts)
    match = re.fullmatch(pattern, text, re.DOTALL)
    return list(match.groups()) if match else None


def _render_joined(parts: list[str | _Hole], text: str) -> str | None:
    """``text`` (an f-string's output) with each value hole put through the
    default-deny policy, or None when ``parts`` cannot have produced it."""
    holes = [p for p in parts if isinstance(p, _Hole)]
    if not holes:
        return text if "".join(parts) == text else None  # type: ignore[arg-type]
    if len(text) > _MAX_SPLIT_LEN or len(holes) > 16:
        return None
    lazy = _split(parts, text, lazy=True)
    if lazy is None:
        return None
    # When literal text also occurs inside a value, the split is ambiguous and a
    # value could spill into a hole that would ship: redact every hole then.
    unambiguous = lazy == _split(parts, text, lazy=False)
    out, values, before = [], iter(lazy), ""
    for part in parts:
        if not isinstance(part, _Hole):
            out.append(part)
            before = part
            continue
        value = next(values)
        key_match = _KEY_BEFORE.search(before)
        key = _key_for(key_match.group(1) if key_match else None, part.name)
        ships = unambiguous and part.name != "<private>" and (
            key is not None
            or (part.numeric and _NUMBER.fullmatch(value) is not None and value != "")
            or (part.name not in _USER_DATA_NAMES and _SHORT_NUMBER.fullmatch(value) is not None)
        )
        out.append(value if ships else _redacted(value))
        before = ""
    return "".join(out)


def _format_template(msg: str, args: Any, arg_nodes: list[ast.AST] | None) -> str:
    """A literal %-style template formatted with default-deny arguments."""
    if isinstance(args, dict) and "%(" in msg:
        return msg % {k: _shippable(k, None, v) for k, v in args.items()}
    values = args if isinstance(args, tuple) else (args,)
    keys = _positional_keys(msg)
    if len(keys) != len(values):
        raise ValueError("template and arguments disagree")
    names: list[str | None] = [None] * len(values)
    if arg_nodes is not None and len(arg_nodes) == len(values):
        names = [_expr_name(n) for n in arg_nodes]
    return msg % tuple(
        v if k == "*" else _shippable(k, n, v) for k, n, v in zip(keys, names, values)
    )


def _site_message(record: logging.LogRecord) -> str | None:
    """The GELF message for ``record`` rebuilt from its call site, or None when
    the source does not show the message as a literal."""
    msg, args = record.msg, record.args
    for call in _calls_by_line(record.pathname).get(record.lineno, ()):
        # logger.info(msg, ...) or logger.log(level, msg, ...)
        for index in (0, 1):
            if index >= len(call.args):
                break
            parts = _template_parts(call.args[index])
            if parts is None:
                continue
            rest = call.args[index + 1:]
            arg_nodes = None if any(isinstance(n, ast.Starred) for n in rest) else rest
            if args:
                if parts != [msg]:
                    continue
                try:
                    return _format_template(msg, args, arg_nodes)
                except Exception:
                    return msg + " <arguments redacted>"
            rendered = _render_joined(parts, msg)
            if rendered is not None:
                return rendered
    return None


def gelf_message(record: logging.LogRecord) -> str:
    """Render ``record``'s message for GELF under the default-deny policy (see
    the module docstring). The record is shared with the local handlers and is
    never mutated."""
    msg = record.msg
    if isinstance(msg, BaseException):
        return scrub_secrets(str(msg))
    if not isinstance(msg, str):
        return _redacted(msg)
    try:
        rendered = _site_message(record)
    except Exception:
        rendered = None
    if rendered is None:
        return _redacted(msg)
    return scrub_secrets(rendered)


def gelf_record(record: logging.LogRecord) -> logging.LogRecord:
    """A copy of ``record`` whose message is the GELF rendering (gelf_message)."""
    copy = logging.makeLogRecord(record.__dict__)
    copy.msg, copy.args = gelf_message(record), None
    return copy
