"""Invariant 1, statically: the collector never writes to Facebook and never speaks for the page.

Three checks over source code (the runtime half is ``GuardedCdp`` and the protocol trace):

- **Import boundary.** Only ``fbcollector/driver/_patchright.py`` imports Patchright, anywhere in
  ``packages/`` and ``scripts/`` (``import``, ``from … import``, ``importlib`` or ``__import__``
  with the name as a string).
- **A fixed list of calls**, in that module and in the driver modules that touch what it hands
  out (``capture`` gets live responses and requests, ``cdp`` the raw CDP session, ``gestures``
  and ``selftest``). Every ``x.method(...)`` in them must call a method on that module's explicit
  allow-list, *whatever the receiver is called* — an alias (``tab = self._page``) changes
  nothing. On top of that, a receiver that names a page, context, mouse, CDP session, Playwright
  or Chromium object may call only the browser allow-list; receivers must be plain names except
  for a few pure string and dict methods (``(await f()).click()`` would dodge the check); a set
  of acting methods (``click``, ``fill``, ``evaluate`` without the isolated world,
  ``add_init_script``, ``route``, …) is refused whatever the receiver.
- **Attributes that lead to acting objects are never touched**: ``keyboard``, ``touchscreen``,
  ``request`` (``context.request`` sends its own HTTP requests with the session's cookies,
  from the Node driver, past ``UrlPolicy`` and past CDP), ``frame``/``frames``/``main_frame``,
  ``locator``, ``wait_for_function`` and every ``expect_*``. The one exception is written out:
  ``capture`` reads ``response.request`` for the friendly name.
- **No dynamic dispatch.** ``getattr`` with a non-literal name, ``setattr``, ``eval``, ``exec``
  and ``operator.methodcaller``/``attrgetter`` are refused; ``isolated_context`` must be the
  literal ``True`` wherever it appears, and every evaluate-family call must pass it. The import
  check follows aliases (``from importlib import import_module as im``, ``import importlib as
  il``, ``getattr(builtins, "__import__")``, a bare reference to either function).

Each rule is also run against small bad snippets, so the checker cannot pass by being blind.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DRIVER_DIR = REPO / "packages/fbcollector/fbcollector/driver"
DRIVER = DRIVER_DIR / "_patchright.py"
SCANNED = (REPO / "packages", REPO / "scripts")

BROWSER_RECEIVER = re.compile(
    r"(?:^|_)(page|pages|context|mouse|keyboard|touchscreen|cdp|session|playwright|chromium|"
    r"browser|manager|response|request)$"
)
BROWSER_CALLS = frozenset(
    {
        # lifecycle
        "start",
        "stop",
        "launch_persistent_context",
        "new_page",
        "new_cdp_session",
        "close",
        "on",
        # the three things the invariant allows
        "goto",
        "move",
        "wheel",
        "body",
        "cookies",
    }
)
ACTING = frozenset(
    {
        "click",
        "dblclick",
        "tap",
        "fill",
        "type",
        "press",
        "press_sequentially",
        "check",
        "uncheck",
        "select_option",
        "set_input_files",
        "focus",
        "hover",
        "drag_and_drop",
        "drag_to",
        "dispatch_event",
        "down",
        "up",
        "insert_text",
        "evaluate",
        "evaluate_handle",
        "eval_on_selector",
        "eval_on_selector_all",
        "add_init_script",
        "add_script_tag",
        "add_style_tag",
        "expose_function",
        "expose_binding",
        "route",
        "route_from_har",
        "route_web_socket",
        "unroute",
        "set_content",
        "set_extra_http_headers",
        "set_viewport_size",
        "emulate_media",
        "add_cookies",
        "clear_cookies",
        "grant_permissions",
        "set_geolocation",
        "set_offline",
        "reload",
        "go_back",
        "go_forward",
        "fetch",
        "post",
        "put",
        "delete",
        "patch",
        "request",
        "send",
    }
)
EVALUATE_FAMILY = frozenset(
    {"evaluate", "evaluate_handle", "eval_on_selector", "eval_on_selector_all", "evaluate_all"}
)
FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "keyboard",
        "touchscreen",
        "request",
        "frame",
        "frames",
        "main_frame",
        "locator",
        "wait_for_function",
    }
)
FORBIDDEN_PREFIXES = ("expect_",)
ATTRIBUTE_EXCEPTIONS = frozenset({("capture.py", "response.request")})
"""(module, expression): a forbidden attribute that module may read, spelled out exactly."""
DYNAMIC = frozenset({"setattr", "eval", "exec", "__import__", "delattr"})
UNNAMED_OK = frozenset(
    {"get", "join", "strip", "lower", "upper", "encode", "decode", "items", "create_task"}
)
"""String, dict and event-loop methods that may be called on an unnamed receiver
(``"".join(...)``, ``asyncio.get_running_loop().create_task(...)``)."""
BROWSER_EXTRA: dict[str, frozenset[str]] = {
    "cdp.py": frozenset({"send", "detach", "ensure_allowed"}),
    "selftest.py": frozenset({"append", "launch", "navigate", "scroll_step", "park"}),
}
"""Calls a browser-named receiver may make in that module besides ``BROWSER_CALLS``: the
guarded session's own methods, the Driver protocol, and the self-test site's request log."""

_LOGGING = {"getLogger", "debug", "info", "warning"}
MODULE_CALLS: dict[str, frozenset[str]] = {
    "_patchright.py": frozenset(
        BROWSER_CALLS
        | _LOGGING
        | {
            # the driver's own helpers and the capture buffer
            "_await_stories",
            "_factory",
            "_move_to_margin",
            "_require",
            "_safe_count",
            "_shut_down",
            "_start",
            "_step",
            "discard",
            "settle",
            "take",
            # asyncio, the clock, the rng, files and the process
            "Event",
            "is_set",
            "set",
            "wait",
            "wait_for",
            "get_running_loop",
            "to_thread",
            "sleep",
            "time",
            "monotonic",
            "uniform",
            "randint",
            "suppress",
            "get",
            "Random",
            "GzipFile",
            "fdopen",
            "open",
            "write",
            "kill",
        }
    ),
    "capture.py": frozenset(
        _LOGGING
        | {
            "body",
            "_fetch",
            "_now",
            "take",
            "Event",
            "add",
            "add_done_callback",
            "append",
            "clear",
            "create_task",
            "get",
            "get_running_loop",
            "isalnum",
            "items",
            "join",
            "lower",
            "pop",
            "set",
            "time",
            "wait",
            "wait_for",
        }
    ),
    "cdp.py": frozenset(
        _LOGGING
        | {
            # ``send`` only through GuardedCdp: the raw session's is behind ensure_allowed
            "send",
            "detach",
            "ensure_allowed",
            "append",
            "b64decode",
            "compile",
            "get",
            "group",
            "lower",
            "search",
            "strip",
            "sub",
            "unescape",
            "upper",
        }
    ),
    "gestures.py": frozenset({"append", "extend", "hypot", "randint", "random", "sin", "uniform"}),
    "selftest.py": frozenset(
        _LOGGING
        | {
            # the Driver protocol against the local site
            "launch",
            "navigate",
            "scroll_step",
            "park",
            "close",
            "read_attribute",
            "ensure_allowed",
            "url",
            # the local HTTP server (stdlib)
            "Thread",
            "ThreadingHTTPServer",
            "_send",
            "send_header",
            "send_response",
            "end_headers",
            "flush",
            "read",
            "write",
            "server_close",
            "shutdown",
            "start",
            "append",
            "decode",
            "dumps",
            "encode",
            "get",
            "join",
            "loads",
            "sleep",
            "suppress",
            # the test hook that holds a stream open (threading.Event)
            "Event",
            "set",
            "wait",
        }
    ),
}
"""Every method each scanned driver module may call. A new call needs a line here: review it."""


def python_files() -> Iterator[Path]:
    for root in SCANNED:
        if root.is_dir():
            yield from sorted(root.rglob("*.py"))


# -- import boundary --------------------------------------------------------------------------


IMPORT_FUNCTIONS = frozenset({"import_module", "__import__"})


def patchright_imports(source: str) -> list[str]:
    found: list[str] = []
    tree = ast.parse(source)
    importers = set(IMPORT_FUNCTIONS)  # plus their aliases
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"importlib", "builtins"}:
            importers |= {a.asname or a.name for a in node.names if a.name in IMPORT_FUNCTIONS}
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] == "patchright"]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "patchright":
                found.append(node.module or "")
        elif isinstance(node, ast.Call):
            found += _import_call(node, importers)
        elif isinstance(node, ast.Name | ast.Attribute) and id(node) not in called:
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in importers and isinstance(node.ctx, ast.Load):
                found.append("<import function passed around>")
    return found


def _import_call(node: ast.Call, importers: set[str]) -> list[str]:
    name = _callee(node)
    if name == "getattr" and len(node.args) >= 2:
        second = node.args[1]
        if isinstance(second, ast.Constant) and second.value in IMPORT_FUNCTIONS:
            return ["<getattr import>"]
    if name not in importers or not node.args:
        return []
    first = node.args[0]
    if isinstance(first, ast.Constant):
        return [str(first.value)] if str(first.value).startswith("patchright") else []
    return ["<dynamic import>"]


def _callee(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_only_the_driver_module_imports_patchright() -> None:
    offenders = {}
    for path in python_files():
        imports = patchright_imports(path.read_text(encoding="utf-8"))
        if imports and path != DRIVER:
            offenders[str(path.relative_to(REPO))] = imports
    assert not offenders
    assert patchright_imports(DRIVER.read_text(encoding="utf-8"))  # the check can see it


@pytest.mark.parametrize(
    "snippet",
    [
        "import patchright",
        "import patchright.async_api as pw",
        "from patchright.async_api import async_playwright",
        "import importlib\nimportlib.import_module('patchright.async_api')",
        "__import__('patchright')",
        "import importlib\nname = 'patch' + 'right'\nimportlib.import_module(name)",
        "from importlib import import_module as im\nim('patchright')",
        "import importlib as il\nil.import_module('patchright.async_api')",
        "import builtins\ngetattr(builtins, '__import__')('patchright')",
        "import importlib\nload = importlib.import_module\nload('patchright')",
        "loader = __import__\nloader('patchright')",
    ],
)
def test_import_check_sees_every_form(snippet: str) -> None:
    assert patchright_imports(snippet)


def test_import_check_leaves_other_imports_alone() -> None:
    assert patchright_imports("from importlib.metadata import version\nversion('x')") == []
    assert patchright_imports("import importlib\nimportlib.import_module('json')") == []


# -- calls in the driver module ---------------------------------------------------------------


def receiver_names(node: ast.expr) -> list[str] | None:
    """``self._page.mouse`` → ``["self", "_page", "mouse"]``; ``None`` for anything else."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return list(reversed(parts))


def violations(source: str, module: str = "_patchright.py") -> list[str]:
    problems: list[str] = []
    tree = ast.parse(source)
    allowed = MODULE_CALLS[module]
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            problems += _attribute_problems(node, module)
        if not isinstance(node, ast.Call):
            continue
        problems += _call_problems(node, module)
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr not in allowed:
            problems.append(f"line {node.lineno}: .{func.attr}() is not on the call allow-list")
    return problems


def _attribute_problems(node: ast.Attribute, module: str) -> list[str]:
    forbidden = node.attr in FORBIDDEN_ATTRIBUTES or node.attr.startswith(FORBIDDEN_PREFIXES)
    if not forbidden:
        return []
    names = receiver_names(node)
    if names is not None and (module, ".".join(names)) in ATTRIBUTE_EXCEPTIONS:
        return []
    return [f"line {node.lineno}: .{node.attr} is never used"]


def _call_problems(node: ast.Call, module: str = "_patchright.py") -> list[str]:
    problems: list[str] = []
    where = f"line {node.lineno}"
    func = node.func
    problems.extend(
        f"{where}: isolated_context must be the literal True"
        for keyword in node.keywords
        if keyword.arg == "isolated_context"
        and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is True)
    )
    if isinstance(func, ast.Name):
        if func.id in DYNAMIC:
            problems.append(f"{where}: {func.id}() is not allowed")
        if func.id == "getattr" and (
            len(node.args) < 2 or not isinstance(node.args[1], ast.Constant)
        ):
            problems.append(f"{where}: getattr needs a literal attribute name")
        return problems
    if not isinstance(func, ast.Attribute):
        return problems
    method = func.attr
    if method in {"methodcaller", "attrgetter"}:
        problems.append(f"{where}: operator.{method} is not allowed")
    if method in EVALUATE_FAMILY and not any(
        k.arg == "isolated_context" and isinstance(k.value, ast.Constant) and k.value.value is True
        for k in node.keywords
    ):
        problems.append(f"{where}: {method} without isolated_context=True")
    if method in ACTING and not (module == "cdp.py" and method == "send"):
        problems.append(f"{where}: .{method}() acts on the page")
    names = receiver_names(func.value)
    if names is None:
        if method not in UNNAMED_OK:
            problems.append(f"{where}: .{method}() on an unnamed receiver")
        return problems
    browser_calls = BROWSER_CALLS | BROWSER_EXTRA.get(module, frozenset())
    if any(BROWSER_RECEIVER.search(n) for n in names) and method not in browser_calls:
        problems.append(f"{where}: .{method}() is not on the browser allow-list")
    return problems


@pytest.mark.parametrize("module", sorted(MODULE_CALLS))
def test_driver_modules_call_only_allow_listed_methods(module: str) -> None:
    assert violations((DRIVER_DIR / module).read_text(encoding="utf-8"), module) == []


def test_every_driver_module_that_touches_browser_objects_is_scanned() -> None:
    touching = {
        path.name
        for path in DRIVER_DIR.glob("*.py")
        if re.search(r"\b(page|context|response|request|session|cdp)\b", path.read_text())
    }
    assert touching <= set(MODULE_CALLS) | {"__init__.py", "launch.py", "policy.py"}


def test_driver_module_really_uses_the_browser() -> None:
    """The allow-list check is not vacuous: the driver makes these calls."""
    tree = ast.parse(DRIVER.read_text(encoding="utf-8"))
    called = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert {"goto", "move", "wheel", "cookies", "launch_persistent_context"} <= called


@pytest.mark.parametrize(
    "snippet",
    [
        "async def f(page):\n    await page.click('a')",
        "async def f(self):\n    await self._page.fill('#x', 'y')",
        "async def f(self):\n    await self._page.keyboard.press('Enter')",
        "async def f(page):\n    await page.evaluate('1')",
        "async def f(page):\n    await page.evaluate('1', isolated_context=False)",
        "async def f(page, flag):\n    await page.evaluate('1', isolated_context=flag)",
        "async def f(context):\n    await context.add_init_script('x')",
        "async def f(context):\n    await context.route('**', None)",
        "async def f(context):\n    await context.add_cookies([])",
        "async def f(page):\n    await page.mouse.down()",
        "async def f(page):\n    await page.reload()",
        "async def f(page):\n    await page.bring_to_front()",
        "async def f(self):\n    await self._cdp.send_raw('Runtime.evaluate')",
        "async def f(page, name):\n    await getattr(page, name)()",
        "def f(page):\n    setattr(page, 'x', 1)",
        "async def f(page):\n    await (await page.frame()).click()",
        "import operator\nasync def f(page):\n    operator.methodcaller('click')(page)",
        "def f(page):\n    eval('page.click()')",
        # aliases: the receiver's name no longer says "page"
        "async def f(context, url):\n    api = context.request\n    await api.get(url)",
        "async def f(self):\n    tab = self._page\n    await tab.wait_for_function('1')",
        (
            "async def f(self):\n    tab = self._page\n    loc = tab.locator('a')\n"
            "    await loc.scroll_into_view_if_needed()"
        ),
        "async def f(resp):\n    fr = resp.frame\n    await fr.wait_for_function('1')",
        "async def f(self):\n    tab = self._page\n    await tab.bring_to_front()",
        "async def f(self):\n    t = self._page\n    async with t.expect_popup():\n        x = 1",
        "async def f(self):\n    w = self._page.main_frame\n    await w.goto('x')",
    ],
)
def test_checker_catches_acting_code(snippet: str) -> None:
    assert violations(snippet)


@pytest.mark.parametrize(
    ("snippet", "module"),
    [
        ("async def f(response):\n    return response.request", "capture.py"),
        ("async def f(self, m):\n    await self._session.send(m)", "cdp.py"),
    ],
)
def test_the_written_out_exceptions_hold_only_where_written(snippet: str, module: str) -> None:
    assert violations(snippet, module) == []
    assert violations(snippet, "_patchright.py")


def test_checker_allows_plain_code() -> None:
    ok = (
        "async def f(self, page, url):\n"
        "    await page.goto(url, timeout=1)\n"
        "    await page.mouse.move(1, 2, steps=3)\n"
        "    await page.mouse.wheel(0, 100)\n"
        "    log.info('x %s', url)\n"
        "    getattr(page, 'url')\n"
        "    await page.evaluate('1', isolated_context=True)\n"
    )
    # Only the evaluate is refused (thrice over): the driver never runs script in the page.
    assert violations(ok) == [
        "line 7: .evaluate() acts on the page",
        "line 7: .evaluate() is not on the browser allow-list",
        "line 7: .evaluate() is not on the call allow-list",
    ]
