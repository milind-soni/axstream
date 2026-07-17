// Push-to-talk hotkey: hold Control+Option to talk, release either to stop.
// Uses a global flagsChanged monitor (needs Accessibility permission; when
// launched from a terminal it inherits the terminal's grant) plus a local
// monitor so the combo also works if our own panel ever has key focus.

import AppKit

final class Hotkey {
    var onTalkStart: () -> Void = {}
    var onTalkStop: () -> Void = {}

    private var monitors: [Any] = []
    private var held = false

    func install() {
        let handle: (NSEvent) -> Void = { [weak self] event in
            self?.flagsChanged(event.modifierFlags)
        }
        if let global = NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged, handler: handle) {
            monitors.append(global)
        }
        if let local = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged, handler: { event in
            handle(event)
            return event
        }) {
            monitors.append(local)
        }
    }

    func uninstall() {
        for monitor in monitors { NSEvent.removeMonitor(monitor) }
        monitors = []
    }

    private func flagsChanged(_ flags: NSEvent.ModifierFlags) {
        let bothHeld = flags.contains(.control) && flags.contains(.option)
        if bothHeld && !held {
            held = true
            onTalkStart()
        } else if !bothHeld && held {
            held = false
            onTalkStop()
        }
    }
}
