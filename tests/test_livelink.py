"""The Live Link Face wire, and the face that is never still.

The encoder tests are golden-bytes rather than round-trip on purpose. A
round-trip proves this module agrees with itself, which is worth nothing:
the thing that has to be true is that Unreal's Apple ARKit Face Support
plugin recognises the datagram, and the only evidence available without
Unreal running is byte equality with an encoder known to work.

`tests/fixtures/livelink/*.bin` were produced by `pylivelinkface` in a
throwaway virtualenv, with only its clock pinned -- it recomputes a Timecode
from datetime.now() inside encode(), which would make the bytes different on
every run. Everything that matters came out of the reference untouched: the
struct layout, the 37-byte uuid field with no length prefix, the big-endian
name length, and the '!B61f' payload.
"""

from __future__ import annotations

import json
import pathlib
import socket

import numpy as np
import pytest

from narrator.avatar.livelink import (
    CHANNEL_COUNT,
    FIRST_ROTATION,
    INDEX,
    LIVELINK_NAMES,
    ROTATION_LIMIT_DEG,
    IdleLayer,
    LiveLinkFaceSender,
    blank,
    clamp,
    decode_frame,
    encode_frame,
    index_of,
)
from narrator.speech import arkit

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "livelink"
CASES = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))


def values_for(case: dict) -> np.ndarray:
    out = blank()
    for index, value in case["values"].items():
        out[int(index)] = value
    return out


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_encoder_is_byte_identical_to_a_known_good_one(name):
    """The only check available without Unreal running.

    Three cases, each covering something that has its own way of being wrong:
    an all-zero frame (the payload), a frame with head rotations (degrees, not
    0-1, in the last nine channels), and a subject name containing a digit
    (the length prefix, which is big-endian while the version word is little).
    """
    case = CASES[name]
    mine = encode_frame(
        case["subject"], case["frames"], values_for(case), case["fps"], case["uuid"]
    )
    assert mine == (FIXTURES / f"{name}.bin").read_bytes()


def test_the_uuid_field_is_exactly_37_bytes_whatever_the_subject_is_called():
    """The receiver slices bytes 4:41 unconditionally -- there is no length
    prefix on this field. A uuid one byte out shifts every field after it and
    the datagram is silently ignored."""
    short = encode_frame("A", 0, blank())
    long = encode_frame("A_very_long_subject_name", 0, blank())
    assert short[4:41].startswith(b"$")
    assert short[4:41] == long[4:41]
    assert len(short[4:41]) == 37


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_decoder_round_trips_the_golden_bytes(name):
    case = CASES[name]
    frame = decode_frame((FIXTURES / f"{name}.bin").read_bytes())
    assert frame is not None
    assert frame.subject == case["subject"]
    assert frame.frame_index == case["frames"]
    assert frame.fps == case["fps"]
    assert np.allclose(frame.values, values_for(case), atol=1e-6)


@pytest.mark.parametrize(
    "data",
    [b"", b"nonsense", b"\x06\x00\x00\x00" + b"$" * 20, bytes(400)],
)
def test_rubbish_decodes_to_none_rather_than_raising(data):
    """This reads a UDP port anything on the machine can write to. A malformed
    packet is a thing to ignore, not an exception at every call site."""
    assert decode_frame(data) is None


def test_a_frame_of_the_wrong_length_is_refused():
    with pytest.raises(ValueError, match="61"):
        encode_frame("Presenter", 0, np.zeros(52, dtype=np.float32))


# ---------------------------------------------------------------------------
# The 61 names
# ---------------------------------------------------------------------------


def test_the_name_order_has_61_unique_entries():
    assert len(LIVELINK_NAMES) == CHANNEL_COUNT == 61
    assert len(set(LIVELINK_NAMES)) == 61


def test_the_rotations_are_the_last_nine():
    """The 52 blendshapes are 0-1 and the nine rotations are degrees, which is
    why nothing here can be a single np.clip(0, 1)."""
    assert FIRST_ROTATION == 52
    assert LIVELINK_NAMES[FIRST_ROTATION:] == (
        "HeadYaw",
        "HeadPitch",
        "HeadRoll",
        "LeftEyeYaw",
        "LeftEyePitch",
        "LeftEyeRoll",
        "RightEyeYaw",
        "RightEyePitch",
        "RightEyeRoll",
    )


def test_every_arkit_mouth_channel_resolves_to_a_livelink_one():
    """`speech/arkit.py` writes `jawOpen`; Live Link calls it `JawOpen`. One
    function knows that, and this is what stops a rename in either module
    silently disconnecting a channel."""
    for name in arkit.CHANNELS:
        assert 0 <= index_of(name) < CHANNEL_COUNT
    assert len({index_of(n) for n in arkit.CHANNELS}) == len(arkit.CHANNELS)


def test_an_unknown_channel_name_says_so():
    with pytest.raises(KeyError, match="Nostril"):
        index_of("NostrilFlare")


def test_clamp_treats_blendshapes_and_degrees_differently():
    values = blank()
    values[INDEX["JawOpen"]] = 4.0
    values[INDEX["MouthClose"]] = -2.0
    values[INDEX["HeadYaw"]] = 400.0
    values[INDEX["HeadPitch"]] = -400.0
    clamp(values)
    assert values[INDEX["JawOpen"]] == 1.0
    assert values[INDEX["MouthClose"]] == 0.0
    assert values[INDEX["HeadYaw"]] == ROTATION_LIMIT_DEG
    assert values[INDEX["HeadPitch"]] == -ROTATION_LIMIT_DEG


# ---------------------------------------------------------------------------
# The sender
# ---------------------------------------------------------------------------


def test_a_frame_actually_reaches_a_socket():
    """Not a mock. The encoder, the socket and the decoder, over real UDP on
    the loopback -- which is every part of the chain this side of Unreal."""
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(2.0)
    port = receiver.getsockname()[1]

    sender = LiveLinkFaceSender("127.0.0.1", port, ("Presenter",), 60)
    assert sender.open()
    values = blank()
    values[INDEX["JawOpen"]] = 0.5
    try:
        assert sender.send("Presenter", values)
        frame = decode_frame(receiver.recv(4096))
    finally:
        sender.close()
        receiver.close()

    assert frame is not None
    assert frame.subject == "Presenter"
    assert frame.values[INDEX["JawOpen"]] == pytest.approx(0.5)
    assert sender.stats()["sent"] == 1


def test_a_late_tick_drops_its_backlog_rather_than_replaying_it():
    """Unreal's Live Link source smooths a late frame and cannot do anything
    sensible with eight arriving at once, which snaps the face. The frame
    index jumps so Unreal sees a gap in a timeline rather than a burst of
    stale frames all stamped as current."""
    sender = LiveLinkFaceSender("127.0.0.1", 1, ("Presenter",), 60)
    sender.open()
    before = sender._frame_index
    sender.note_late(8)
    assert sender.stats()["late"] == 1
    assert sender.stats()["dropped"] == 8
    assert sender._frame_index == before + 8
    sender.close()


def test_the_status_never_claims_a_connection_udp_cannot_prove():
    """An operator with Unreal closed sees what an operator with Unreal open
    sees. Saying "connected" would be inventing a green light."""
    sender = LiveLinkFaceSender()
    assert "no ack" in sender.status()
    assert "connected" not in sender.status().lower()


# ---------------------------------------------------------------------------
# The idle layer
# ---------------------------------------------------------------------------


def frames_over(seconds: float, seed: int = 7, fps: int = 60) -> np.ndarray:
    idle = IdleLayer(seed)
    return np.array([idle.frame(i / fps).copy() for i in range(int(seconds * fps))])


def test_the_same_seed_blinks_alike():
    """Two runs being compared have to be comparable, or a change in the head
    motion is a feeling rather than a diff."""
    assert np.array_equal(frames_over(20.0, seed=7), frames_over(20.0, seed=7))


def test_a_different_seed_does_not():
    """Two characters on stage must not blink in unison -- that reads as one
    puppet with two heads rather than as two people."""
    assert not np.array_equal(frames_over(20.0, seed=7), frames_over(20.0, seed=8))


def test_nothing_ever_leaves_range_over_ten_minutes():
    values = frames_over(600.0, fps=20)
    assert values[:, :FIRST_ROTATION].min() >= 0.0
    assert values[:, :FIRST_ROTATION].max() <= 1.0
    assert np.abs(values[:, FIRST_ROTATION:]).max() <= ROTATION_LIMIT_DEG


def test_the_blink_rate_is_within_its_bounds_over_ten_minutes():
    """A face that does not blink is dead and a face that blinks constantly is
    a tic. Counted over ten minutes because a handful of blinks proves
    nothing about the distribution."""
    values = frames_over(600.0)
    lid = values[:, INDEX["EyeBlinkLeft"]]
    shut = lid > 0.9
    # Rising edges: one per blink, however many frames it spans.
    blinks = int(np.sum(shut[1:] & ~shut[:-1])) + int(shut[0])
    per_minute = blinks / 10.0
    # 2-6 s apart, plus 10% doubles: 10-30 a minute is the honest window.
    assert 8 <= per_minute <= 32, f"{per_minute:.1f} blinks a minute"


def test_both_eyes_blink_together():
    """More than about 15 ms apart reads as a twitch rather than a blink."""
    values = frames_over(60.0)
    left = values[:, INDEX["EyeBlinkLeft"]]
    right = values[:, INDEX["EyeBlinkRight"]]
    assert left.max() > 0.9 and right.max() > 0.9
    # Never one eye fully shut while the other is fully open.
    assert not np.any((left > 0.9) & (right < 0.1))


def test_the_head_never_stops_moving():
    """A still head is the loudest single tell that there is nobody home."""
    values = frames_over(60.0)
    for channel in ("HeadYaw", "HeadPitch", "HeadRoll"):
        column = values[:, INDEX[channel]]
        assert column.std() > 0.1, f"{channel} is static"
        assert np.abs(column).max() <= 3.5


def test_the_head_motion_does_not_repeat_inside_a_stream():
    """Sines with rates that share a common multiple loop, and a two-second
    loop in the head motion is something an audience notices without being
    able to say why."""
    values = frames_over(300.0, fps=20)
    yaw = values[:, INDEX["HeadYaw"]]
    first, later = yaw[:600], yaw[3000:3600]
    assert not np.allclose(first, later, atol=0.05)


def test_the_gaze_stays_near_the_camera():
    """Without the recentre bias the eyes perform a random walk and end up
    staring at the corner of the room."""
    values = frames_over(300.0, fps=20)
    yaw = values[:, INDEX["LeftEyeYaw"]]
    assert abs(float(yaw.mean())) < 2.0
    assert np.abs(yaw).max() <= 6.0


def test_a_late_tick_does_not_strand_the_schedule():
    """The loop can be late by more than one blink interval. Advancing by a
    single step would leave the next blink permanently in the past, and the
    character would blink on every frame from then on."""
    idle = IdleLayer(7)
    idle.frame(0.0)
    idle.frame(45.0)  # 45 seconds in one step
    after = np.array([idle.frame(45.0 + i / 60).copy() for i in range(600)])
    lid = after[:, INDEX["EyeBlinkLeft"]]
    shut = lid > 0.9
    blinks = int(np.sum(shut[1:] & ~shut[:-1]))
    assert blinks <= 6, f"{blinks} blinks in ten seconds after a late tick"


def test_the_face_breathes():
    values = frames_over(20.0)
    puff = values[:, INDEX["CheekPuff"]]
    assert puff.min() >= 0.0 and puff.max() <= 0.05
    assert puff.std() > 0.001


# ---------------------------------------------------------------------------
# Composition cost
# ---------------------------------------------------------------------------


def test_a_frame_costs_no_allocation_of_a_python_list():
    """The layers return their own preallocated buffer. This is the property
    the 60 fps budget depends on, and it is easy to lose by adding a `.copy()`
    for tidiness."""
    idle = IdleLayer(7)
    first = idle.frame(0.0)
    second = idle.frame(0.1)
    assert first is second


def test_a_tick_advances_the_timeline_exactly_once():
    """Two subjects on stage are two subjects at ONE instant, not two
    instants -- so the frame index moves per tick, not per datagram.

    `tools/livelink_check.py` called `send` in its own loop and stamped four
    minutes of frames as frame 0. It sent perfectly, counted perfectly, and
    would have animated nothing: Unreal interpolates along that index, and
    every frame claiming to be the same moment is a face being replaced sixty
    times a second at one point in time.
    """
    sender = LiveLinkFaceSender("127.0.0.1", 1, ("A", "B"), 60)
    sender.open()
    try:
        first = sender._frame_index
        sender.send("A", blank())
        sender.send("A", blank())
        assert sender._frame_index == first, "send must not move the timeline"

        sender.send_all({"A": blank(), "B": blank()})
        assert sender._frame_index == first + 1, "one tick, one frame"
    finally:
        sender.close()


def test_the_frame_index_reaches_the_wire():
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(2.0)
    sender = LiveLinkFaceSender("127.0.0.1", receiver.getsockname()[1], ("P",), 60)
    sender.open()
    seen = []
    try:
        for _ in range(3):
            sender.send_all({"P": blank()})
            frame = decode_frame(receiver.recv(4096))
            assert frame is not None
            seen.append(frame.frame_index)
    finally:
        sender.close()
        receiver.close()
    assert seen == [seen[0], seen[0] + 1, seen[0] + 2]
