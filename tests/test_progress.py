"""Progress bars and logging on and off a TTY."""

# pylint: disable=missing-function-docstring

import io
import logging
import sys

import pytest

from pocketrechip import progress as P


class Stderr(io.StringIO):
    """Captured stderr with a chosen isatty()."""

    def __init__(self, tty: bool):
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def test_line_stream_turns_refreshes_into_lines():
    out = io.StringIO()
    s = P.LineStream(out)
    assert s.write("\ra 1/3   ") == 9
    s.write("\r     ")
    s.write("\n")
    s.write("\rb 3/3")
    assert out.getvalue() == "a 1/3\nb 3/3\n"


def test_bar_off_tty_writes_lines(monkeypatch):
    err = Stderr(False)
    monkeypatch.setattr(sys, "stderr", err)
    with P.progress_bar(total=3, desc="hash", leave=False) as b:
        assert (b.mininterval, b.disable) == (P.LOG_INTERVAL, False)
        b.update(3)
    lines = err.getvalue().splitlines()
    assert lines[0].startswith("hash:   0%") and lines[-1].startswith("hash: 100%")
    assert "\r" not in err.getvalue()


def test_bar_on_tty_keeps_tqdm_defaults(monkeypatch):
    err = Stderr(True)
    monkeypatch.setattr(sys, "stderr", err)
    with P.progress_bar(total=1, desc="x") as b:
        assert (b.mininterval, b.disable, b.leave) == (0.1, False, True)
        b.update(1)
    assert "\r" in err.getvalue()


@pytest.mark.parametrize("tty,level", [(True, logging.WARNING), (False, logging.INFO)])
def test_setup_logging_level(monkeypatch, tty, level):
    monkeypatch.setattr(sys, "stderr", Stderr(tty))
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    P.setup_logging()
    assert root.level == level
