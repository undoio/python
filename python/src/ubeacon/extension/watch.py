"""
Python expression watchpoints via chained hardware watchpoints.

Parses a Python expression like ``kennel[0].dogs`` into a chain of steps
(name lookup, subscript, attribute access), resolves the memory addresses
where each intermediate ``PyObject*`` pointer is stored in the debuggee,
and sets hardware watchpoints so that UDB stops when any link in the chain
changes.
"""

from __future__ import annotations

import ast

import gdb  # pyright: ignore[reportMissingModuleSource]
from src.udbpy import report  # pyright: ignore[reportMissingModuleSource]

from . import debuggee, ubeacon


# ---------------------------------------------------------------------------
# Expression parser
# ---------------------------------------------------------------------------

def parse_expression(expr: str) -> list[dict]:
    """
    Parse a Python expression into a list of chain steps.

    Supported forms:
      - Simple name: ``x``        -> ``[{"type": "name", "name": "x"}]``
      - Subscript:   ``x[0]``     -> ``[..., {"type": "index", "index": 0}]``
      - Attribute:   ``x.y``      -> ``[..., {"type": "attr", "name": "y"}]``
      - Chained:     ``a[0].b.c`` -> name + index + attr + attr

    Raises :class:`report.ReportableError` for unsupported expressions.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise report.ReportableError(f"Invalid expression: {exc}") from exc

    steps: list[dict] = []
    _walk(tree.body, steps)
    return steps


def _walk(node: ast.expr, steps: list[dict]) -> None:
    """Recursively decompose an AST node into chain steps (outermost first)."""
    if isinstance(node, ast.Name):
        steps.append({"type": "name", "name": node.id})
    elif isinstance(node, ast.Attribute):
        _walk(node.value, steps)
        steps.append({"type": "attr", "name": node.attr})
    elif isinstance(node, ast.Subscript):
        _walk(node.value, steps)
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
            steps.append({"type": "index", "index": node.slice.value})
        elif isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            steps.append({"type": "key", "key": node.slice.value})
        else:
            raise report.ReportableError(
                "Only integer and string literal subscripts are supported "
                "in watch expressions."
            )
    else:
        raise report.ReportableError(
            f"Unsupported expression node: {type(node).__name__}. "
            "Only names, integer subscripts, and attribute access are supported."
        )


# ---------------------------------------------------------------------------
# Hardware watchpoint
# ---------------------------------------------------------------------------

class _HwWatchpoint(gdb.Breakpoint):
    """A single hardware watchpoint that delegates to its owning PythonWatch."""

    def __init__(self, addr: int, owner: PythonWatch) -> None:
        super().__init__(
            f"*(void **){hex(addr)}",
            type=gdb.BP_WATCHPOINT,
            wp_class=gdb.WP_WRITE,
            internal=True,
        )
        self.silent = True
        self._owner = owner

    def stop(self) -> bool:
        """Called by GDB when this watchpoint fires.

        We cannot safely call into the debuggee from here, so we just
        set a flag.  The caller is responsible for calling
        :func:`evaluate_pending` once ``gdb.execute`` has returned.
        """
        self._owner._pending_report = True
        return True


# ---------------------------------------------------------------------------
# Watchpoint manager
# ---------------------------------------------------------------------------

class PythonWatch:
    """Manages the hardware watchpoints for a single Python watch expression."""

    INDEX = 1

    def __init__(self, expr: str, chain: ubeacon.WatchChain) -> None:
        self.expr = expr
        self.chain = chain
        self.index = PythonWatch.INDEX
        PythonWatch.INDEX += 1

        self._watchpoints: list[_HwWatchpoint] = []
        self._prev_value: str | None = None
        self._pending_report: bool = False

    def install(self) -> None:
        """Set hardware watchpoints on every resolved storage and guard address."""
        self._remove_watchpoints()
        self._prev_value = self._evaluate_safe()

        for link in self.chain.links:
            if link.storage_addr is not None:
                self._add_watchpoint(link.storage_addr)
            if link.guard_addr is not None:
                self._add_watchpoint(link.guard_addr)

    def remove(self) -> None:
        """Delete all hardware watchpoints owned by this watch."""
        self._remove_watchpoints()

    # -- internal ------------------------------------------------------------

    def _add_watchpoint(self, addr: int) -> None:
        try:
            self._watchpoints.append(_HwWatchpoint(addr, self))
        except gdb.error:
            pass

    def _remove_watchpoints(self) -> None:
        for wp in self._watchpoints:
            try:
                wp.delete()
            except Exception:
                pass
        self._watchpoints.clear()

    def _do_report(self) -> bool:
        """Evaluate the expression and report if the value changed."""
        new_value = self._evaluate_safe()
        if new_value is None:
            return False
        if new_value != self._prev_value:
            report.user(
                f"Python watchpoint {self.index}: {self.expr}\n"
                f"  Old value: {self._prev_value}\n"
                f"  New value: {new_value}"
            )
            self._prev_value = new_value
            return True
        return False

    def _evaluate_safe(self) -> str | None:
        """Evaluate the full expression, returning None on error."""
        try:
            with debuggee.disable_volatile_warning_maybe():
                result = ubeacon.evaluate(self.expr)
            if result.startswith("Python error:"):
                return None
            return result
        except Exception:
            return None

    @property
    def display(self) -> str:
        return f"  {self.index}: {self.expr} = {self._prev_value}"


# Module-level state.
watches: list[PythonWatch] = []


def add_watch(expr: str) -> PythonWatch:
    """Parse an expression, resolve its chain, install watchpoints, and register it."""
    steps = parse_expression(expr)
    chain = ubeacon.WatchChain.from_gdb(steps)
    pw = PythonWatch(expr, chain)
    pw.install()
    watches.append(pw)
    return pw


def remove_watch(num: int) -> None:
    """Remove a watch by its user-visible index (0 = remove all)."""
    if num == 0:
        for w in watches:
            w.remove()
        watches.clear()
    else:
        target = None
        for w in watches:
            if w.index == num:
                target = w
                break
        if target is None:
            raise report.ReportableError(f"No Python watchpoint number {num}.")
        target.remove()
        watches.remove(target)


def any_pending() -> bool:
    """Return True if any watch had a hardware watchpoint fire."""
    return any(w._pending_report for w in watches)


def evaluate_pending() -> bool:
    """Evaluate watches whose hardware watchpoints fired.

    Returns True if any watched expression actually changed value.
    Must be called **after** ``gdb.execute`` has returned (not from
    inside a stop handler) so that ``evaluate()`` is safe to call.
    """
    changed = False
    for w in watches:
        if w._pending_report:
            w._pending_report = False
            if w._do_report():
                changed = True
    return changed


def report_pending() -> None:
    """Evaluate and report all watches that may have changed.

    Called from the ``internal_breakpoint`` finally block (after
    ``gdb.execute`` has returned).  Handles both hardware-watchpoint-
    triggered watches and the evaluate-and-compare fallback.
    """
    for w in watches:
        w._pending_report = False
        w._do_report()
