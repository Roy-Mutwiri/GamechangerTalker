"""The four layers on their own, at fixed times on a clock nothing owns.

Every test here drives a layer with explicit timestamps rather than
`time.perf_counter()`. That is not a testing convenience -- it is the property
the layers are built to have, and the one bug in this area was a layer that
did not: `MoodLayer.set()` read the clock itself, so the expression was
undrivable on any clock but the real one and silently rendered nothing.
"""

from __future__ import annotations

import numpy as np
import pytest

from narrator.avatar import face as face_mod
from narrator.avatar import livelink
from narrator.script import expression
from narrator.speech import phonemes

CH = livelink.INDEX
LINE = "Gold just swept the Asian low and now we wait, and that is the boring part."


def compositor(**kwargs) -> face_mod.FaceCompositor:
    kwargs.setdefault("seed", 7)
    return face_mod.FaceCompositor("Presenter", **kwargs)


def run(face: face_mod.FaceCompositor, seconds: float, fps: int = 60) -> np.ndarray:
    return np.array(
        [face.frame(i / fps, i / fps).copy() for i in range(int(seconds * fps))]
    )


# ---------------------------------------------------------------------------
# The mouth
# ---------------------------------------------------------------------------


def test_the_mouth_track_covers_the_line_and_ends_shut():
    """A mouth with no explicit close sticks open on the final phoneme, which
    is the single most obvious way for lip sync to look broken."""
    spans = phonemes.from_text(LINE, 4.0)
    track = livelink.mouth_track(spans, 4.0, 60)
    assert len(track) >= 4.0 * 60
    assert track.max() > 0.3
    assert not track[-1].any()


def test_the_mouth_replaces_rather_than_adds():
    """A phoneme's jaw is an absolute statement about where the jaw is. Adding
    a mood's half-open jaw to it produces a character who cannot shut up."""
    spans = phonemes.from_text(LINE, 3.0)
    track = livelink.mouth_track(spans, 3.0, 60)

    # "surprised" contributes JawOpen, so if the mouth added rather than
    # replaced, a speaking jaw would sit above the track by that much.
    assert face_mod.MOODS["surprised"]["JawOpen"] > 0.0

    quiet = compositor()
    quiet.mood.set("surprised", hold=3.0, at=0.0)
    speaking = compositor()
    speaking.mood.set("surprised", hold=3.0, at=0.0)
    speaking.mouth.speak_track(track, started_at=0.0, fps=60)

    jaw = livelink.MOUTH_INDICES.tolist().index(CH["JawOpen"])
    for index in (30, 60, 90, 120):
        t = index / 60
        assert quiet.frame(t, t).copy()[CH["JawOpen"]] > 0.05, (
            "the mood should open an idle jaw"
        )
        # While speaking, the jaw is exactly what the phonemes said and
        # nothing else has been allowed to contribute to it.
        assert speaking.frame(t, t).copy()[CH["JawOpen"]] == pytest.approx(
            track[index][jaw], abs=1e-6
        )


def test_an_empty_line_produces_a_shut_mouth_rather_than_an_error():
    assert livelink.mouth_track([], 3.0, 60).shape == (1, 13)
    assert livelink.mouth_track(phonemes.from_text(LINE, 3.0), 0.0, 60).shape == (1, 13)


def test_the_mouth_shuts_when_the_track_runs_out():
    spans = phonemes.from_text(LINE, 1.0)
    face = compositor()
    face.mouth.speak_track(livelink.mouth_track(spans, 1.0, 60), started_at=0.0, fps=60)
    frames = run(face, 3.0)
    assert frames[-1][CH["JawOpen"]] < 0.01
    assert not face.mouth.speaking, "the source should have been dropped"


# ---------------------------------------------------------------------------
# The mood
# ---------------------------------------------------------------------------


def test_the_mood_eases_in_rather_than_cutting():
    """A linear step onto an expression reads as a wipe."""
    face = compositor()
    face.mood.set("serious", hold=5.0, at=0.0)
    at = [face.mood.envelope(t) for t in (0.0, 0.15, 0.30, 1.0)]
    assert at[0] == 0.0
    assert 0.0 < at[1] < at[2]
    assert at[2] == pytest.approx(1.0, abs=0.01)
    assert at[3] == pytest.approx(1.0, abs=0.01)


def test_the_mood_always_completes_its_release():
    """A mood that outlives its line reads as a character stuck in it."""
    face = compositor()
    face.mood.set("excited", hold=1.0, at=0.0)
    assert face.mood.envelope(1.2) > 0.5
    assert face.mood.envelope(5.0) == 0.0


def test_a_mood_takes_the_callers_clock():
    """The bug this whole file is shaped around. set() used to read
    perf_counter itself, so on any other clock the elapsed time went hugely
    negative and the expression silently never rendered."""
    face = compositor()
    face.mood.set("serious", hold=3.0, at=100.0)
    assert face.mood.envelope(100.0) == 0.0
    assert face.mood.envelope(100.3) == pytest.approx(1.0, abs=0.01)
    frames = np.array([face.frame(i / 60, 100.0 + i / 60).copy() for i in range(120)])
    assert frames[:, CH["BrowDownLeft"]].max() > 0.2


def test_an_unknown_mood_is_counted_and_changes_nothing():
    face = compositor()
    assert not face.mood.set("flabbergasted", at=0.0)
    assert face.mood.unknown == 1
    assert face.mood.name == ""


def test_the_operators_table_merges_over_the_defaults():
    merged = face_mod.merge_moods({"happy": {"MouthSmileLeft": 0.9}})
    happy = dict(merged["happy"])
    assert happy[CH["MouthSmileLeft"]] == 0.9
    # Everything else in the default entry survives.
    assert happy[CH["MouthSmileRight"]] == face_mod.MOODS["happy"]["MouthSmileRight"]
    assert "serious" in merged, "other moods must not be dropped"


def test_an_unknown_mood_in_the_table_names_itself():
    with pytest.raises(KeyError, match="smug"):
        face_mod.merge_moods({"smug": {"MouthSmileLeft": 0.4}})


def test_an_unknown_channel_in_the_table_names_itself_and_its_mood():
    with pytest.raises(KeyError, match="MouthSmirk"):
        face_mod.merge_moods({"happy": {"MouthSmirk": 0.4}})


def test_every_mood_the_model_can_write_has_a_face():
    """A tag the model is told to use and the face ignores is worse than one
    it was never offered."""
    for mood in expression.MOODS:
        assert mood in face_mod.MOODS, f"{mood} has no face"


def test_every_emote_the_library_can_send_has_a_face():
    """The templates emit the five [warudo] expression names, not the hosts'
    eight moods. Both vocabularies reach the same layer."""
    for emote in ("neutral", "excited", "bored", "alert", "surprised"):
        assert emote in face_mod.MOODS, f"{emote} has no face"


# ---------------------------------------------------------------------------
# Beats
# ---------------------------------------------------------------------------


def test_a_beat_is_placed_not_started():
    """fire() schedules. Starting immediately is what made the last beat in a
    line overwrite every earlier one before any of them played."""
    face = compositor()
    face.beat.fire("nod", at=5.0)
    assert face.beat.pending == 1
    assert face.beat.name == "", "nothing should be running yet"
    frame = face.frame(0.0, 0.0).copy()
    assert abs(frame[CH["HeadPitch"]]) < 4.0, "the nod started early"


def test_beats_run_in_the_order_they_were_placed():
    face = compositor()
    face.beat.fire("nod", at=2.0)
    face.beat.fire("headshake", at=0.5)
    frames = run(face, 4.0)
    yaw_at = int(frames[:, CH["HeadYaw"]].argmax()) / 60
    pitch_at = int(frames[:, CH["HeadPitch"]].argmax()) / 60
    assert 0.5 <= yaw_at < 1.5, "the headshake did not run at 0.5s"
    assert pitch_at > 1.9, "the nod did not run at 2.0s"


def test_a_beat_that_is_entirely_in_the_past_is_dropped():
    face = compositor()
    face.beat.fire("nod", at=0.0)
    frame = face.frame(20.0, 20.0).copy()
    assert abs(frame[CH["HeadPitch"]]) < 4.0
    assert face.beat.pending == 0


def test_the_schedule_cannot_grow_without_limit():
    face = compositor()
    for index in range(40):
        face.beat.fire("nod", at=100.0 + index)
    assert face.beat.pending <= face_mod.MAX_PENDING_BEATS


def test_every_beat_the_model_can_write_has_a_clip():
    """Fails when somebody adds a beat to expression.py and not a face."""
    for beat in expression.BEATS:
        assert beat in face_mod.BEAT_INDEX, f"{beat} has no clip"
        assert face_mod.BEAT_SPAN[beat] > 0.0


def test_a_beat_returns_the_face_to_where_it_found_it():
    """Both ends of every curve are zero, so an interrupted beat cannot leave
    the head at an angle for the rest of the stream."""
    for name in expression.BEATS:
        face = compositor()
        face.beat.fire(name, at=0.0)
        span = face_mod.BEAT_SPAN[name]
        after = face.frame(span + 1.0, span + 1.0).copy()
        assert abs(after[CH["HeadPitch"]]) < 4.0, name
        assert abs(after[CH["HeadYaw"]]) < 4.0, name


def test_a_laughing_face_follows_the_audio_and_not_the_tag():
    """`performance.plan` decided where the chuckle goes and `_render` built
    the audio around it, so its offset is where the laugh IS. The tag's word
    index is only where the laugh was ASKED for, and clause splitting moves
    the two apart. Timing the face off the tag puts the laugh on the face a
    fifth of a second from the laugh in the ear."""
    from narrator.avatar.character import SOUND_BEAT_KINDS

    assert SOUND_BEAT_KINDS["laugh"] == "chuckle"
    assert SOUND_BEAT_KINDS["chuckle"] == "chuckle"
    assert SOUND_BEAT_KINDS["sigh"] == "out"
    # Every sound beat expression.py knows must map to a delivery beat kind,
    # or its face falls back to the tag's word index without anybody noticing.
    for beat in expression.BEAT_AS_SOUND:
        assert beat in SOUND_BEAT_KINDS, f"{beat} would be timed off its tag"


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_nothing_ever_leaves_range_whatever_is_running():
    """Four layers adding into one vector, all at once, at full strength."""
    spans = phonemes.from_text(LINE, 3.0)
    face = compositor()
    face.mouth.speak_track(livelink.mouth_track(spans, 3.0, 60), started_at=0.0, fps=60)
    face.mood.set("surprised", hold=3.0, at=0.0)
    for index, beat in enumerate(expression.BEATS):
        face.beat.fire(beat, at=index * 0.4)
    frames = run(face, 4.0)
    assert frames[:, : livelink.FIRST_ROTATION].min() >= 0.0
    assert frames[:, : livelink.FIRST_ROTATION].max() <= 1.0
    assert np.abs(frames[:, livelink.FIRST_ROTATION :]).max() <= 30.0


def test_the_face_is_never_still():
    """A frozen face is how an audience is told the thing has crashed."""
    frames = run(compositor(), 30.0, fps=30)
    identical = np.sum(np.all(np.abs(np.diff(frames, axis=0)) < 1e-9, axis=1))
    assert identical == 0


def test_rest_stops_the_mouth_and_the_mood_but_not_the_idle():
    face = compositor()
    spans = phonemes.from_text(LINE, 2.0)
    face.mouth.speak_track(livelink.mouth_track(spans, 2.0, 60), started_at=0.0, fps=60)
    face.mood.set("happy", hold=5.0, at=0.0)
    face.frame(0.5, 0.5)
    face.rest()
    assert not face.mouth.speaking
    assert face.mood.name == ""
    after = run(face, 5.0)
    assert after[:, CH["EyeBlinkLeft"]].max() > 0.9, "the idle layer stopped too"


def test_two_seats_get_different_idle_schedules():
    left = face_mod.FaceCompositor("A", seed=7)
    right = face_mod.FaceCompositor("B", seed=8)
    a = run(left, 30.0, fps=30)[:, CH["EyeBlinkLeft"]]
    b = run(right, 30.0, fps=30)[:, CH["EyeBlinkLeft"]]
    assert a.max() > 0.9 and b.max() > 0.9
    assert not np.allclose(a, b)


def test_a_beat_can_still_move_the_mouth_mid_line():
    """The mouth group is held across the mood layer, and the beat layer runs
    AFTER that restore. The ordering is load-bearing rather than incidental:
    move the beat above it and a laugh mid-sentence becomes a smile with a
    closed jaw, which is a worse artefact than the one the hold prevents.
    """
    spans = phonemes.from_text(LINE, 3.0)
    track = livelink.mouth_track(spans, 3.0, 60)

    def speak(with_laugh: bool) -> np.ndarray:
        face = compositor()
        face.mouth.speak_track(track, started_at=0.0, fps=60)
        face.mood.set("surprised", hold=3.0, at=0.0)
        if with_laugh:
            face.beat.fire("laugh", at=1.0)
        return run(face, 3.0)

    # Against the same line without the beat, so the phoneme mouth -- which is
    # wide open in places anyway -- cannot be mistaken for the laugh.
    plain = speak(False)[60:130, CH["JawOpen"]]
    laughing = speak(True)[60:130, CH["JawOpen"]]
    assert laughing.max() > plain.max() + 0.05, (
        "the laugh did not reach the jaw: the beat layer is being run before "
        "the mouth group is restored, so its mouth contribution is discarded"
    )
    assert speak(True)[60:130, CH["MouthSmileLeft"]].max() > 0.3


def test_the_mood_owns_the_whole_face_in_silence():
    """The hold is only while somebody is speaking. With the mouth at rest a
    surprised face is allowed its dropped jaw, which is most of what makes it
    read as surprise."""
    face = compositor()
    face.mood.set("surprised", hold=3.0, at=0.0)
    frames = run(face, 2.0)
    assert frames[:, CH["JawOpen"]].max() > 0.05
    assert not face.mouth.speaking
