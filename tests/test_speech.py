"""Speech pipeline tests: phoneme timing, viseme mapping, the phrase cache,
emote triggers and the Warudo message format.

None of these need Kokoro installed -- they drive the modules with synthetic
Speech objects, so they run on a machine with no GPU.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from narrator.avatar.emotes import EmoteDirector
from narrator.avatar.warudo import WarudoBridge
from narrator.config import Config
from narrator.speech import phonemes as ph
from narrator.speech import visemes as vis
from narrator.speech.engine import PhraseCache, SilentEngine, Speech
from narrator.ui.webui import WebUI

T0 = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


@dataclass
class FakeToken:
    phonemes: str
    start_ts: float
    end_ts: float


@dataclass
class FakeSpeech:
    duration: float
    phonemes: str = ""
    tokens: list = field(default_factory=list)
    spans: list = field(default_factory=list)
    timing: str = "unknown"


# ---------------------------------------------------------------------------
# Phoneme timing
# ---------------------------------------------------------------------------


def test_vowels_are_recognised():
    assert ph.is_vowel("ɑ")
    assert ph.is_vowel("i")
    assert ph.is_vowel("ə")
    assert not ph.is_vowel("t")
    assert not ph.is_vowel("k")


def test_length_marks_stay_attached_to_their_vowel():
    assert ph.split_phonemes("ɔːl") == ["ɔː", "l"]
    assert ph.split_phonemes("ˈɡold") == ["ˈɡ", "o", "l", "d"]


def test_proportional_timing_covers_the_whole_utterance():
    spans = ph.proportional("ɡold", 2.0)
    assert spans
    assert spans[0].start == 0.0
    assert spans[-1].end == pytest.approx(2.0)
    for earlier, later in itertools.pairwise(spans):
        assert earlier.end == pytest.approx(later.start)


def test_proportional_timing_gives_vowels_more_room():
    spans = {s.phoneme: s.duration for s in ph.proportional("ɡa", 1.0)}
    assert spans["a"] == pytest.approx(spans["ɡ"] * ph.VOWEL_WEIGHT, rel=1e-6)


def test_token_timestamps_are_used_when_present():
    speech = FakeSpeech(
        duration=1.0,
        tokens=[FakeToken("ɡo", 0.0, 0.5), FakeToken("ld", 0.5, 1.0)],
    )
    spans = ph.extract(speech)
    assert ph.timing_mode() == "timestamps"
    assert spans[0].start == 0.0
    assert spans[-1].end == pytest.approx(1.0)


def test_nonsense_timestamps_are_rejected_for_the_fallback():
    """Timestamps in milliseconds would put a 1s utterance at 1000s."""
    speech = FakeSpeech(
        duration=1.0,
        phonemes="ɡold",
        tokens=[FakeToken("ɡo", 0.0, 500.0), FakeToken("ld", 500.0, 1000.0)],
    )
    spans = ph.extract(speech)
    assert spans
    assert spans[-1].end == pytest.approx(1.0)
    assert ph.timing_mode() == "proportional"


def test_chunk_timestamps_are_shifted_onto_the_utterance_timeline():
    """Kokoro splits long text and times every chunk from zero.

    Concatenating those tokens unshifted piles all the chunks onto the first
    one: the mouth moves through the opening seconds of a long line and then
    sits shut for the rest. It does not look like a timing bug when you watch
    it -- it looks like the avatar gave up.
    """
    chunk = [FakeToken("ɡo", 0.0, 1.0), FakeToken("ld", 1.0, 2.0)]

    first = ph.spans_from_tokens(chunk, 2.0, offset=0.0)
    second = ph.spans_from_tokens(chunk, 2.0, offset=2.0)
    third = ph.spans_from_tokens(chunk, 2.0, offset=4.0)
    spans = first + second + third

    assert spans[0].start == pytest.approx(0.0)
    assert spans[-1].end == pytest.approx(6.0), "should cover the whole utterance"
    for earlier, later in itertools.pairwise(spans):
        assert later.start >= earlier.start, "time went backwards between chunks"


def test_an_unshifted_second_chunk_would_leave_the_mouth_shut():
    """The shape of the bug, pinned so it cannot come back quietly."""
    chunk = [FakeToken("ɡo", 0.0, 1.0), FakeToken("ld", 1.0, 2.0)]
    unshifted = ph.spans_from_tokens(chunk, 2.0) + ph.spans_from_tokens(chunk, 2.0)

    # Everything crowds into the first half; nothing drives the second.
    assert max(s.end for s in unshifted) == pytest.approx(2.0)

    shifted = ph.spans_from_tokens(chunk, 2.0) + ph.spans_from_tokens(
        chunk, 2.0, offset=2.0
    )
    assert max(s.end for s in shifted) == pytest.approx(4.0)


def test_the_cache_version_invalidates_entries_with_stale_spans():
    """A cache entry carries phoneme spans, not just a waveform.

    When the span maths is fixed, every existing entry is wrong -- and a stale
    span file drives a mouth that stops halfway through the sentence, which is
    a miserable thing to debug a second time.
    """
    from narrator.speech import engine as engine_module

    cache = PhraseCache(Path("unused"), enabled=False)
    key_now = cache.key("Gold moved.", "am_michael", 1.0)

    original = engine_module.CACHE_VERSION
    try:
        engine_module.CACHE_VERSION = original + 1
        assert cache.key("Gold moved.", "am_michael", 1.0) != key_now
    finally:
        engine_module.CACHE_VERSION = original


def test_cached_spans_short_circuit_extraction():
    """A cache hit must keep the timing worked out at synthesis time."""
    cached = [ph.PhonemeSpan("a", 0.0, 1.0)]
    speech = FakeSpeech(duration=1.0, spans=cached, timing="timestamps")
    assert ph.extract(speech) == cached
    assert ph.timing_mode() == "timestamps"


def test_from_text_gives_the_silent_engine_something_to_move_to():
    spans = ph.from_text("Gold's at thirty-three forty-one.", 2.0)
    assert spans
    assert spans[-1].end == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Visemes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phoneme,expected",
    [
        ("ɑ", "aa"),
        ("æ", "aa"),
        ("ʌ", "aa"),
        ("a", "aa"),
        ("i", "ee"),
        ("ɪ", "ee"),
        ("ɛ", "ee"),
        ("u", "ou"),
        ("ʊ", "ou"),
        ("o", "oh"),
        ("ɔ", "oh"),
        ("ə", "ih"),
        ("ɜ", "ih"),
        ("ɚ", "ih"),
    ],
)
def test_vowel_to_viseme_mapping(phoneme, expected):
    assert vis.viseme_for_vowel(phoneme) == expected


def test_vowels_open_by_their_own_width_and_consonants_lean_on_the_next_vowel():
    spans = [
        ph.PhonemeSpan("t", 0.0, 0.1),
        ph.PhonemeSpan("i", 0.1, 0.3),
    ]
    targets = vis.targets(spans)
    consonant = targets[0][2]
    vowel = targets[1][2]
    assert vowel["ee"] == pytest.approx(vis.VISEME_OPENNESS["ee"] * vis.UNSTRESSED_SCALE)
    assert consonant["ee"] == pytest.approx(vis.CONSONANT_WEIGHT)
    assert sum(consonant.values()) == pytest.approx(vis.CONSONANT_WEIGHT)


def test_bilabials_shut_the_mouth_completely():
    """/p/ /b/ /m/ are made by the lips meeting. Anything above zero here is a
    "problem" pronounced with the mouth hanging open, which reads as dubbing
    faster than any other error in lip sync."""
    for consonant in ("p", "b", "m"):
        spans = [
            ph.PhonemeSpan("ɑ", 0.0, 0.1),
            ph.PhonemeSpan(consonant, 0.1, 0.16),
            ph.PhonemeSpan("ɑ", 0.16, 0.3),
        ]
        closed = vis.targets(spans)[1][2]
        assert sum(closed.values()) == 0.0, f"/{consonant}/ left the mouth open"


def test_open_vowels_open_wider_than_close_ones():
    """A jaw-drop "aa" is not the same size as a spread "ee". Driving every
    vowel to the same weight is the classic puppet look."""
    widths = {}
    for phoneme, viseme in (
        ("ɑ", "aa"),
        ("o", "oh"),
        ("u", "ou"),
        ("i", "ee"),
        ("ə", "ih"),
    ):
        weights = vis.targets([ph.PhonemeSpan(phoneme, 0.0, 0.2)])[0][2]
        widths[viseme] = weights[viseme]
    assert widths["aa"] > widths["oh"] > widths["ou"] > widths["ee"] > widths["ih"]


def test_stress_makes_a_syllable_bigger():
    stressed = vis.targets([ph.PhonemeSpan("ˈɑ", 0.0, 0.2)])[0][2]["aa"]
    secondary = vis.targets([ph.PhonemeSpan("ˌɑ", 0.0, 0.2)])[0][2]["aa"]
    reduced = vis.targets([ph.PhonemeSpan("ɑ", 0.0, 0.2)])[0][2]["aa"]
    assert stressed > secondary > reduced


def test_a_consonant_takes_its_shape_from_both_neighbours():
    """The same /t/ between two different vowels is not the same shape. Fixed
    per-consonant shapes are what make cheap lip sync buzz."""
    spans = [
        ph.PhonemeSpan("o", 0.0, 0.1),
        ph.PhonemeSpan("t", 0.1, 0.16),
        ph.PhonemeSpan("i", 0.16, 0.3),
    ]
    blended = vis.targets(spans)[1][2]
    assert blended["ee"] > 0.0 and blended["oh"] > 0.0
    assert blended["ee"] > blended["oh"], "the vowel ahead should dominate"
    assert sum(blended.values()) == pytest.approx(vis.CONSONANT_WEIGHT)


def test_the_mouth_arrives_before_the_sound():
    """Articulation leads audio. With a 100ms lead the mouth is already moving
    for a vowel that has not been heard yet."""
    spans = [ph.PhonemeSpan(" ", 0.0, 0.3), ph.PhonemeSpan("ɑ", 0.3, 0.6)]
    early = vis.stream(spans, 0.6, fps=60, lead=0.1)
    late = vis.stream(spans, 0.6, fps=60, lead=0.0)

    def first_open(frames):
        return next(f.t for f in frames if f.open_amount > 0.05)

    assert first_open(early) < first_open(late)


@pytest.mark.asyncio
async def test_the_bridge_notices_warudo_going_away():
    """Warudo restarting must end the wait, so the connect loop can retry.

    The original code awaited only the shutdown event, so a Warudo restart
    left the bridge parked inside a closed socket: the narrator kept talking
    and the avatar silently stopped listening until the whole process was
    restarted. Nothing in the transcript or the status bar showed it.
    """

    class ClosingSocket:
        def __init__(self) -> None:
            self.closed = asyncio.Event()

        async def wait_closed(self) -> None:
            await self.closed.wait()

    bridge = WarudoBridge(Config())
    socket = ClosingSocket()

    waiting = asyncio.ensure_future(bridge._until_closed(socket))
    await asyncio.sleep(0)
    assert not waiting.done(), "returned before anything happened"

    socket.closed.set()  # Warudo goes away
    await asyncio.wait_for(waiting, timeout=1.0)


@pytest.mark.asyncio
async def test_the_bridge_wait_also_ends_on_shutdown():
    class OpenSocket:
        async def wait_closed(self) -> None:
            await asyncio.Event().wait()  # never

    bridge = WarudoBridge(Config())
    waiting = asyncio.ensure_future(bridge._until_closed(OpenSocket()))
    await asyncio.sleep(0)
    bridge._stopping.set()
    await asyncio.wait_for(waiting, timeout=1.0)


def test_the_framing_control_orbits_the_camera_around_the_face():
    """Drag yaw, and the camera swings round the character without losing it.

    The camera always looks back at the focus point, so no amount of dragging
    can leave the avatar off-screen -- which is the failure mode of a free
    camera and the reason this is an orbit.
    """
    cfg = Config()
    cfg.warudo.camera_focus_height = 1.35
    bridge = WarudoBridge(cfg)
    sent: list[dict] = []
    bridge.send = sent.append  # type: ignore[method-assign]

    bridge.send_camera(yaw=0.0, pitch=0.0, distance=1.0)
    position = next(
        m["data"] for m in sent if m["action"] == cfg.warudo.camera_position_action
    )
    rotation = next(
        m["data"] for m in sent if m["action"] == cfg.warudo.camera_rotation_action
    )

    # Straight in front, at face height, looking back at the character.
    assert position["x"] == pytest.approx(0.0, abs=1e-6)
    assert position["y"] == pytest.approx(1.35)
    assert position["z"] == pytest.approx(1.0)
    assert rotation["y"] == pytest.approx(180.0)

    sent.clear()
    bridge.send_camera(yaw=90.0, pitch=0.0, distance=1.0)
    side = next(
        m["data"] for m in sent if m["action"] == cfg.warudo.camera_position_action
    )
    assert side["x"] == pytest.approx(1.0)
    assert side["z"] == pytest.approx(0.0, abs=1e-6)


def test_raising_the_camera_tilts_it_down_towards_the_face():
    bridge = WarudoBridge(Config())
    sent: list[dict] = []
    bridge.send = sent.append  # type: ignore[method-assign]

    bridge.send_camera(yaw=0.0, pitch=30.0, distance=1.0)
    position = next(m["data"] for m in sent if m["action"] == "cam_pos")
    rotation = next(m["data"] for m in sent if m["action"] == "cam_rot")

    assert position["y"] > bridge.cfg.warudo.camera_focus_height, (
        "should be above the face"
    )
    # Unity's positive X rotation tilts down. Getting this backwards aims the
    # camera at the ceiling and the avatar leaves the frame entirely.
    assert rotation["x"] > 0, "a camera above the face must tilt down, not up"


def test_only_the_newest_framing_survives():
    """A drag fires a move per pointer event. Queueing them all would leave
    the camera crawling through a backlog after the hand stopped moving."""
    web = WebUI(Config(), run_id="test", symbol="XAUUSD", mode="replay")
    for distance in (1.0, 2.0, 3.0):
        web._queue_camera({"type": "camera", "yaw": 0, "pitch": 0, "distance": distance})

    assert web.pop_camera() == {"yaw": 0.0, "pitch": 0.0, "distance": 3.0}
    assert web.pop_camera() is None


def test_the_avatar_picker_only_accepts_characters_on_the_roster():
    """The source string goes on to Warudo as a property value, so a browser's
    word for it is not something to pass through unchecked."""
    cfg = Config()
    web = WebUI(cfg, run_id="test", symbol="XAUUSD", mode="replay")
    web.avatars = [
        {
            "file": "Shipilka.warudo",
            "source": "character://data/Characters/Shipilka.warudo",
            "label": "Shipilka",
            "gender": "female",
            "focus": 0.92,
            "lipSync": None,
        }
    ]

    web._queue_avatar({"type": "avatar", "file": "../../../etc/passwd"})
    assert web.pop_avatar() is None, "accepted a character that is not on the roster"

    web._queue_avatar({"type": "avatar", "file": "Shipilka.warudo"})
    picked = web.pop_avatar()
    assert picked is not None
    assert picked["source"] == "character://data/Characters/Shipilka.warudo"
    assert picked["focus"] == 0.92


def test_switching_avatar_reframes_the_camera():
    """Models differ in height. Swapping without re-framing leaves the camera
    pointing at an empty room, which looks like a crash."""
    cfg = Config()
    cfg.warudo.camera_focus_height = 1.5
    bridge = WarudoBridge(cfg)
    sent: list[dict] = []
    bridge.send = sent.append  # type: ignore[method-assign]

    bridge.send_avatar("character://data/Characters/Shipilka.warudo", focus_height=0.92)

    assert sent[0]["action"] == cfg.warudo.avatar_action
    # Set Asset Property takes the serialized value, so it arrives quoted.
    assert sent[0]["data"] == '"character://data/Characters/Shipilka.warudo"'
    assert cfg.warudo.camera_focus_height == 0.92


def test_a_malformed_camera_message_is_ignored():
    web = WebUI(Config(), run_id="test", symbol="XAUUSD", mode="replay")
    web._queue_camera({"type": "camera", "yaw": "sideways"})
    web._queue_camera({"type": "camera"})
    assert web.pop_camera() is None


def test_the_avatar_response_curve_lifts_quiet_weights_without_breaking_order():
    """The engine's weights are articulation; gamma is per-avatar calibration.

    A model with weak morphs shows nothing at 0.4, so the quiet end has to be
    lifted -- but a vowel must never overtake a wider one, and a closure must
    stay absolutely shut.
    """
    cfg = Config()
    cfg.warudo.viseme_gamma = 0.55
    bridge = WarudoBridge(cfg)

    assert bridge.shaped(0.0) == 0.0, "a closure must stay shut"
    assert bridge.shaped(1.0) == 1.0, "a full jaw drop must not overshoot"
    assert bridge.shaped(0.45) > 0.45, "the quiet end should be lifted"
    assert bridge.shaped(0.45) < bridge.shaped(0.80) < bridge.shaped(1.0)


def test_the_response_curve_is_off_by_default():
    bridge = WarudoBridge(Config())
    for weight in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert bridge.shaped(weight) == weight


def test_pauses_close_the_mouth():
    targets = vis.targets([ph.PhonemeSpan(" ", 0.0, 0.2)])
    assert sum(targets[0][2].values()) == 0.0


def test_stream_is_sixty_fps_and_ends_closed():
    spans = ph.proportional("ɡold", 1.0)
    frames = vis.stream(spans, 1.0, fps=60)
    # 1s plus the release tail, at 60fps.
    assert 60 <= len(frames) <= 72
    assert frames[-1].open_amount == 0.0
    assert set(frames[0].weights) == set(vis.VISEMES)


def test_stream_smooths_instead_of_snapping():
    """A 40ms attack means no single frame may slam from closed to open."""
    spans = [ph.PhonemeSpan("a", 0.0, 0.5), ph.PhonemeSpan("i", 0.5, 1.0)]
    frames = vis.stream(spans, 1.0, fps=60, attack=0.04, release=0.04)
    for earlier, later in itertools.pairwise(frames):
        for name in vis.VISEMES:
            jump = abs(later.weights[name] - earlier.weights[name])
            assert jump < 0.45, f"{name} jumped {jump:.2f} in one frame"


def test_stream_actually_moves():
    frames = vis.stream(ph.proportional("ɡoldi", 1.0), 1.0, fps=60)
    assert max(f.open_amount for f in frames) > 0.5


def test_viseme_message_shape():
    message = vis.rest_frame().as_message()
    assert message["type"] == "viseme"
    assert set(message) == {"type", *vis.VISEMES}
    assert all(message[v] == 0.0 for v in vis.VISEMES)


# ---------------------------------------------------------------------------
# Phrase cache
# ---------------------------------------------------------------------------


def test_phrase_cache_round_trip(tmp_path):
    pytest.importorskip("numpy")
    import numpy as np

    cache = PhraseCache(tmp_path)
    speech = Speech(
        text="Gold's at thirty-three forty-one twenty.",
        audio=np.zeros(2400, dtype=np.float32),
        sample_rate=24000,
        duration=0.1,
        phonemes="ɡold",
        spans=[ph.PhonemeSpan("ɡ", 0.0, 0.05), ph.PhonemeSpan("o", 0.05, 0.1)],
        timing="timestamps",
    )
    key = cache.key(speech.text, "am_michael", 1.0)
    assert cache.get(key) is None
    cache.put(key, speech)

    restored = cache.get(key)
    assert restored is not None
    assert restored.from_cache
    assert restored.duration == speech.duration
    assert len(restored.audio) == 2400
    # The timing must survive; otherwise every cache hit degrades the mouth.
    assert restored.timing == "timestamps"
    assert [s.phoneme for s in restored.spans] == ["ɡ", "o"]
    assert cache.hit_rate == 0.5


def test_phrase_cache_key_covers_voice_and_speed(tmp_path):
    cache = PhraseCache(tmp_path)
    base = cache.key("hello", "am_michael", 1.0)
    assert base != cache.key("hello", "am_adam", 1.0)
    assert base != cache.key("hello", "am_michael", 1.1)
    assert base != cache.key("hello there", "am_michael", 1.0)


def test_disabled_cache_never_writes(tmp_path):
    cache = PhraseCache(tmp_path, enabled=False)
    assert cache.get(cache.key("x", "v", 1.0)) is None
    assert cache.size() == (0, 0.0)


def test_voice_catalogue_is_sane():
    from narrator.speech.engine import ALL_VOICES, VOICES, lang_code_for

    assert len(ALL_VOICES) == len(set(ALL_VOICES)), "duplicate voice names"
    assert Config().speech.voice in ALL_VOICES, "the default voice must exist"
    for group, names in VOICES.items():
        assert names, f"{group} is empty"
        # The group label must agree with the language the pipeline needs.
        wanted = "b" if group.startswith("British") else "a"
        for name in names:
            assert lang_code_for(name) == wanted, f"{name} in {group}"


def test_switching_voice_updates_the_config():
    import asyncio

    engine = SilentEngine(Config())
    assert asyncio.run(engine.set_voice("am_adam")) is True
    assert engine.cfg.speech.voice == "am_adam"


def test_an_unknown_voice_is_refused():
    import asyncio

    engine = SilentEngine(Config())
    before = engine.cfg.speech.voice
    assert asyncio.run(engine.set_voice("am_notarealvoice")) is False
    assert engine.cfg.speech.voice == before


def test_the_cache_key_separates_voices():
    """Switching voice must not serve the old voice's audio."""
    cache = PhraseCache(Path(), enabled=False)
    text = "Gold's at thirty-three forty-one twenty."
    assert cache.key(text, "am_michael", 1.0) != cache.key(text, "bm_george", 1.0)


def test_silent_engine_still_reports_a_duration():
    import asyncio

    engine = SilentEngine(Config())
    speech = asyncio.run(engine.synthesize("Gold's at thirty-three forty-one twenty."))
    assert speech.audio is None
    assert speech.duration > 0
    assert not speech.has_audio


# ---------------------------------------------------------------------------
# Two mouths on one connection
# ---------------------------------------------------------------------------


def _sent(bridge):
    """Drain the bridge's outbound queue."""
    out = []
    while not bridge._queue.empty():
        out.append(bridge._queue.get_nowait())
    return out


def test_visemes_go_to_the_first_character_by_default():
    bridge = WarudoBridge(Config())
    bridge.send_viseme(vis.VisemeFrame(t=0.0, weights={"aa": 0.8}))
    assert [m["action"] for m in _sent(bridge)] == ["viseme_aa"]


def test_speaking_as_the_second_host_switches_the_prefix():
    bridge = WarudoBridge(Config())
    bridge.speak_as(1)
    _sent(bridge)  # the handover closes the first mouth; not what we assert on
    bridge.send_viseme(vis.VisemeFrame(t=0.0, weights={"aa": 0.8}))
    assert [m["action"] for m in _sent(bridge)] == ["viseme2_aa"]


def test_handing_over_closes_the_mouth_being_left():
    """Otherwise the previous character's face freezes mid-vowel."""
    bridge = WarudoBridge(Config())
    bridge.send_viseme(vis.VisemeFrame(t=0.0, weights={"aa": 0.9}))
    _sent(bridge)
    bridge.speak_as(1)
    closing = _sent(bridge)
    assert all(m["action"].startswith("viseme_") for m in closing)
    assert all(m["data"] == 0.0 for m in closing)


def test_one_characters_weights_do_not_suppress_the_others():
    """The skip-unchanged cache is per character, or the second mouth stays shut."""
    bridge = WarudoBridge(Config())
    frame = vis.VisemeFrame(t=0.0, weights={"aa": 0.8})
    bridge.send_viseme(frame)
    _sent(bridge)
    bridge.speak_as(1)
    _sent(bridge)
    bridge.send_viseme(frame)
    assert [m["action"] for m in _sent(bridge)] == ["viseme2_aa"]


def test_speaking_as_the_same_host_twice_sends_nothing():
    bridge = WarudoBridge(Config())
    bridge.speak_as(0)
    assert _sent(bridge) == []


def test_visemes_are_counted_as_frames_not_emotes():
    """The status line read '0 frames' through a whole stream of working lip
    sync, because the counter tested for a "type" key these messages have
    never had. A diagnostic that lies is worse than no diagnostic."""
    bridge = WarudoBridge(Config())
    bridge.send_viseme(vis.VisemeFrame(t=0.0, weights={"aa": 0.8}))
    bridge.speak_as(1)
    bridge.send_viseme(vis.VisemeFrame(t=0.0, weights={"aa": 0.8}))
    messages = _sent(bridge)
    assert messages, "precondition: something was queued"
    assert all(bridge.is_viseme(m) for m in messages), (
        "both characters' visemes must count as frames -- the second host "
        "speaks on viseme2_, which is not a prefix of viseme_"
    )
    assert not bridge.is_viseme({"action": "emote", "data": "Fun"})
    assert not bridge.is_viseme({"action": "cam_pos", "data": {}})


# ---------------------------------------------------------------------------
# Emotes
# ---------------------------------------------------------------------------


def base_facts(**overrides):
    facts = {
        "session": "london",
        "atr_ratio": 1.0,
        "minutes_since_move": 5,
        "bars_in_range": 3,
        "pdh_tested": False,
        "pdl_tested": False,
        "price": 3341.0,
        "asian_high": 3350.0,
        "asian_low": 3330.0,
    }
    facts.update(overrides)
    return facts


def director() -> EmoteDirector:
    cfg = Config()
    d = EmoteDirector(cfg)
    d.evaluate(T0, base_facts())  # establish the baseline
    return d


def test_first_tick_fires_nothing():
    d = EmoteDirector(Config())
    assert d.evaluate(T0, base_facts()) is None


def test_volatility_spike_surprises():
    d = director()
    emote = d.evaluate(T0 + timedelta(seconds=120), base_facts(atr_ratio=2.4))
    assert emote is not None
    assert emote.name == "surprised"


def test_a_fact_staying_high_does_not_refire():
    d = director()
    assert d.evaluate(T0 + timedelta(seconds=120), base_facts(atr_ratio=2.4)) is not None
    later = d.evaluate(T0 + timedelta(seconds=300), base_facts(atr_ratio=2.6))
    assert later is None


def test_level_broken_after_being_untested():
    d = director()
    emote = d.evaluate(T0 + timedelta(seconds=120), base_facts(pdh_tested=True))
    assert emote is not None and emote.name == "surprised"


def test_going_quiet_is_boring():
    d = director()
    emote = d.evaluate(T0 + timedelta(seconds=120), base_facts(minutes_since_move=31))
    assert emote is not None and emote.name == "bored"


def test_new_session_alerts():
    d = director()
    emote = d.evaluate(T0 + timedelta(seconds=120), base_facts(session="london_ny"))
    assert emote is not None and emote.name == "alert"


def test_range_release_excites():
    cfg = Config()
    d = EmoteDirector(cfg)
    d.evaluate(T0, base_facts(bars_in_range=16))
    emote = d.evaluate(
        T0 + timedelta(seconds=120), base_facts(bars_in_range=1, atr_ratio=1.8)
    )
    assert emote is not None and emote.name == "excited"


def test_debounce_stops_a_twitch():
    d = director()
    assert d.evaluate(T0 + timedelta(seconds=120), base_facts(atr_ratio=2.4)) is not None
    # Well inside the 60s debounce, a different trigger still gets held back.
    assert d.evaluate(T0 + timedelta(seconds=130), base_facts(session="newyork")) is None


# ---------------------------------------------------------------------------
# Warudo bridge
# ---------------------------------------------------------------------------


def drain(bridge) -> list[dict]:
    out = []
    while not bridge._queue.empty():
        out.append(bridge._queue.get_nowait())
    return out


def test_every_message_carries_an_action():
    """Warudo discards anything without one -- its log says "Received data
    but action is null". That envelope is the whole protocol."""
    bridge = WarudoBridge(Config(), enabled=True)
    bridge.send_emote("surprised")
    bridge.send_rest()
    messages = drain(bridge)
    assert messages
    for message in messages:
        assert set(message) == {"action", "data"}
        assert isinstance(message["action"], str) and message["action"]


def test_emote_sends_the_name_for_the_configured_vrm_version():
    for style, emote, expected in (
        ("vrm0", "surprised", "Fun"),
        ("vrm1", "excited", "happy"),
        ("name", "bored", "bored"),
    ):
        cfg = Config()
        cfg.warudo.expression_style = style
        bridge = WarudoBridge(cfg, enabled=True)
        bridge.send_emote(emote)
        assert drain(bridge) == [{"action": "emote", "data": expected}]


def test_each_viseme_channel_is_its_own_action():
    """Warudo has no JSON parsing node, so one fat message could not be
    unpacked in a blueprint."""
    bridge = WarudoBridge(Config(), enabled=True)
    bridge.send_viseme(
        vis.VisemeFrame(0.0, {"aa": 0.5, "ee": 0.0, "ih": 0.0, "oh": 0.0, "ou": 0.0})
    )
    assert {m["action"]: m["data"] for m in drain(bridge)} == {
        "viseme_aa": 0.5,
        "viseme_ee": 0.0,
        "viseme_ih": 0.0,
        "viseme_oh": 0.0,
        "viseme_ou": 0.0,
    }


def test_unchanged_channels_are_not_resent():
    """A closed mouth must cost nothing on the wire."""
    bridge = WarudoBridge(Config(), enabled=True)
    frame = vis.rest_frame()
    bridge.send_viseme(frame)
    assert len(drain(bridge)) == 5  # the first frame sets the baseline
    bridge.send_viseme(frame)
    assert drain(bridge) == []  # nothing moved, nothing sent

    moved = vis.VisemeFrame(0.0, {**frame.weights, "ee": 0.9})
    bridge.send_viseme(moved)
    assert drain(bridge) == [{"action": "viseme_ee", "data": 0.9}]


def test_a_change_below_the_threshold_is_suppressed():
    bridge = WarudoBridge(Config(), enabled=True)
    bridge.send_viseme(vis.VisemeFrame(0.0, dict.fromkeys(vis.VISEMES, 0.5)))
    drain(bridge)
    bridge.send_viseme(vis.VisemeFrame(0.0, dict.fromkeys(vis.VISEMES, 0.502)))
    assert drain(bridge) == []


def test_bridge_drops_frames_rather_than_backing_up():
    bridge = WarudoBridge(Config(), enabled=True)
    for index in range(50):
        # Vary the weights, else the change filter suppresses them instead.
        bridge.send_viseme(
            vis.VisemeFrame(0.0, dict.fromkeys(vis.VISEMES, (index % 10) / 10))
        )
    assert bridge._queue.qsize() <= 8
    assert bridge.frames_dropped > 0


def test_disabled_bridge_swallows_everything():
    bridge = WarudoBridge(Config(), enabled=False)
    bridge.send_emote("surprised")
    bridge.send_rest()
    assert bridge._queue.empty()
    assert bridge.status() == "off"
