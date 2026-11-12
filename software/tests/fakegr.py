"""A stand-in for GNU Radio and gr-osmosdr, so the radio wiring can be tested on any PC.

No samples flow. What the fake does do is the part bugs hide in:

* ``top_block.connect`` / ``disconnect`` keep the set of edges, and disconnecting an edge
  that is not there fails the way real GNU Radio does;
* ``lock`` / ``unlock`` must balance, and reconfiguring a running graph outside a lock fails;
* every open ``osmosdr.source`` and ``audio.sink`` is counted, so a test can prove the
  dongle was really released (bias tee, network mode);
* every call on a block is recorded in ``block.calls``.

Blocks subclassing ``gr.sync_block`` (FFT sink, CW envelope sink, WAV player...) are the
project's own Python and run for real: call their ``work`` directly.
"""
from __future__ import annotations

import sys
import types
import weakref

OPEN_SOURCES: "weakref.WeakSet" = weakref.WeakSet()
OPEN_SINKS: "weakref.WeakSet" = weakref.WeakSet()
WAV_OPEN_OK = True          # set False to simulate a recording that cannot be created


def _endpoint(item):
    """(block, port) for either a block or a (block, port) tuple."""
    return (item[0], int(item[1])) if isinstance(item, tuple) else (item, 0)


class Block:
    """Generic block: any method call is accepted and recorded."""

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.calls: list[tuple] = []
        self.enabled = True

    def __getattr__(self, name: str):
        if name.startswith("_") or name in ("args", "kwargs", "calls", "enabled"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return method

    def set_enabled(self, on) -> None:
        self.enabled = bool(on)
        self.calls.append(("set_enabled", (on,), {}))

    def last(self, name: str):
        """Arguments of the most recent call to *name*, or None."""
        for call in reversed(self.calls):
            if call[0] == name:
                return call[1]
        return None

    def __repr__(self) -> str:
        return f"<fake {type(self).__name__}>"


class _Graph(Block):
    """Shared connect / disconnect bookkeeping for top_block and hier_block2."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.edges: set[tuple] = set()
        self.msg_edges: list[tuple] = []

    def connect(self, *items) -> None:
        self._check_reconfigure()
        for a, b in zip(items, items[1:]):
            src, dst = _endpoint(a), _endpoint(b)
            edge = (src[0], src[1], dst[0], dst[1])
            if edge in self.edges:
                raise RuntimeError(f"already connected: {src} -> {dst}")
            if any(e[2] is dst[0] and e[3] == dst[1] for e in self.edges) and dst[0] is not self:
                raise RuntimeError(f"input {dst} already has a source")
            self.edges.add(edge)

    def disconnect(self, *items) -> None:
        self._check_reconfigure()
        for a, b in zip(items, items[1:]):
            src, dst = _endpoint(a), _endpoint(b)
            edge = (src[0], src[1], dst[0], dst[1])
            if edge not in self.edges:
                raise RuntimeError(f"not connected: {src} -> {dst}")
            self.edges.discard(edge)

    def msg_connect(self, src, dst) -> None:
        self.msg_edges.append((src, dst))

    def _check_reconfigure(self) -> None:
        pass

    def has_edge(self, a, b) -> bool:
        src, dst = _endpoint(a), _endpoint(b)
        return (src[0], src[1], dst[0], dst[1]) in self.edges

    def sources_of(self, block) -> list:
        return [e[0] for e in self.edges if e[2] is block]

    def sinks_of(self, block) -> list:
        return [e[2] for e in self.edges if e[0] is block]


class top_block(_Graph):  # noqa: N801 (GNU Radio's name)
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.running = False
        self.lock_depth = 0
        self.locks = 0

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def wait(self) -> None:
        pass

    def lock(self) -> None:
        self.lock_depth += 1
        self.locks += 1

    def unlock(self) -> None:
        if self.lock_depth <= 0:
            raise RuntimeError("unlock without lock")
        self.lock_depth -= 1

    def _check_reconfigure(self) -> None:
        if self.running and self.lock_depth == 0:
            raise RuntimeError("flowgraph changed while running without lock()")


class hier_block2(_Graph):  # noqa: N801
    def __init__(self, name="", in_sig=None, out_sig=None) -> None:
        super().__init__(name, in_sig, out_sig)


class sync_block:  # noqa: N801
    """Base for the project's own Python blocks; their real code runs in tests."""

    def __init__(self, name="", in_sig=None, out_sig=None) -> None:
        self._name = name

    def message_port_register_in(self, port) -> None:
        pass

    def set_msg_handler(self, port, handler) -> None:
        self._msg_handler = handler


basic_block = sync_block


class _Probe(Block):
    value = 1e-6

    def level(self) -> float:
        return self.value


class _WavSink(Block):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.path: str | None = None

    def open(self, path: str) -> bool:
        self.calls.append(("open", (path,), {}))
        if not WAV_OPEN_OK:
            return False
        self.path = path
        return True

    def close(self) -> None:
        self.calls.append(("close", (), {}))
        self.path = None


class _Source(Block):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        OPEN_SOURCES.add(self)

    def get_sample_rate(self) -> float:
        rate = self.last("set_sample_rate")
        return float(rate[0]) if rate else 0.0


class _AudioSink(Block):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        OPEN_SINKS.add(self)


class _Firdes:
    """Filter designers return a short tap list; the values don't matter here."""

    def __getattr__(self, name: str):
        return lambda *args, **kwargs: [0.25, 0.5, 0.25]


class _Module(types.ModuleType):
    """Module whose every unknown attribute is a :class:`Block` subclass of that name."""

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        cls = type(name, (Block,), {})
        setattr(self, name, cls)
        return cls


def install() -> None:
    """Register the fake modules. Call before anything imports ``pisdr.radio``."""
    if "gnuradio" in sys.modules and not getattr(sys.modules["gnuradio"], "IS_FAKE", False):
        raise RuntimeError("real GNU Radio is already imported")
    root = _Module("gnuradio")
    root.IS_FAKE = True
    root.__path__ = []          # looks like a package

    gr = _Module("gnuradio.gr")
    gr.top_block, gr.hier_block2 = top_block, hier_block2
    gr.sync_block, gr.basic_block = sync_block, basic_block
    gr.io_signature = lambda *args: ("io_signature", *args)
    gr.sizeof_gr_complex, gr.sizeof_float, gr.sizeof_char = 8, 4, 1
    gr.version = lambda: "fake"

    blocks = _Module("gnuradio.blocks")
    blocks.wavfile_sink = _WavSink
    analog = _Module("gnuradio.analog")
    analog.probe_avg_mag_sqrd_c = _Probe
    audio = _Module("gnuradio.audio")
    audio.sink = _AudioSink
    digital = _Module("gnuradio.digital")
    flt = _Module("gnuradio.filter")
    flt.__path__ = []
    firdes = _Firdes()
    flt.firdes = firdes
    firdes_mod = types.ModuleType("gnuradio.filter.firdes")
    firdes_mod.__getattr__ = lambda name: getattr(firdes, name)   # type: ignore[attr-defined]

    osmosdr = _Module("osmosdr")
    osmosdr.source = _Source
    osmosdr.IS_FAKE = True

    for name, module in (("gr", gr), ("blocks", blocks), ("analog", analog), ("audio", audio),
                         ("digital", digital), ("filter", flt)):
        setattr(root, name, module)
        sys.modules[f"gnuradio.{name}"] = module
    sys.modules["gnuradio"] = root
    sys.modules["gnuradio.filter.firdes"] = firdes_mod
    sys.modules["osmosdr"] = osmosdr


def is_fake() -> bool:
    return getattr(sys.modules.get("gnuradio"), "IS_FAKE", False)
