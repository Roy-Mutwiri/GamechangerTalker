"""The five calls to Unreal, and the two wires they can travel down.

Nothing here needs Unreal. The OSC tests bind a real UDP socket on loopback
and read back what was actually sent, and the Remote Control tests replace
urlopen -- so what is asserted is the bytes and the request body, not a mock
agreeing with the code that built it.

The padding tests are the load-bearing ones. An OSC message whose address
length happens to be a multiple of four is the difference between a gesture
that plays and a gesture that silently does not, and neither Unreal nor this
process reports anything when it goes wrong.
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any
from urllib import error as urlerror

import pytest

from narrator.avatar import unreal as U
from narrator.config import load_config


def config(**overrides: Any):
    cfg = load_config()
    cfg.character.enabled = True
    for key, value in overrides.items():
        setattr(cfg.character.unreal, key, value)
    return cfg


# ---------------------------------------------------------------------------
# OSC on the wire
# ---------------------------------------------------------------------------
def test_every_message_is_four_byte_aligned():
    """OSC requires it, and Unreal's server node quietly misreads it if not."""
    for address, arguments in [
        ("/character/speaking", [True]),
        ("/character/mood", ["excited", 0.8]),
        ("/character/gesture", ["nod"]),
        ("/character/camera", ["closeup"]),
        ("/character/stage", [2]),
    ]:
        packet = U.osc_message(address, arguments)
        assert len(packet) % 4 == 0, f"{address} produced {len(packet)} bytes"


def test_addresses_of_every_length_round_trip():
    """The bug this pins only appears when a string is already 4-aligned.

    `/character/gesture` and `/character/stage` survive the naive padding by
    luck of their lengths; `/character/mood` and `/character/speaking` do not,
    and arrive with no arguments at all. So every residue is covered here on
    purpose -- testing one address would have passed against the broken code.
    """
    for address in ("/a", "/ab", "/abc", "/abcd", "/abcde", "/abcdef"):
        got_address, got_arguments = U.osc_parse(U.osc_message(address, ["x", 1]))
        assert got_address == address
        assert got_arguments == ["x", 1]


def test_string_arguments_of_every_length_round_trip():
    names = ["a", "ab", "abc", "abcd", "abcde", "lean_in", "headshake"]
    _address, arguments = U.osc_parse(U.osc_message("/character/gesture", names))
    assert arguments == names


def test_a_bool_travels_as_an_int():
    """Unreal's OSC Server reads a bool pin off an int argument."""
    _address, arguments = U.osc_parse(U.osc_message("/character/speaking", [True]))
    assert arguments == [1]
    _address, arguments = U.osc_parse(U.osc_message("/character/speaking", [False]))
    assert arguments == [0]


def test_a_float_survives_as_a_float():
    _address, arguments = U.osc_parse(U.osc_message("/character/mood", ["bored", 0.25]))
    assert arguments[0] == "bored"
    assert arguments[1] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# OSC through the transport
# ---------------------------------------------------------------------------
@pytest.fixture
def receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    yield sock
    sock.close()


async def drain(transport: U.UnrealTransport, seconds: float = 0.3) -> None:
    worker = asyncio.create_task(transport.start())
    await asyncio.sleep(seconds)
    await transport.stop()
    await asyncio.wait_for(worker, timeout=1.0)


@pytest.mark.asyncio
async def test_the_five_calls_reach_the_wire_with_their_arguments(receiver):
    port = receiver.getsockname()[1]
    transport = U.OscTransport("127.0.0.1", port)

    transport.set_speaking(True)
    transport.set_mood("excited", 0.8)
    transport.play_gesture("lean-in")
    transport.set_camera("closeup")
    transport.set_stage(2)
    await drain(transport)

    got = [U.osc_parse(receiver.recv(1024)) for _ in range(5)]
    assert got == [
        ("/character/speaking", [1]),
        ("/character/mood", ["excited", pytest.approx(0.8)]),
        ("/character/gesture", ["lean_in"]),
        ("/character/camera", ["closeup"]),
        ("/character/stage", [2]),
    ]
    assert transport.stats()["sent"] == 5


@pytest.mark.asyncio
async def test_a_dash_becomes_an_underscore(receiver):
    """`lean-in` is a beat name; no Unreal montage may carry a dash."""
    port = receiver.getsockname()[1]
    transport = U.OscTransport("127.0.0.1", port)
    transport.play_gesture("lean-in")
    await drain(transport)
    assert U.osc_parse(receiver.recv(1024))[1] == ["lean_in"]


# ---------------------------------------------------------------------------
# The never-raise guarantee
# ---------------------------------------------------------------------------
def test_an_off_contract_function_is_counted_not_raised():
    """`call` runs inside _speak. An exception there costs the stream a line."""
    transport = U.OscTransport("127.0.0.1", 1)
    transport.call("SetHat", name="fedora")
    assert transport.stats()["rejected"] == 1
    assert transport.stats()["queued"] == 0
    assert "not one of the five" in transport.last_error


def test_a_missing_pin_is_counted_not_raised():
    transport = U.OscTransport("127.0.0.1", 1)
    transport.call("SetMood", name="bored")  # no weight
    assert transport.stats()["rejected"] == 1
    assert "weight" in transport.last_error


@pytest.mark.asyncio
async def test_a_dead_socket_does_not_escape_the_worker():
    """Port 1 on loopback refuses; the worker must absorb it and carry on."""
    transport = U.OscTransport("127.0.0.1", 1)
    for _ in range(3):
        transport.play_gesture("nod")
        transport._last.clear()  # defeat the dedup, we want three real sends
    await drain(transport)
    assert transport.stats()["sent"] + transport.stats()["failed"] == 3


# ---------------------------------------------------------------------------
# Queue behaviour
# ---------------------------------------------------------------------------
def test_a_full_queue_drops_the_oldest():
    """The newest instruction is the one that describes the present."""
    transport = U.OscTransport("127.0.0.1", 1)
    for index in range(U.QUEUE_LIMIT + 5):
        transport.call("SetStage", count=index)
        transport._last.clear()
    stats = transport.stats()
    assert stats["queued"] == U.QUEUE_LIMIT
    assert stats["dropped"] == 5
    # The survivors end at the most recent call, not the earliest.
    last = None
    while not transport._queue.empty():
        last = transport._queue.get_nowait()
    assert last is not None and last.parameters["count"] == U.QUEUE_LIMIT + 4


def test_an_identical_call_inside_the_dedup_window_is_one_call():
    transport = U.OscTransport("127.0.0.1", 1)
    transport.call("PlayGesture", name="nod")
    transport.call("PlayGesture", name="nod")
    assert transport.stats()["queued"] == 1
    transport.call("PlayGesture", name="shrug")
    assert transport.stats()["queued"] == 2


def test_the_edge_triggered_three_only_send_on_a_change():
    transport = U.OscTransport("127.0.0.1", 1)
    assert transport.set_speaking(True) is True
    assert transport.set_speaking(True) is False
    assert transport.set_speaking(False) is True

    assert transport.set_stage(2) is True
    assert transport.set_stage(2) is False

    assert transport.set_camera("chart") is True
    assert transport.set_camera("chart") is False
    assert transport.set_camera("main") is True


def test_a_mood_is_not_edge_triggered():
    """The same mood twice is two lines in the same mood, and the posture has
    to be re-asserted or the second line is played by a face that has decayed
    back to neutral."""
    transport = U.OscTransport("127.0.0.1", 1)
    assert transport.set_mood("bored") is True
    assert transport.set_mood("bored") is True


# ---------------------------------------------------------------------------
# Remote Control
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload: bytes = b"{}") -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_the_request_body_is_what_remote_control_expects(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["body"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(U.urlrequest, "urlopen", fake_urlopen)
    transport = U.RemoteControlTransport("http://127.0.0.1:30010/", "/Game/X.X:Y")
    transport.call_now("SetMood", name="excited", weight=0.5)

    assert seen["url"] == "http://127.0.0.1:30010/remote/object/call"
    assert seen["method"] == "PUT"
    assert seen["body"] == {
        "objectPath": "/Game/X.X:Y",
        "functionName": "SetMood",
        "parameters": {"name": "excited", "weight": 0.5},
        # An undo transaction per nod fills the editor's undo stack over an
        # eight-hour stream, and none of these are things anyone undoes.
        "generateTransaction": False,
    }
    assert transport.stats()["sent"] == 1


@pytest.mark.asyncio
async def test_an_http_error_is_absorbed_and_named(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urlerror.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            None,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(U.urlrequest, "urlopen", fake_urlopen)
    transport = U.RemoteControlTransport("http://127.0.0.1:30010", "/Game/X.X:Y")
    transport.play_gesture("nod")
    await drain(transport)

    assert transport.stats()["failed"] == 1
    assert transport.stats()["sent"] == 0
    assert "400" in transport.last_error
    assert transport.reachable is False


def test_ping_is_false_when_nothing_answers(monkeypatch):
    def fake_urlopen(*args: Any, **kwargs: Any):
        raise urlerror.URLError("connection refused")

    monkeypatch.setattr(U.urlrequest, "urlopen", fake_urlopen)
    assert U.RemoteControlTransport("http://127.0.0.1:30010", "/X").ping() is False


def test_osc_never_claims_to_have_been_answered():
    """UDP has no reply. A tick here would be an invention."""
    assert U.OscTransport("127.0.0.1", 8000).ping() is False


# ---------------------------------------------------------------------------
# Choosing a transport
# ---------------------------------------------------------------------------
def test_none_is_inert_and_costs_nothing():
    transport = U.build_transport(config(transport="none"))
    assert isinstance(transport, U.NullTransport)
    transport.play_gesture("nod")
    assert transport.stats() == {
        "sent": 0,
        "failed": 0,
        "dropped": 0,
        "rejected": 0,
        "queued": 0,
    }


def test_the_character_being_off_beats_any_transport():
    cfg = config(transport="remote_control", object_path="/Game/X.X:Y")
    cfg.character.enabled = False
    assert isinstance(U.build_transport(cfg), U.NullTransport)


def test_osc_needs_no_object_path():
    """The whole appeal of the fallback: nothing to copy out of the editor."""
    transport = U.build_transport(config(transport="osc", osc_port=9123))
    assert isinstance(transport, U.OscTransport)
    assert transport.port == 9123


def test_an_empty_object_path_is_not_a_crash():
    """An operator who has not been into the editor yet still gets a face."""
    transport = U.build_transport(config(transport="remote_control", object_path=""))
    assert isinstance(transport, U.NullTransport)


def test_remote_control_is_built_from_the_configured_url():
    cfg = config(
        transport="remote_control",
        remote_control_url="http://10.0.0.4:30010",
        object_path="/Game/Maps/Studio.Studio:PersistentLevel.BP_Presenter_C_1",
    )
    transport = U.build_transport(cfg)
    assert isinstance(transport, U.RemoteControlTransport)
    assert transport.url == "http://10.0.0.4:30010"
    assert transport.object_path.endswith("BP_Presenter_C_1")


# ---------------------------------------------------------------------------
# The contract itself
# ---------------------------------------------------------------------------
def test_the_contract_is_five_functions_and_every_one_has_an_address():
    assert set(U.FUNCTIONS) == {
        "SetSpeaking",
        "SetMood",
        "PlayGesture",
        "SetCamera",
        "SetStage",
    }
    assert set(U.OSC_ADDRESS) == set(U.FUNCTIONS)


def test_the_named_methods_cover_the_whole_contract():
    """If a sixth function is added, it needs a method or nothing calls it."""
    methods = {
        "SetSpeaking": "set_speaking",
        "SetMood": "set_mood",
        "PlayGesture": "play_gesture",
        "SetCamera": "set_camera",
        "SetStage": "set_stage",
    }
    assert set(methods) == set(U.FUNCTIONS)
    for name in methods.values():
        assert callable(getattr(U.OscTransport("127.0.0.1", 1), name))
