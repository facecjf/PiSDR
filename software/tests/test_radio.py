"""Receiver back ends: view / retune maths, the spectrum tap, and the GNU Radio wiring.

The wiring tests run against ``tests/fakegr.py`` (see its docstring): no samples flow, but
every connection, lock and device open is checked.
"""
from __future__ import annotations

import gc
import os
import stat
import wave

import numpy as np
import pytest

import fakegr
from conftest import needs_fake_gr, needs_real_gr, pump
from pisdr import config
from pisdr.radio import tuners
from pisdr.radio.backend_sim import SimBackend
from pisdr.state import RadioState

SPAN = float(config.SAMPLE_RATE)


def write_wav(path, seconds: float = 0.5, rate: int = config.AUDIO_RATE, width: int = 2) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        tone = (np.sin(np.arange(int(seconds * rate)) * 0.1) * 8000).astype("<i2")
        wav.writeframes(tone.tobytes() if width == 2 else bytes(len(tone)))
    return str(path)


# --------------------------------------------------------------------------- simulator / view maths
def _sim(state, lineup=None):
    backend = SimBackend(state, reason="--sim requested", lineup=lineup)
    backend.frames = []
    backend.views = []
    backend.messages = []
    backend.spectrum_ready.connect(backend.frames.append)
    backend.center_changed.connect(lambda c, s: backend.views.append((c, s)))
    backend.status.connect(backend.messages.append)
    backend.start()
    backend._timer.stop()                 # frames on demand via _tick()
    yield backend
    backend.stop()


@pytest.fixture
def sim(state):
    """The simulator as it comes: a V3 (main tuner) and an XTR."""
    yield from _sim(state)


@pytest.fixture
def sim_one(state):
    """The simulator with a single dongle."""
    yield from _sim(state, tuners.Lineup())


def assert_view_ok(backend, state) -> None:
    lo, hi = backend.view_center_hz - backend.view_span_hz / 2, backend.view_center_hz + backend.view_span_hz / 2
    assert lo <= state.vfo_hz <= hi, "VFO is outside the displayed window"
    assert backend.center_hz - SPAN / 2 - 1 <= lo and hi <= backend.center_hz + SPAN / 2 + 1, \
        "window reaches outside what the dongle is receiving"
    assert backend.view_span_hz == pytest.approx(SPAN / state.spectrum_zoom)
    assert backend.center_hz - SPAN / 2 >= 0


@pytest.mark.parametrize("zoom", config.SPECTRUM_ZOOMS)
def test_vfo_stays_in_view_while_tuning_at_every_zoom(sim, state, zoom):
    state.cycle_spectrum_zoom(config.SPECTRUM_ZOOMS.index(zoom))
    rng = np.random.default_rng(zoom)
    state.set_step_hz(5000)
    for _ in range(300):
        state.tune_steps(int(rng.integers(-40, 41)))
        assert_view_ok(sim, state)
    for hz in (433_920_000, config.FREQ_MIN, 30_000_000, config.FREQ_MAX):
        state.set_vfo(hz)                       # a jump: the dongle retunes
        assert_view_ok(sim, state)
        assert abs(state.vfo_hz - sim.center_hz) == pytest.approx(0.25 * SPAN), "not parked clear of the DC spike"


def test_small_tuning_steps_do_not_retune_the_dongle(sim, state):
    """Retuning the hardware clicks the audio, so it must only happen near the span edge."""
    state.set_vfo(146_000_000)
    centre = sim.center_hz
    state.set_step_hz(5000)
    for _ in range(20):
        state.tune_steps(1)
    assert sim.center_hz == centre
    state.set_vfo(state.vfo_hz + 0.8 * SPAN)
    assert sim.center_hz != centre


def test_view_only_announced_when_it_moves(sim, state):
    state.set_vfo(146_000_000)
    del sim.views[:]
    state.tune_steps(1)
    assert sim.views == []                      # x1: the window did not move
    state.cycle_spectrum_zoom(3)
    assert len(sim.views) == 1 and sim.views[0][1] == pytest.approx(SPAN / 8)


@pytest.mark.parametrize("zoom", config.SPECTRUM_ZOOMS)
def test_simulator_frames_are_complete_at_every_zoom(sim, state, zoom):
    state.cycle_spectrum_zoom(config.SPECTRUM_ZOOMS.index(zoom))
    sim._tick()
    frame = sim.frames[-1]
    assert frame.shape == (config.FFT_SIZE,) and frame.dtype == np.float32
    assert np.isfinite(frame).all()
    assert -140 < state.signal_db < 0


def test_simulator_fakes_playback_decoders_and_network_mode(sim, state, tmp_path, qapp):
    assert sim.play_file(str(tmp_path / "missing.wav")).startswith("Cannot open")
    assert "16-bit" in sim.play_file(write_wav(tmp_path / "8k.wav", rate=8000))
    assert sim.play_file(write_wav(tmp_path / "ok.wav")) is None
    assert state.playing.endswith("ok.wav")
    sim.playback_finished.emit()
    assert state.playing == ""

    state.set_decoder("APRS")
    sim._emit_decoder_line()
    assert state.decoder_lines and state.decoder_lines[0].startswith("AFSK1200")
    state.set_decoder("OFF")
    assert state.decoder_lines == []

    assert sim.start_network_mode() is None and state.network_mode
    state.set_recording(True)
    assert not state.recording
    assert sim.stop_network_mode() is None and not state.network_mode


def test_simulated_morse_reaches_the_cw_panel(sim, state, qapp):
    sim.cw_text.connect(state.append_cw_text)
    state.set_mode("CW")
    sim._cw_last_t -= 12.0                      # pretend 12 s of keying went by
    sim._tick()
    pump(qapp, 0.05)
    assert "PISDR" in state.cw_text
    assert 15 <= state.cw_wpm <= 21


# --------------------------------------------------------------------------- spectrum tap
def tone(offset_hz: float, n: int) -> np.ndarray:
    t = np.arange(n) / config.SAMPLE_RATE
    return (0.5 * np.exp(2j * np.pi * offset_hz * t)).astype(np.complex64)


def show(tap, samples: np.ndarray) -> None:
    """Leave in the tap's probe what GNU Radio's FFT chain would for these samples."""
    frame = samples[-tap.size:] * np.blackman(tap.size).astype(np.float32)
    tap.probe.value = (np.abs(np.fft.fftshift(np.fft.fft(frame))) ** 2).astype(np.float32).tolist()


def display(backend, samples: np.ndarray) -> None:
    """Spectra of *samples* through the back end, fetched the way its poll timer does."""
    show(backend.spectrum, samples * 0.5)       # (the first one after a change of view is dropped)
    backend._poll_spectrum()
    show(backend.spectrum, samples)
    backend._poll_spectrum()


def _tap(fps: int = 20):
    from gnuradio import blocks, gr
    from pisdr.radio.spectrum_tap import SpectrumTap
    tb = gr.top_block("tap")
    source = blocks.null_source(gr.sizeof_gr_complex)
    return SpectrumTap(tb, source, config.FFT_SIZE, config.SAMPLE_RATE, config.SPECTRUM_ZOOMS, fps), tb, source


@needs_fake_gr
def test_spectrum_tap_puts_a_tone_in_the_right_bin():
    tap, _tb, _source = _tap()
    assert tap.read() is None, "nothing has been transformed yet"
    show(tap, tone(-100_000, 4096))
    assert tap.read() is None, "the first vector is partly from before the tap was switched on"
    show(tap, tone(+300_000, 8192))
    frame = tap.read()
    assert frame.shape == (config.FFT_SIZE,) and frame.dtype == np.float32
    expected = config.FFT_SIZE / 2 + 300_000 / (SPAN / config.FFT_SIZE)
    assert abs(int(np.argmax(frame)) - expected) <= 1
    assert frame.max() == pytest.approx(20 * np.log10(0.5), abs=1.5)       # dBFS, window-corrected
    assert np.median(frame) < frame.max() - 60
    assert tap.read() is None, "the same spectrum must not be drawn twice"


@needs_fake_gr
def test_spectrum_tap_shows_the_window_asked_for_at_every_zoom():
    tap, _tb, _source = _tap()
    for port, zoom in list(enumerate(config.SPECTRUM_ZOOMS))[1:]:
        size = config.FFT_SIZE * zoom
        tone_bin = size / 2 - 150_000 / (SPAN / size)
        start = int(tone_bin) - 100
        tap.set_view(size, start)
        assert tap.size == size and tap.selector.last("set_output_index") == (port,)
        show(tap, tone(0, size))
        assert tap.read() is None                       # dropped: the chain has just been switched to
        show(tap, tone(-150_000, size))
        frame = tap.read()
        assert frame.shape == (config.FFT_SIZE,)
        assert abs(int(np.argmax(frame)) - 100) <= 1, f"x{zoom}"
        tap.set_view(size, start - 50)                  # the window slides: same chain, nothing dropped
        show(tap, tone(-150_000, size) * 0.9)
        assert abs(int(np.argmax(tap.read())) - 150) <= 1, f"x{zoom}"
    tap.set_view(config.FFT_SIZE, 500)                  # x1 has only one window
    show(tap, tone(0, 1024))
    tap.read()
    show(tap, tone(150_000, 1024))
    assert abs(int(np.argmax(tap.read())) - (512 + 80)) <= 1


@needs_fake_gr
def test_spectrum_tap_switched_off_drops_the_samples_and_shows_nothing_stale():
    tap, _tb, _source = _tap()
    show(tap, tone(0, 1024))
    tap.read()
    tap.set_enabled(False)
    assert tap.selector.enabled is False, "the samples must be dropped before any FFT"
    show(tap, tone(50_000, 1024))
    assert tap.read() is None
    tap.set_enabled(True)
    assert tap.selector.enabled is True
    assert tap.read() is None, "that spectrum is from before it was switched off"
    show(tap, tone(60_000, 1024))
    assert tap.read() is None                           # partly old samples
    show(tap, tone(70_000, 1024))
    assert tap.read() is not None


@needs_fake_gr
def test_spectrum_tap_thins_the_vectors_out_before_the_fft():
    tap, tb, _source = _tap(fps=20)
    by_size = {chain.size: chain for chain in tap._chains.values()}
    assert sorted(by_size) == [config.FFT_SIZE * zoom for zoom in config.SPECTRUM_ZOOMS]
    assert by_size[1024].keep.last("set_n") == (46,)    # 1875 vectors/s -> about 40 FFTs/s
    assert by_size[65536].keep.last("set_n") == (1,)    # 29 vectors/s: all of them
    tap.set_fps(5)
    assert by_size[1024].keep.last("set_n") == (187,) and by_size[65536].keep.last("set_n") == (2,)
    for chain in by_size.values():                      # selector -> vector -> keep -> FFT -> power -> probe
        block, steps = tap.selector, 0
        while block is not chain.probe:
            (block,) = [b for e in tb.edges if e[0] is block and (block is not tap.selector or e[1] == chain.port)
                        for b in [e[2]]]
            steps += 1
        assert steps == 5 and tb.sinks_of(chain.probe) == []


@needs_real_gr
def test_spectrum_tap_on_real_gnuradio_and_a_busy_gui_does_not_hold_the_samples_up():
    """The reason the tap exists. A Python sink in its place let 60-80 % through here."""
    import time
    from gnuradio import analog, blocks, gr
    from pisdr.radio.spectrum_tap import SpectrumTap
    tb = gr.top_block("tap")
    carrier = analog.sig_source_c(config.SAMPLE_RATE, analog.GR_COS_WAVE, 300_000, 0.5)
    noise = analog.noise_source_c(analog.GR_GAUSSIAN, 1e-4)      # as from a dongle: no two spectra alike
    source = blocks.add_cc()
    throttle = blocks.throttle(gr.sizeof_gr_complex, config.SAMPLE_RATE)
    tb.connect(carrier, (source, 0))
    tb.connect(noise, (source, 1))
    tb.connect(source, throttle)
    tb.connect(throttle, blocks.null_sink(gr.sizeof_gr_complex))        # stands for the receiver
    tap = SpectrumTap(tb, throttle, config.FFT_SIZE, config.SAMPLE_RATE, config.SPECTRUM_ZOOMS, 20)
    tb.start()
    try:
        for zoom in (1, 8):
            size = config.FFT_SIZE * zoom
            tone_bin = size / 2 + 300_000 / (SPAN / size)
            tap.set_view(size, int(tone_bin) - 200)
            frames, end = [], time.monotonic() + 1.0
            while time.monotonic() < end:
                frame = tap.read()
                if frame is not None:
                    frames.append(frame)
                time.sleep(0.05)
            assert len(frames) >= 10, f"x{zoom}: {len(frames)} spectra in a second"
            shown = tap._start + int(np.argmax(frames[-1]))
            assert abs(shown - tone_bin) <= 1, f"x{zoom}"
            assert frames[-1].max() == pytest.approx(20 * np.log10(0.5), abs=1.5)
        before, start, busy = throttle.nitems_written(0), time.monotonic(), 0
        while time.monotonic() - start < 2.0:           # a GUI thread that never lets go of Python
            for i in range(20_000):
                busy += i * i
            tap.read()
        passed = (throttle.nitems_written(0) - before) / (time.monotonic() - start) / config.SAMPLE_RATE
        assert passed > 0.95, f"only {passed:.0%} of the samples got through"
    finally:
        tb.stop()
        tb.wait()


# --------------------------------------------------------------------------- GNU Radio wiring
pytestmark_gr = needs_fake_gr


def _radio(state, monkeypatch, lineup=None):
    from pisdr.radio import flowgraph
    monkeypatch.setattr(flowgraph.time, "sleep", lambda seconds: None)
    backend = flowgraph.GnuRadioBackend(state, lineup=lineup)
    backend.messages = []
    backend.status.connect(backend.messages.append)
    backend.start()
    backend._poll.stop()
    yield backend
    backend.stop()


@pytest.fixture
def radio(state, monkeypatch):
    """The GNU Radio back end on one dongle of no particular kind."""
    yield from _radio(state, monkeypatch)


# The dongles the other way round from the simulator's, so that nothing can lean on "main is #0".
TWO_DONGLES = tuners.Lineup(tuners.Dongle(1, tuners.V3, "RTLSDRBlog", "Blog V3", "00000001"),
                            tuners.Dongle(0, tuners.XTR, "Nooelec", "SMArt XTR v5", "00000001"))


@pytest.fixture
def radio2(state, monkeypatch):
    """The GNU Radio back end on the real radio's pair: V3 (device 1, main) and XTR (device 0)."""
    yield from _radio(state, monkeypatch, TWO_DONGLES)


def open_sources() -> int:
    gc.collect()
    return len(fakegr.OPEN_SOURCES)


def audio_path(backend) -> list:
    """Blocks from the audio filters to the sound card, following the single path."""
    path = [backend.audio_filters]
    while path[-1] is not backend.audio_sink:
        nxt = [b for b in backend.tb.sinks_of(path[-1])
               if b not in (backend.rec_gate, backend.audio_null)]
        assert len(nxt) == 1, f"{path[-1]} feeds {nxt}"
        path.append(nxt[0])
    return path


@needs_fake_gr
def test_every_mode_has_a_demodulator_between_the_selectors(radio, state):
    tb = radio.tb
    assert tb.running and tb.lock_depth == 0
    for mode, index in radio._mode_index.items():
        assert tb.has_edge((radio.sel_in, index), radio.demods[mode]), mode
        assert tb.has_edge(radio.demods[mode], (radio.sel_out, index)), mode
        state.set_mode(mode)
        assert radio.sel_in.last("set_output_index") == (index,)
        assert radio.sel_out.last("set_input_index") == (index,)
        assert radio.cw_sink.enabled is (mode == "CW")
    assert len({id(d) for d in radio.demods.values()}) == len(config.MODES)


@needs_fake_gr
def test_default_audio_path_has_no_python_blocks(radio):
    """NR / NB are Python and hold the GIL; they must stay out unless switched on."""
    assert audio_path(radio) == [radio.audio_filters, radio.audio_bus, radio.volume, radio.audio_sink]
    assert radio.tb.has_edge(radio.audio_bus, radio.rec_gate)        # recording tap is before volume
    assert radio.rec_gate.enabled is False and radio.dec_gate.enabled is False


@needs_fake_gr
def test_noise_blocks_are_spliced_in_and_out_under_lock(radio, state):
    locks = radio.tb.locks
    state.set_dsp_nr_level(5)
    assert audio_path(radio)[:3] == [radio.audio_filters, radio.nr, radio.audio_bus]
    state.set_dsp_nb_level(3)
    assert audio_path(radio)[:4] == [radio.audio_filters, radio.nb, radio.nr, radio.audio_bus]
    state.set_dsp_nr_level(7)                                        # level change: no rewiring
    assert radio.tb.locks == locks + 2
    state.set_dsp_nr_level(0)
    state.set_dsp_nb_level(0)
    assert audio_path(radio)[:2] == [radio.audio_filters, radio.audio_bus]
    assert radio.tb.lock_depth == 0 and radio.tb.running


@needs_fake_gr
def test_settings_reach_the_hardware(radio, state):
    src = radio.src
    state.set_vfo(433_920_000)
    centre = src.last("set_center_freq")[0]
    assert abs(433_920_000 - centre) == pytest.approx(0.25 * SPAN)
    assert radio.xlate.last("set_center_freq") == (433_920_000 - centre,)
    state.set_gain(42)
    assert src.last("set_gain") == (42.0, 0) and src.last("set_gain_mode") == (False, 0)
    state.set_agc(True)
    assert src.last("set_gain_mode") == (True, 0)
    state.set_ppm(-3)
    assert src.last("set_freq_corr") == (-3, 0)
    state.set_volume(50)
    assert radio.volume.last("set_k") == (0.25,)
    state.set_cw_pitch(600)
    assert radio.cw_xlate.last("set_center_freq") == (600,)


@needs_fake_gr
def test_spectrum_shows_a_signal_at_the_frequency_it_is_on(radio, state, qapp):
    """End to end: a carrier on the VFO frequency must be drawn at the VFO, at every zoom."""
    frames: list[np.ndarray] = []
    radio.spectrum_ready.connect(frames.append)
    state.set_vfo(146_520_000)
    for zoom_index, zoom in enumerate(config.SPECTRUM_ZOOMS[:5]):          # up to a 16k FFT
        state.cycle_spectrum_zoom(zoom_index - config.SPECTRUM_ZOOMS.index(state.spectrum_zoom))
        size = config.FFT_SIZE * zoom
        assert radio.spectrum.size == size
        display(radio, tone(state.vfo_hz - radio.center_hz, 2 * size))
        frame = frames[-1]
        bin_hz = radio.view_span_hz / config.FFT_SIZE
        shown_at = radio.view_center_hz - radio.view_span_hz / 2 + (np.argmax(frame) + 0.5) * bin_hz
        assert abs(shown_at - state.vfo_hz) <= bin_hz, f"x{zoom}: drawn {shown_at - state.vfo_hz:+.0f} Hz off"


@needs_fake_gr
def test_nothing_in_python_reads_the_sample_stream(radio, state):
    """A Python block there needs the interpreter lock per chunk, and a busy GUI then starves the audio."""
    from gnuradio import gr
    fed_by_the_dongle = radio.tb.sinks_of(radio.src)
    assert set(fed_by_the_dongle) == {radio.xlate, radio.spectrum.selector}
    for block in (radio.xlate, radio.probe, radio.sel_in, *radio.tb.sinks_of(radio.spectrum.selector)):
        assert not isinstance(block, gr.sync_block), block


@needs_fake_gr
def test_spectrum_is_fetched_by_a_timer_only_while_it_is_shown(radio, state, qapp):
    frames: list[np.ndarray] = []
    radio.spectrum_ready.connect(frames.append)
    assert radio._spectrum_poll.isActive() and radio._spectrum_poll.interval() == 1000 // state.fft_fps
    state.set_fft_fps(40)
    assert radio._spectrum_poll.interval() == 25
    display(radio, tone(0, 1024))
    assert len(frames) >= 1
    radio.set_spectrum_enabled(False)                   # another page, or the aircraft map
    assert not radio._spectrum_poll.isActive() and radio.spectrum.selector.enabled is False
    del frames[:]
    display(radio, tone(10_000, 1024))
    assert frames == []
    state.set_bias_tee(True)                            # the graph is rebuilt meanwhile: still off
    assert not radio._spectrum_poll.isActive() and radio.spectrum.selector.enabled is False
    radio.set_spectrum_enabled(True)
    assert radio._spectrum_poll.isActive() and radio._spectrum_poll.interval() == 25
    display(radio, tone(20_000, 1024))
    assert len(frames) >= 1
    assert radio.start_network_mode() is not None or not radio._spectrum_poll.isActive()
    radio.stop()
    assert not radio._spectrum_poll.isActive()


@needs_fake_gr
def test_recording_goes_to_the_recordings_folder(radio, state):
    state.set_recording(True)
    assert state.recording and radio.rec_gate.enabled
    path = radio.wav.path
    assert os.path.dirname(path) == str(config.RECORDINGS_DIR) and path.endswith(f"{state.vfo_hz}_{state.mode}.wav")
    assert config.RECORDINGS_DIR.is_dir()
    state.set_recording(False)
    assert not radio.rec_gate.enabled and radio.wav.path is None
    assert radio.messages[-1].startswith("Saved pisdr_")


@needs_fake_gr
def test_recording_that_cannot_be_created_is_reported(radio, state, monkeypatch):
    monkeypatch.setattr(fakegr, "WAV_OPEN_OK", False)
    state.set_recording(True)
    assert state.recording is False, "REC shown although nothing is being written"
    assert radio.rec_gate.enabled is False
    assert any(m.startswith("Cannot record") for m in radio.messages)


@needs_fake_gr
def test_closing_the_radio_finalises_a_recording(radio, state):
    state.set_recording(True)
    wav = radio.wav
    radio.stop()
    assert wav.path is None and wav.last("close") == ()


@needs_fake_gr
def test_playback_replaces_the_radio_audio_and_restores_it(radio, state, tmp_path):
    first, second = write_wav(tmp_path / "a.wav"), write_wav(tmp_path / "b.wav")
    tb = radio.tb
    assert radio.play_file(first) is None
    player = radio.player
    assert tb.sources_of(radio.audio_bus) == [player]
    assert tb.has_edge(radio.audio_filters, radio.audio_null)        # radio audio parked, not blocked

    state.set_dsp_nr_level(4)                                        # DSP change while playing
    assert tb.has_edge(radio.nr, radio.audio_null) and tb.sources_of(radio.audio_bus) == [player]

    assert radio.play_file(second) is None                           # next file without stopping
    assert tb.sources_of(radio.audio_bus) == [radio.player] and radio.player is not player
    assert player._wav is None, "first file left open"

    radio.playback_finished.emit()
    assert state.playing == "" and radio.player is None
    assert audio_path(radio)[:3] == [radio.audio_filters, radio.nr, radio.audio_bus]
    assert tb.sinks_of(radio.nr) == [radio.audio_bus]
    assert tb.lock_depth == 0


@needs_fake_gr
def test_playback_refuses_files_it_cannot_play(radio, state, tmp_path):
    assert "16-bit" in radio.play_file(write_wav(tmp_path / "8bit.wav", width=1))
    (tmp_path / "junk.wav").write_bytes(b"not a wav file")
    assert radio.play_file(str(tmp_path / "junk.wav")).startswith("Cannot open")
    assert state.playing == "" and radio.player is None
    assert audio_path(radio)[1] is radio.audio_bus


@needs_fake_gr
def test_wav_player_plays_once_then_reports_the_end(tmp_path):
    from pisdr.radio.gr_extras import WavPlayer
    done: list[bool] = []
    player = WavPlayer(write_wav(tmp_path / "a.wav", seconds=0.1), lambda: done.append(True))
    out = np.zeros(4096, dtype=np.float32)
    played = 0
    while not done and played < 100_000:
        player.work([], [out])
        played += len(out)
    assert done == [True] and 4800 <= played <= 4800 + 4096
    assert np.abs(out[-100:]).max() == 0.0                           # silence after the end
    player.work([], [out])
    assert done == [True] and np.abs(out).max() == 0.0


@needs_fake_gr
def test_bias_tee_reopens_the_dongle_with_the_flag(radio, state):
    state.tune_to(137_100_000, "NFM", 5000)
    state.set_gain(38)
    old = radio.src
    assert "bias=1" not in old.kwargs["args"]
    state.set_bias_tee(True)
    assert "bias=1" in radio.src.kwargs["args"] and radio.src is not old
    del old
    assert open_sources() == 1, "the old device handle was never released"
    assert len(fakegr.OPEN_SINKS) == 1
    # Everything is applied again to the new device.
    assert radio.src.last("set_gain") == (38.0, 0)
    assert abs(radio.src.last("set_center_freq")[0] - 137_100_000) == pytest.approx(0.25 * SPAN)
    assert radio.sel_in.last("set_output_index") == (radio._mode_index["NFM"],)
    assert radio.tb.running
    state.set_bias_tee(False)
    assert "bias=1" not in radio.src.kwargs["args"] and open_sources() == 1


@needs_fake_gr
def test_bias_tee_change_ends_recording_and_playback_cleanly(radio, state, tmp_path):
    state.set_recording(True)
    state.set_bias_tee(True)
    assert not state.recording
    assert radio.play_file(write_wav(tmp_path / "a.wav")) is None
    state.set_bias_tee(False)
    assert state.playing == "", "top bar still says PLAY after the graph was rebuilt"
    assert audio_path(radio)[1] is radio.audio_bus


@needs_fake_gr
def test_radio_that_will_not_reopen_does_not_crash_the_ui(radio, state, monkeypatch):
    import osmosdr

    def busy(*args, **kwargs):
        raise RuntimeError("usb_claim_interface error -6")
    monkeypatch.setattr(osmosdr, "source", busy)
    state.set_bias_tee(True)
    assert radio.radio_down
    assert radio.messages[-1].startswith("Radio did not reopen: usb_claim_interface")
    # The user keeps pressing things; nothing may raise.
    state.set_vfo(100_000_000)
    state.set_mode("AM")
    state.set_volume(10)
    state.set_dsp_nr_level(3)
    state.set_recording(True)
    radio.set_spectrum_enabled(False)
    radio.stop_playback()
    radio.stop()


def fake_rtl_tcp(tmp_path, monkeypatch, body: str) -> None:
    script = tmp_path / "bin" / "rtl_tcp"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")


@needs_fake_gr
def test_network_mode_hands_the_dongle_over_and_takes_it_back(radio, state, tmp_path, monkeypatch):
    log = tmp_path / "args.txt"
    fake_rtl_tcp(tmp_path, monkeypatch, f'[ "$1" = "-h" ] && {{ echo "usage: -T bias"; exit 0; }}\n'
                                        f'echo "$@" > {log}\nexec sleep 30\n')
    state.set_ppm(2)
    state.set_dsp_nr_level(3)
    state.set_recording(True)
    assert radio.start_network_mode() is None
    assert state.network_mode and not state.recording
    assert open_sources() == 0, "dongle still open: rtl_tcp could not have claimed it"
    assert radio.rtl_tcp_alive
    assert log.read_text().split() == ["-a", "0.0.0.0", "-p", str(config.RTL_TCP_PORT), "-P", "2"]
    state.set_vfo(7_000_000)                    # ignored while the dongle is away; must not raise
    state.set_volume(20)

    proc = radio._rtl_tcp
    assert radio.stop_network_mode() is None
    assert not state.network_mode and proc.poll() is not None
    assert open_sources() == 1 and radio.tb.running
    assert audio_path(radio)[:3] == [radio.audio_filters, radio.nr, radio.audio_bus]
    assert radio.volume.last("set_k") == (0.2 ** 2,)
    assert radio.src.last("set_freq_corr") == (2, 0)


@needs_fake_gr
def test_network_mode_falls_back_when_rtl_tcp_dies(radio, state, tmp_path, monkeypatch):
    fake_rtl_tcp(tmp_path, monkeypatch, 'echo "usb_claim_interface error -6" >&2\nexit 1\n')
    error = radio.start_network_mode()
    assert error == "rtl_tcp exited: usb_claim_interface error -6"
    assert not state.network_mode and not radio.radio_down
    assert open_sources() == 1 and radio.tb.running
    state.set_vfo(7_000_000)
    assert radio.src.last("set_center_freq") is not None


@needs_fake_gr
def test_network_mode_without_rtl_tcp_leaves_the_radio_alone(radio, state, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    src = radio.src
    assert "not found" in radio.start_network_mode()
    assert radio.src is src and radio.tb.running and not state.network_mode


@needs_fake_gr
def test_smeter_reads_the_probe_with_calibration(radio, state):
    radio.probe.value = 1e-3                    # -30 dBFS
    radio._poll_level()
    assert state.signal_db == pytest.approx(-30 + state.smeter_cal_db)
    state.set_smeter_cal(state.smeter_cal_db + 10)
    radio._poll_level()
    assert state.signal_db == pytest.approx(-30 + state.smeter_cal_db)
    radio.probe.value = 0.0                     # silence: no log(0)
    radio._poll_level()
    assert np.isfinite(state.signal_db)


@needs_fake_gr
def test_decoder_feed_is_gated_and_converts_to_16_bit(radio, state):
    assert radio.dec_gate.enabled is False
    got: list[bytes] = []
    from pisdr.radio.gr_extras import DecoderFeed
    feed_block = DecoderFeed(got.append)
    assert feed_block.work([np.array([0.0, 0.5, -0.5, 9.0], dtype=np.float32)], []) == 4
    assert np.frombuffer(got[0], "<i2").tolist() == [0, 10000, -10000, 32767]
    state.set_decoder("APRS")                   # multimon-ng is not installed here
    if not radio.multimon.available():
        assert state.decoder == "OFF" and radio.dec_gate.enabled is False
        assert "multimon-ng is not installed" in radio.messages[-1]


def test_make_backend_falls_back_to_the_simulator(state, monkeypatch):
    from pisdr.radio import backend as backend_mod
    assert isinstance(backend_mod.make_backend(state, force_sim=True), SimBackend)
    if fakegr.is_fake():
        import osmosdr

        def no_dongle(*args, **kwargs):
            raise RuntimeError("Failed to open rtlsdr device #0")
        monkeypatch.setattr(osmosdr, "source", no_dongle)
        fallback = backend_mod.make_backend(RadioState())
        assert isinstance(fallback, SimBackend) and "Failed to open rtlsdr" in fallback.reason
