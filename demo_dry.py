"""Milestone A dry run: no network, no API keys, no computer-server.

A canned LLM response is replayed at a realistic decode speed into the real
compiler + executor against a MockComputer, proving the core thesis: actions
execute while the model is still "generating", and the timeline shows the
overlap. Compare TOTAL time against the buffered baseline printed at the end.
"""

import asyncio

from axstream.computer import MockComputer
from axstream.runner import run_task

AX_FIXTURE = {
    "windows": [
        {
            "title": "Login — Safari",
            "children": [
                {
                    "role": "AXTextField", "name": "Email", "enabled": True,
                    "absolute_position": "400.00;300.00", "size": "240;32", "children": [],
                },
                {
                    "role": "AXTextField", "name": "Password", "enabled": True,
                    "absolute_position": "400.00;350.00", "size": "240;32", "children": [],
                },
                {
                    "role": "AXButton", "name": "Sign_In", "enabled": True,
                    "absolute_position": "400.00;400.00", "size": "120;36", "children": [],
                },
            ],
        }
    ],
    "menubar_items": [],
    "dock_items": [],
}

CANNED_RESPONSE = """\
Filling in the login form.
```spec
{"op":"act","do":"click","target":{"ax":{"id":"e0"}}}
{"op":"act","do":"type","text":"milind@example.com"}
{"op":"act","do":"click","target":{"ax":{"id":"e1"}}}
{"op":"act","do":"type","text":"hunter2-hunter2"}
{"op":"act","do":"click","target":{"ax":{"id":"e2"}},"risk":"risky"}
{"op":"observe"}
```
"""

TOKENS_PER_SECOND = 40.0


async def main() -> None:
    computer = MockComputer(latency=0.05, ax_fixture=AX_FIXTURE)

    def stream_factory(system: str, user: str):
        from axstream.llm import replay_stream

        return replay_stream(CANNED_RESPONSE, TOKENS_PER_SECOND)

    results = await run_task(computer, "log into the site", stream_factory, max_bursts=1)

    result = results[0]
    executed = [e for e in result.events if e["kind"] == "executed"]
    stream_end = result.stream_ended_at
    total_streamed = result.last_action_done_at
    exec_time = sum(e["t_done"] - e["t_start"] for e in executed)
    buffered_total = stream_end + exec_time  # baseline: wait for full response, then act

    print("\n--- streamed vs buffered ---")
    print(f"decode time (full response):   {stream_end:.2f}s")
    print(f"pure execution time:           {exec_time:.2f}s")
    print(f"buffered baseline (decode+exec): {buffered_total:.2f}s")
    print(f"streamed total (this run):       {total_streamed:.2f}s")
    print(f"saved:                           {buffered_total - total_streamed:.2f}s "
          f"({(1 - total_streamed / buffered_total) * 100:.0f}%)")


if __name__ == "__main__":
    asyncio.run(main())
