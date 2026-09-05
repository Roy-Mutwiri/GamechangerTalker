"""The adapter main.py talks to, and the promise that it cannot break a stream.

Two properties matter more than anything else here and both are tested by
their failure mode rather than their happy path:

**With `[character] enabled = false` nothing is constructed and nothing is
sent.** That is the default, so every existing run has to be untouched by all
of this. `--simulate` being byte-identical is checked separately and by
running it; this checks the object.

**Nothing here reaches `_speak`.** A character that raises costs the stream
the line it was speaking, which is a far worse trade than any nod is worth.
"""

from __future__ import annotations

import asyncio
import socket

import numpy as np
import pytest

from narrator.avatar import livelink
from narrator.avatar.character import Character
from narrator.config import Config
from narrator.script.expression import Beat
from narrator.speech import phonemes

LINE = "Gold just swept the Asian low and now we wait."


def config(**character) -> Config:
    cfg = Config()
    cfg.character.enabled = character.pop("enabled", True)
    cfg.character.unreal.transport = character.pop("transport", "none")
    cfg.character.livelink.port = character.pop("port", 39999)
    for key, value in character.items():
        setattr(cfg.character, key, value)
    return cfg


class Utterance:
    """The three attributes the character reads off a real one."""

    def __init__(self, mood: str = "", beats: list | None = None):
        self.text = LINE
        self.mood = mood
        self.beats = beats or []
        self.stage_index = 0


def spans_for(duration: float = 2.0) -> list:
    return phonemes.from_text(LINE, duration)


# ---------------------------------------------------------------------------
# Off is off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_disabled_character_sends_nothing_and_opens_nothing():
    """The default. Every run that existed before this work package has to be
    exactly as it was, and the cheapest way to be sure is for the object to do
    nothing at all rather than to do nothing carefully."""
    character = Character(config(enabled=False))
    await character.start()
    character.speak_as(1)
    character.begin_utterance(Utterance("happy"), None, 2.0, 0.0, spans=spans_for())
    character.emote("excited", 2.0)
    character.gesture("nod")
    character.set_thinking(True)
    character.set_stage(2)
    character.end_utterance()
    await character.close()

    assert character.status() == "off"
    assert character.sender.stats()["sent"] == 0
    assert character.sender._socket is None


@pytest.mark.asyncio
async def test_the_flag_turns_it_off_even_when_config_turns_it_on():
    character = Character(config(enabled=True), enabled=False)
    await character.start()
    assert character.status() == "off"
    await character.close()


# ---------------------------------------------------------------------------
# One utterance, end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_utterance_produces_a_mouth_a_mood_and_a_beat():
    character = Character(config())
    await character.start()
    try:
        started = 1000.0
        utterance = Utterance("serious", [Beat(name="nod", char_index=20, word_index=5)])
        character.begin_utterance(utterance, None, 2.0, started, spans=spans_for())

        face = character.speaker
        assert face.mouth.speaking
        assert face.mood.name == "serious"

        jaw = livelink.INDEX["JawOpen"]
        brow = livelink.INDEX["BrowDownLeft"]
        frames = np.array(
            [face.frame(i / 60, started + i / 60).copy() for i in range(150)]
        )
        assert frames[:, jaw].max() > 0.2, "the mouth never opened"
        assert frames[:, brow].max() > 0.1, "the mood never rendered"
        # The nod is at word 5 of 9, so somewhere in the middle of the line.
        pitch = frames[:, livelink.INDEX["HeadPitch"]]
        assert pitch.max() > 5.0, "the nod never landed"

        character.end_utterance()
        assert not face.mouth.speaking
    finally:
        await character.close()


@pytest.mark.asyncio
async def test_the_mouth_track_is_as_long_as_the_line():
    character = Character(config())
    await character.start()
    try:
        character.begin_utterance(Utterance(), None, 3.0, 0.0, spans=spans_for(3.0))
        source = character.speaker.mouth.source
        assert source is not None
        # duration + the tail visemes.py adds so the mouth is seen to close.
        assert source.duration == pytest.approx(3.0, abs=0.15)
    finally:
        await character.close()


@pytest.mark.asyncio
async def test_a_line_with_no_spans_still_moves_a_mouth():
    """Kokoro does not always return token timestamps, and a cache hit that
    lost them used to be the common case. Proportional timing is weaker and it
    is not nothing."""
    character = Character(config())
    await character.start()
    try:
        character.begin_utterance(Utterance(), None, 2.0, 0.0, spans=spans_for())
        frames = np.array(
            [character.speaker.frame(i / 60, i / 60).copy() for i in range(120)]
        )
        assert frames[:, livelink.INDEX["JawOpen"]].max() > 0.2
    finally:
        await character.close()


# ---------------------------------------------------------------------------
# Never into _speak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_broken_utterance_is_swallowed():
    """`begin_utterance` is called from `_speak`. Anything it raises costs the
    stream the line being spoken, which is worse than any face."""
    character = Character(config())
    await character.start()
    try:
        character.begin_utterance(object(), None, 2.0, 0.0, spans=[object()])
        character.begin_utterance(Utterance(), None, -1.0, 0.0, spans=None)
    finally:
        await character.close()


@pytest.mark.asyncio
async def test_an_unknown_mood_and_an_unknown_beat_are_counted_not_raised():
    character = Character(config())
    await character.start()
    try:
        character.emote("flabbergasted", 2.0, channel="conversation")
        character.gesture("pirouette")
        assert character.beats_unknown == 1
        assert character.speaker.mood.unknown == 1
    finally:
        await character.close()


@pytest.mark.asyncio
async def test_a_composition_failure_still_sends_a_face():
    """A traceback in one layer is not a reason to tell the audience the thing
    has crashed, and a frozen face is exactly how they would be told."""
    character = Character(config())
    await character.start()
    try:
        broken = next(iter(character.loop.faces))

        class Exploding:
            def frame(self, t, now=None):
                raise RuntimeError("boom")

            def rest(self):
                return livelink.blank()

        character.loop.faces[broken] = Exploding()
        character.loop.tick(0.0)
        assert character.sender.stats()["sent"] == 1
    finally:
        await character.close()


# ---------------------------------------------------------------------------
# Two seats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_occupied_seats_are_streamed():
    """A second Live Link subject that arrives and never does anything shows
    up in Unreal's panel as a source the operator has to explain, and doubles
    the traffic to say nothing."""
    character = Character(config())
    await character.start()
    try:
        assert list(character.loop.faces) == [character.subjects[0]]
        character.set_stage(2)
        assert list(character.loop.faces) == character.subjects
        character.set_stage(1)
        assert list(character.loop.faces) == [character.subjects[0]]
    finally:
        await character.close()


@pytest.mark.asyncio
async def test_the_two_seats_do_not_blink_together():
    """Duplicating one actor and forgetting this is the first thing anybody
    notices about a two-host shot."""
    character = Character(config())
    await character.start()
    try:
        character.set_stage(2)
        left, right = (character.faces[s] for s in character.subjects)
        blink = livelink.INDEX["EyeBlinkLeft"]
        a = np.array([left.frame(i / 30, i / 30).copy()[blink] for i in range(900)])
        b = np.array([right.frame(i / 30, i / 30).copy()[blink] for i in range(900)])
        assert a.max() > 0.9 and b.max() > 0.9
        assert not np.allclose(a, b)
    finally:
        await character.close()


@pytest.mark.asyncio
async def test_switching_seats_closes_the_mouth_it_leaves():
    """Switching mid-line would leave the previous character's mouth frozen
    on whatever syllable it was holding."""
    character = Character(config())
    await character.start()
    try:
        character.set_stage(2)
        character.begin_utterance(Utterance(), None, 2.0, 0.0, spans=spans_for())
        first = character.speaker
        assert first.mouth.speaking
        character.speak_as(1)
        assert not first.mouth.speaking
        assert character.speaker is not first
    finally:
        await character.close()


# ---------------------------------------------------------------------------
# One arbiter for two renderers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_market_emote_loses_to_the_line_being_spoken():
    """The rule avatar/channels.py owns. A level breaking mid-sentence must not
    put a surprised face on a host who is calmly explaining something else."""
    character = Character(config(transport="osc"))
    await character.start()
    try:
        character.emote("serious", hold=5.0, channel="conversation", at=100.0)
        before = character.speaker.mood.changes
        character.emote("surprised", hold=2.0, channel="market", at=101.0)
        assert character.speaker.mood.changes == before, "the market won"
        assert character.speaker.mood.name == "serious"
        assert character.channels.suppressed == 1
    finally:
        await character.close()


@pytest.mark.asyncio
async def test_the_face_and_the_body_get_the_same_answer():
    """Either both or neither. A market emote that reached the body and not
    the face is a character whose posture disagrees with its expression."""
    character = Character(config(transport="osc"))
    await character.start()
    try:
        character.emote("excited", hold=2.0, channel="conversation", at=200.0)
        assert character.speaker.mood.name == "excited"
        body_calls = character.body.stats()["queued"] + character.body.stats()["sent"]

        character.emote("bored", hold=2.0, channel="market", at=200.5)
        assert character.speaker.mood.name == "excited", "face took the market emote"
        after = character.body.stats()["queued"] + character.body.stats()["sent"]
        assert after == body_calls, "body took an emote the face refused"
    finally:
        await character.close()


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_loop_actually_streams_to_a_socket():
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(2.0)
    port = receiver.getsockname()[1]

    character = Character(config(port=port))
    await character.start()
    try:
        await asyncio.sleep(0.25)
        frames = []
        receiver.setblocking(False)
        while True:
            try:
                decoded = livelink.decode_frame(receiver.recv(4096))
            except BlockingIOError:
                break
            if decoded is not None:
                frames.append(decoded)
        assert len(frames) > 5, f"only {len(frames)} frames in 250 ms"
        assert {f.subject for f in frames} == {character.subjects[0]}
        indices = [f.frame_index for f in frames]
        assert indices == sorted(indices)
        assert len(set(indices)) == len(indices), "the timeline stood still"
    finally:
        await character.close()
        receiver.close()


@pytest.mark.asyncio
async def test_a_socket_that_will_not_open_turns_the_character_off():
    """And says so once. The stream is unaffected either way."""
    character = Character(config())

    def refuse() -> bool:
        character.sender.last_error = "OSError: nope"
        return False

    character.sender.open = refuse  # type: ignore[method-assign]
    await character.start()
    assert not character.enabled
    assert character.status() == "off"
    await character.close()


@pytest.mark.asyncio
async def test_close_is_safe_twice_and_without_a_start():
    character = Character(config())
    await character.close()
    await character.start()
    await character.close()
    await character.close()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_an_unknown_mood_in_config_is_a_load_time_error_naming_it():
    cfg = config()
    cfg.character.moods = {"smug": {"MouthSmileLeft": 0.4}}
    with pytest.raises(KeyError, match="smug"):
        Character(cfg)


def test_an_unknown_channel_in_config_is_a_load_time_error_naming_it():
    cfg = config()
    cfg.character.moods = {"happy": {"MouthSmirk": 0.4}}
    with pytest.raises(KeyError, match="MouthSmirk"):
        Character(cfg)


@pytest.mark.asyncio
async def test_the_status_never_claims_unreal_received_anything():
    character = Character(config())
    await character.start()
    try:
        assert "no ack" in character.status()
        assert "connected" not in character.status().lower()
    finally:
        await character.close()


# ---------------------------------------------------------------------------
# Recorded clips
# ---------------------------------------------------------------------------


def write_clip(path, frames, channel="HeadPitch", amplitude=9.0):
    """A clip in the Live Link Face app's own CSV export format."""
    names = livelink.LIVELINK_NAMES
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(",".join(["Timecode", "BlendshapeCount", *names]) + "\n")
        for index in range(frames):
            row = [0.0] * len(names)
            row[livelink.INDEX[channel]] = amplitude * np.sin(np.pi * index / frames)
            fh.write(
                f"00:00:00:{index % 60:02d},61,"
                + ",".join(f"{v:.6f}" for v in row)
                + "\n"
            )


def test_a_recorded_clip_replaces_the_procedural_one(tmp_path):
    """A nod performed by a person beats a nod described by a sine, and the
    whole point of tools/record_clip.py is that swapping one in is a file."""
    from narrator.avatar import face as face_mod

    write_clip(tmp_path / "nod.csv", 90)
    clips = face_mod.load_clips(tmp_path)
    assert set(clips) == {"nod"}

    plain = face_mod.FaceCompositor("P", seed=7)
    plain.beat.fire("nod", at=0.0)
    recorded = face_mod.FaceCompositor("P", seed=7, clips=clips)
    recorded.beat.fire("nod", at=0.0)

    assert recorded.beat._span == pytest.approx(1.5, abs=0.05)
    assert plain.beat._span != recorded.beat._span

    pitch = livelink.INDEX["HeadPitch"]
    a = np.array([plain.frame(i / 60, i / 60).copy()[pitch] for i in range(120)])
    b = np.array([recorded.frame(i / 60, i / 60).copy()[pitch] for i in range(120)])
    assert not np.allclose(a, b)
    assert b.max() > 8.0


def test_a_clip_is_matched_by_channel_name_not_by_column_position(tmp_path):
    """Exporters reorder and drop columns between versions. A clip recorded on
    a different build must animate the channels it says it animates."""
    from narrator.avatar import face as face_mod

    path = tmp_path / "wink.csv"
    with path.open("w", encoding="utf-8") as fh:
        fh.write("Timecode,BlendshapeCount,JawOpen,EyeBlinkLeft\n")
        fh.write("00:00:00:00,61,0.0,0.0\n")
        fh.write("00:00:00:01,61,0.25,1.0\n")
        fh.write("00:00:00:02,61,0.0,0.0\n")
    clips = face_mod.load_clips(tmp_path)
    assert clips["wink"].shape == (3, 61)
    assert clips["wink"][1][livelink.INDEX["EyeBlinkLeft"]] == pytest.approx(1.0)
    assert clips["wink"][1][livelink.INDEX["JawOpen"]] == pytest.approx(0.25)
    assert clips["wink"][1][livelink.INDEX["HeadYaw"]] == 0.0


def test_a_malformed_clip_costs_its_own_gesture_and_nothing_else(tmp_path):
    """A bad clip must not stop a stream, and must not stop the good clips
    beside it from loading."""
    from narrator.avatar import face as face_mod

    (tmp_path / "broken.csv").write_text("not,a,live,link,export\n1,2,3,4,5\n", "utf-8")
    (tmp_path / "empty.csv").write_text("Timecode,BlendshapeCount,JawOpen\n", "utf-8")
    write_clip(tmp_path / "nod.csv", 40)
    clips = face_mod.load_clips(tmp_path)
    assert set(clips) == {"nod"}


def test_no_clips_directory_is_the_normal_case(tmp_path):
    from narrator.avatar import face as face_mod

    assert face_mod.load_clips(tmp_path / "nothing here") == {}


@pytest.mark.asyncio
async def test_every_beat_the_model_can_write_has_a_face(tmp_path):
    """A test that fails when somebody adds a beat to expression.py and not a
    clip -- which would otherwise be a tag the model is told to use and the
    face silently ignores."""
    from narrator.avatar import face as face_mod
    from narrator.script.expression import BEATS as MODEL_BEATS

    character = Character(config())
    await character.start()
    try:
        for name in MODEL_BEATS:
            assert name in face_mod.BEAT_INDEX, f"{name} has no face"
            assert character.speaker.beat.fire(name, at=0.0), name
        assert character.speaker.beat.unknown == 0
    finally:
        await character.close()
