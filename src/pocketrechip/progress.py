"""Progress bars that also work in logs (stderr not a TTY, e.g. `docker logs`)."""

import io
import logging
import sys

from tqdm import tqdm

LOG_INTERVAL = 30.0
LOG_FORMAT = "%(asctime)s %(name)s: %(message)s"


class LineStream(io.TextIOBase):
    """Stream giving each tqdm refresh its own line instead of a carriage return."""

    def __init__(self, stream):
        super().__init__()
        self.stream = stream

    def write(self, s: str) -> int:
        line = s.strip("\r\n ")
        if line:
            self.stream.write(line + "\n")
            self.stream.flush()
        return len(s)


def progress_bar(**kw) -> tqdm:
    """tqdm on stderr: unchanged on a TTY, otherwise a line per LOG_INTERVAL and at the end."""
    if sys.stderr.isatty():
        return tqdm(file=sys.stderr, **kw)
    kw.update(leave=True, disable=False, mininterval=LOG_INTERVAL)
    return tqdm(file=LineStream(sys.stderr), **kw)


def setup_logging() -> None:
    """INFO lines on stderr when it is not a TTY; the bars carry progress on a TTY."""
    logging.basicConfig(
        level=logging.WARNING if sys.stderr.isatty() else logging.INFO,
        format=LOG_FORMAT,
        stream=sys.stderr,
    )
