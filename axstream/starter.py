"""The starter pack: curated macOS macros so a fresh install isn't empty.

Authored, not learned — but the same shape as learned macros, merged into the
user's store with `axstream seed` (or automatically on first `up`). Design
rules: keyboard shortcuts over clicks (robust — no AX targets to drift),
explicit `open` steps for app-scoped macros, no slot value copied from
examples, `risk: risky` on anything hard to undo. Context-free macros (copy,
paste, undo...) act on the frontmost app.
"""

from .macros import Macro

def _m(id, description, slots, examples, actions, risk=None, app=None):  # noqa: A002
    if risk:
        actions = [{**a, "risk": risk} if a.get("do") != "wait" else a
                   for a in actions]
    return Macro(id=id, description=description, slots=slots,
                 examples=examples, actions=actions, app=app)


STARTER = [
    # -- universal / context-free (frontmost app) ----------------------------
    _m("copy_selection", "copy the current selection", [],
       [{"utterance": "copy that", "slots": {}},
        {"utterance": "copy this", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "c"]}]),
    _m("paste_clipboard", "paste the clipboard", [],
       [{"utterance": "paste it", "slots": {}},
        {"utterance": "paste here", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "v"]}]),
    _m("undo_last", "undo the last action", [],
       [{"utterance": "undo that", "slots": {}},
        {"utterance": "undo", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "z"]}]),
    _m("redo_last", "redo the undone action", [],
       [{"utterance": "redo that", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "shift", "z"]}]),
    _m("select_all", "select everything", [],
       [{"utterance": "select all", "slots": {}},
        {"utterance": "select everything", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "a"]}]),
    _m("save_document", "save the current document", [],
       [{"utterance": "save this", "slots": {}},
        {"utterance": "save the file", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "s"]}]),
    _m("find_in_app", "find text in the current app", ["text"],
       [{"utterance": "find revenue in here", "slots": {"text": "revenue"}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "f"]},
        {"op": "act", "do": "wait", "ms": 300},
        {"op": "act", "do": "type", "text": "{text}"}]),
    _m("close_window", "close the current window", [],
       [{"utterance": "close this window", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "w"]}], risk="risky"),
    _m("minimize_window", "minimize the current window", [],
       [{"utterance": "minimize this", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "m"]}]),
    _m("fullscreen_window", "toggle fullscreen for the current window", [],
       [{"utterance": "go fullscreen", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["ctrl", "cmd", "f"]}]),
    _m("hide_current_app", "hide the current app", [],
       [{"utterance": "hide this app", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "h"]}]),
    _m("new_window", "open a new window in the current app", [],
       [{"utterance": "new window", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "n"]}]),

    # -- system-wide ---------------------------------------------------------
    _m("spotlight_open", "search for and open anything via spotlight", ["query"],
       [{"utterance": "spotlight activity monitor", "slots": {"query": "activity monitor"}},
        {"utterance": "search for disk utility", "slots": {"query": "disk utility"}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "space"]},
        {"op": "act", "do": "wait", "ms": 400},
        {"op": "act", "do": "type", "text": "{query}"},
        {"op": "act", "do": "wait", "ms": 600},
        {"op": "act", "do": "key", "keys": ["enter"]}]),
    _m("screenshot_full", "take a screenshot of the whole screen", [],
       [{"utterance": "take a screenshot", "slots": {}},
        {"utterance": "screenshot the screen", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "shift", "3"]}]),
    _m("screenshot_area", "screenshot a selected area", [],
       [{"utterance": "screenshot a section", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "shift", "4"]}]),
    _m("mission_control", "show all open windows", [],
       [{"utterance": "show all my windows", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["ctrl", "up"]}]),
    _m("open_app", "open or switch to an application", ["app"],
       [{"utterance": "open terminal", "slots": {"app": "terminal"}},
        {"utterance": "switch to slack", "slots": {"app": "slack"}}],
       [{"op": "act", "do": "open", "target": "{app}"}]),

    # -- browser (Safari / Firefox, app-scoped) ------------------------------
    _m("safari_open_url", "open a website in safari", ["url"],
       [{"utterance": "open wikipedia.org in safari", "slots": {"url": "wikipedia.org"}}],
       [{"op": "act", "do": "open", "target": "Safari"},
        {"op": "act", "do": "key", "keys": ["cmd", "t"]},
        {"op": "act", "do": "wait", "ms": 300},
        {"op": "act", "do": "type", "text": "{url}"},
        {"op": "act", "do": "key", "keys": ["enter"]}], app="Safari"),
    _m("firefox_open_url", "open a website in firefox", ["url"],
       [{"utterance": "open wikipedia.org in firefox", "slots": {"url": "wikipedia.org"}}],
       [{"op": "act", "do": "open", "target": "Firefox"},
        {"op": "act", "do": "key", "keys": ["cmd", "t"]},
        {"op": "act", "do": "wait", "ms": 300},
        {"op": "act", "do": "type", "text": "{url}"},
        {"op": "act", "do": "key", "keys": ["enter"]}], app="Firefox"),
    _m("safari_search", "search the web in safari", ["query"],
       [{"utterance": "search for weather in tokyo", "slots": {"query": "weather in tokyo"}}],
       [{"op": "act", "do": "open", "target": "Safari"},
        {"op": "act", "do": "key", "keys": ["cmd", "t"]},
        {"op": "act", "do": "wait", "ms": 300},
        {"op": "act", "do": "type", "text": "{query}"},
        {"op": "act", "do": "key", "keys": ["enter"]}], app="Safari"),
    _m("safari_new_tab", "open a new tab in safari", [],
       [{"utterance": "new tab in safari", "slots": {}}],
       [{"op": "act", "do": "open", "target": "Safari"},
        {"op": "act", "do": "key", "keys": ["cmd", "t"]}], app="Safari"),
    _m("reopen_closed_tab", "reopen the last closed tab", [],
       [{"utterance": "bring back that tab", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "shift", "t"]}]),

    # -- writing (TextEdit-based: reliable AX) -------------------------------
    _m("textfile_saying", "make a new text document and type some text", ["text"],
       [{"utterance": "make a text file that says pick up the parcel",
         "slots": {"text": "pick up the parcel"}}],
       [{"op": "act", "do": "open", "target": "TextEdit"},
        {"op": "act", "do": "key", "keys": ["cmd", "n"]},
        {"op": "act", "do": "wait", "ms": 500},
        {"op": "act", "do": "type", "text": "{text}"}], app="TextEdit"),

    # -- risky, gated --------------------------------------------------------
    _m("quit_current_app", "quit the current application", [],
       [{"utterance": "quit this app", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["cmd", "q"]}], risk="risky"),
    _m("lock_screen", "lock the screen", [],
       [{"utterance": "lock my screen", "slots": {}}],
       [{"op": "act", "do": "key", "keys": ["ctrl", "cmd", "q"]}], risk="risky"),
]
