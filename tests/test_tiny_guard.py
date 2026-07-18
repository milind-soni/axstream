"""The slot-verbatim guard: reject matches whose slot values were not spoken."""

from axstream.tiny import slots_verbatim


def test_verbatim_slots_pass():
    assert slots_verbatim("launch safari", {"app": "safari"})
    assert slots_verbatim("make a text file that says Hello World",
                          {"text": "hello world"})
    assert slots_verbatim("no slots at all", {})


def test_hallucinated_slot_rejected():
    # the live failure: "launch a text editor" -> slot copied from an example
    assert not slots_verbatim("launch a text editor", {"text": "see you soon"})


def test_partial_presence_still_requires_full_value():
    assert not slots_verbatim("open notes", {"app": "notes for me"})
