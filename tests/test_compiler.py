import json

from axstream.compiler import StreamCompiler

RESPONSE = """\
Let me fill the form.
```spec
{"op":"act","do":"click","target":{"ax":{"id":"e0"}}}
{"op":"act","do":"type","text":"hello"}
{"op":"act","do":"type","text":"hello"}
{"op":"done","status":"success"}
```
"""


def collect(compiler, chunks):
    events = []
    for chunk in chunks:
        events.extend(compiler.push(chunk))
    events.extend(compiler.finish())
    return events


def actions(events):
    return [e[1] for e in events if e[0] == "action"]


def test_whole_response():
    events = collect(StreamCompiler(), [RESPONSE])
    acts = actions(events)
    assert len(acts) == 4
    assert acts[0]["do"] == "click"
    assert acts[-1]["op"] == "done"
    assert ("text", "Let me fill the form.") in events


def test_no_dedup_of_identical_actions():
    acts = actions(collect(StreamCompiler(), [RESPONSE]))
    assert sum(1 for a in acts if a.get("text") == "hello") == 2


def test_chunk_boundary_invariance():
    expected = actions(collect(StreamCompiler(), [RESPONSE]))
    for size in (1, 2, 3, 7, 16):
        chunks = [RESPONSE[i : i + size] for i in range(0, len(RESPONSE), size)]
        assert actions(collect(StreamCompiler(), chunks)) == expected, f"chunk size {size}"


def test_incomplete_tail_not_executed_until_newline():
    compiler = StreamCompiler()
    events = list(compiler.push('```spec\n{"op":"act","do":"type","text":"hel'))
    assert actions([e for e in events]) == []
    events = list(compiler.push('lo"}\n'))
    acts = actions(events)
    assert len(acts) == 1
    assert acts[0]["text"] == "hello"


def test_malformed_line_dropped_stream_continues():
    compiler = StreamCompiler()
    text = '```spec\n{"op":"act","do":"click","target":{{bad\n{"op":"done","status":"success"}\n'
    events = collect(compiler, [text])
    kinds = [e[0] for e in events]
    assert "invalid" in kinds
    acts = actions(events)
    assert len(acts) == 1 and acts[0]["op"] == "done"


def test_unknown_action_rejected():
    compiler = StreamCompiler()
    line = json.dumps({"op": "act", "do": "self_destruct"})
    events = collect(compiler, [f"```spec\n{line}\n"])
    assert actions(events) == []
    assert any(e[0] == "invalid" for e in events)


def test_unfenced_mode():
    compiler = StreamCompiler(fenced=False)
    events = collect(compiler, ['{"op":"done","status":"success"}\n'])
    assert len(actions(events)) == 1
