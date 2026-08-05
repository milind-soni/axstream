import AppKit
import AVFoundation
import ApplicationServices

// Menu-bar app: hold ⌃⌥ → mic opens; release → whisper → tiny matcher →
// axstream replay. The entire hot path is in-process except the replay
// engine itself (the axstream CLI).
final class BarController: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private let menu = NSMenu(title: "Axstream")
    private var whisper: Whisper?
    private let matcher = Matcher()
    private let prerank = Prerank()
    private let recorder = Recorder()
    private let pipeline = DispatchQueue(label: "axstream.pipeline")
    private let hud = Hud()
    private var holding = false
    private var busy = false
    private var notice = ""
    private var lastResult: (name: String, ok: Bool, seconds: Double)?
    private var workflowsByTag: [Int: Workflow] = [:]
    private var monitors: [Any] = []
    private var trustPoll: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "AX"
        statusItem.button?.toolTip = "Axstream — hold ⌃⌥ and speak a workflow"
        menu.delegate = self
        statusItem.menu = menu
        requestPermissions()
        installHotkey()
        preload()
        print("axstream-bar ready (pid \(ProcessInfo.processInfo.processIdentifier))")
    }

    func applicationWillTerminate(_ notification: Notification) {
        // free the Metal context before ggml's atexit teardown (it asserts
        // if a device still holds residency sets)
        whisper = nil
    }

    private func requestPermissions() {
        AVCaptureDevice.requestAccess(for: .audio) { granted in
            print("microphone \(granted ? "granted" : "DENIED")")
            if !granted {
                DispatchQueue.main.async {
                    self.hud.show("Grant Microphone to “AxstreamBar” "
                                  + "(System Settings → Privacy → Microphone)",
                                  autoHide: 8)
                }
            }
        }
        let prompt = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        let trusted = AXIsProcessTrustedWithOptions([prompt: true] as CFDictionary)
        print("accessibility \(trusted ? "granted" : "DENIED")")
        if !trusted {
            hud.show("Grant Accessibility to “AxstreamBar” to enable the "
                     + "⌃⌥ hold-to-talk hotkey", autoHide: 8)
            // A grant while we're running doesn't revive already-installed
            // monitors — poll and reinstall the moment it lands.
            trustPoll = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) {
                [weak self] timer in
                guard let self, AXIsProcessTrusted() else { return }
                timer.invalidate()
                trustPoll = nil
                installHotkey()
                print("accessibility granted — hotkey reinstalled")
                hud.show("⌃⌥ hold-to-talk enabled — hold and speak", autoHide: 3)
            }
        }
    }

    private func preload() {
        pipeline.async { [self] in
            let t0 = Date()
            whisper = Whisper()
            if whisper == nil {
                DispatchQueue.main.async {
                    self.notice = "Model missing: ~/.axstream/models/ggml-base.en.bin"
                }
            }
            // Warm llama-server's prompt-prefix cache so the first real
            // match costs ~100ms like every later one, and pre-embed the
            // library so shortlisting adds only the query embedding (~20ms).
            let library = MacroLibrary.voiceWorkflows()
            if prerank.available() {
                prerank.warm(library)
                print("prerank ready (\(library.count) templates embedded)")
            }
            let templates = library.prefix(25).map(MatchTemplate.from)
            if matcher.available() {
                _ = matcher.match("warm up", templates: Array(templates))
            }
            print(String(format: "voice ready in %.1fs", Date().timeIntervalSince(t0)))
        }
    }

    // --- hold-to-talk ------------------------------------------------------

    private func installHotkey() {
        for monitor in monitors { NSEvent.removeMonitor(monitor) }
        monitors.removeAll()
        if let global = NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged,
                                                          handler: { [weak self] event in
            self?.handleFlags(event)
        }) { monitors.append(global) }
        if let local = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged,
                                                        handler: { [weak self] event in
            self?.handleFlags(event)
            return event
        }) { monitors.append(local) }
    }

    private func handleFlags(_ event: NSEvent) {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        let active = flags.contains(.control) && flags.contains(.option)
            && !flags.contains(.command) && !flags.contains(.shift)
        if active && !holding {
            holdStart()
        } else if holding && !active {
            holdEnd()
        }
    }

    private func holdStart() {
        guard !busy else {
            print("hold ignored: busy")
            return
        }
        guard whisper != nil else {
            print("hold ignored: model still loading")
            hud.show("Voice model still loading…", autoHide: 2)
            return
        }
        do {
            try recorder.start()
        } catch {
            print("mic error: \(error.localizedDescription)")
            hud.show("Mic error: \(error.localizedDescription)", autoHide: 4)
            return
        }
        holding = true
        print("hold start")
        statusItem.button?.title = "AX 🎤"
        hud.show("🎤 Listening… release ⌃⌥ to run")
    }

    private func holdEnd() {
        holding = false
        let samples = recorder.stop()
        print("hold end (\(String(format: "%.2f", Double(samples.count) / 16_000))s)")
        // a tap shorter than ~0.4s is almost certainly a shortcut chord
        // (window managers use ⌃⌥-arrow), not speech — ignore silently
        guard samples.count > 6400 else {
            statusItem.button?.title = "AX"
            hud.hide()
            return
        }
        busy = true
        statusItem.button?.title = "AX …"
        hud.show("…")
        pipeline.async { [self] in process(samples) }
    }

    private func process(_ samples: [Float]) {
        guard let whisper else { return finish(notice: "Voice not loaded") }
        let peak = samples.reduce(0) { max($0, abs($1)) }
        guard peak > 1e-5 else {
            return finish(notice: "Mic delivered silence — check Microphone "
                          + "permission for “AxstreamBar”")
        }
        let t0 = Date()
        let text = whisper.transcribe(samples)
        let sttMs = Int(Date().timeIntervalSince(t0) * 1000)
        guard !text.isEmpty else {
            return finish(notice: samples.count > 16_000 ? "Heard nothing" : "")
        }
        DispatchQueue.main.async { [self] in hud.show("“\(text)”") }
        let workflows = MacroLibrary.voiceWorkflows()
        let t1 = Date()

        func matchOne(_ utterance: String) -> (Workflow, [String: String])? {
            // shortlist keeps the matcher prompt small and in-distribution
            // at any library size; embedder offline → full ranked library
            let candidates = prerank.shortlist(utterance, from: workflows)
                ?? Array(workflows.prefix(25))
            guard let hit = matcher.match(utterance,
                                          templates: candidates.map(MatchTemplate.from)),
                  let workflow = workflows.first(where: { $0.name == hit.template })
            else { return nil }
            return (workflow, hit.slots)
        }

        // Compound commands: "open blender and select the shape and delete
        // the shape" — chain ONLY when every clause matches a macro, so a
        // false split ("note saying milk and eggs") can never make things
        // worse than the single-utterance path.
        let clauses = Self.splitClauses(text)
        if clauses.count >= 2 {
            let plan = clauses.compactMap(matchOne)
            if plan.count == clauses.count {
                let names = plan.map(\.0.name).joined(separator: " → ")
                print("heard \"\(text)\" stt=\(sttMs)ms -> chain [\(names)]")
                runChain(plan)
                return
            }
        }

        let hit = matchOne(text)
        let matchMs = Int(Date().timeIntervalSince(t1) * 1000)
        print("heard \"\(text)\" stt=\(sttMs)ms match=\(matchMs)ms -> "
              + (hit?.0.name ?? "none"))
        guard let hit else {
            fallback(text)
            return
        }
        run(workflow: hit.0, slots: hit.1)
    }

    static func splitClauses(_ text: String) -> [String] {
        var clauses = [text]
        for separator in [" and then ", " then ", " and "] {
            clauses = clauses.flatMap {
                $0.components(separatedBy: separator)
            }
        }
        return clauses.map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    private func runChain(_ plan: [(Workflow, [String: String])]) {
        for (index, step) in plan.enumerated() {
            DispatchQueue.main.async { [self] in
                busy = true
                notice = ""
                statusItem.button?.title = "AX ▶"
                hud.show("▶ \(index + 1)/\(plan.count)  \(step.0.name)")
            }
            let outcome = Replay.run(workflow: step.0, slots: step.1)
            print(String(format: "chain %d/%d %@ %@ in %.1fs", index + 1,
                         plan.count, step.0.name,
                         outcome.ok ? "ok" : "FAILED", outcome.seconds))
            if !outcome.ok {
                DispatchQueue.main.async { [self] in
                    lastResult = (step.0.name, false, outcome.seconds)
                    notice = "✗ chain stopped at \(step.0.name)"
                    hud.show("✗ chain stopped at \(index + 1)/\(plan.count) "
                             + "(\(step.0.name))", autoHide: 4)
                    finishOnMain(title: "AX !")
                }
                return
            }
        }
        DispatchQueue.main.async { [self] in
            lastResult = (plan.map(\.0.name).joined(separator: "+"), true, 0)
            hud.show("✓ \(plan.count) workflows done", autoHide: 2.5)
            finishOnMain(title: "AX ✓")
        }
    }

    // No file macro matched — let the axstream engine construct/execute it
    // (learned-template store instantly; LLM planning when a key is set).
    private func fallback(_ text: String) {
        DispatchQueue.main.async { [self] in
            statusItem.button?.title = "AX 🧠"
            hud.show("🧠 “\(text)” — no macro, constructing…")
        }
        let outcome = Replay.utterance(text)
        print(String(format: "fallback \"%@\" -> %@ (%.1fs) %@", text,
                     outcome.ok ? "ok" : "no", outcome.seconds, outcome.detail))
        DispatchQueue.main.async { [self] in
            if outcome.ok {
                hud.show(String(format: "✓ %@ · %.1fs", outcome.detail,
                                outcome.seconds), autoHide: 2.5)
            } else {
                notice = "✗ \(outcome.detail.prefix(80))"
                hud.show("✗ \(outcome.detail.prefix(70))", autoHide: 4)
            }
            finishOnMain(title: outcome.ok ? "AX ✓" : "AX")
        }
    }

    private func run(workflow: Workflow, slots: [String: String]) {
        DispatchQueue.main.async { [self] in
            busy = true
            notice = ""
            statusItem.button?.title = "AX ▶"
            let detail = slots.isEmpty ? ""
                : "  ·  " + slots.values.joined(separator: ", ")
            hud.show("▶ \(workflow.name)\(detail)")
        }
        let outcome = Replay.run(workflow: workflow, slots: slots)
        DispatchQueue.main.async { [self] in
            lastResult = (workflow.name, outcome.ok, outcome.seconds)
            if outcome.ok {
                hud.show(String(format: "✓ %@ · %.1fs", workflow.name,
                                outcome.seconds), autoHide: 2)
            } else {
                notice = "! \(workflow.name): \(outcome.detail.prefix(80))"
                hud.show("✗ \(workflow.name) failed", autoHide: 3)
            }
            finishOnMain(title: outcome.ok
                ? String(format: "AX ✓ %.1fs", outcome.seconds) : "AX !")
        }
        print(String(format: "replay %@ %@ in %.1fs",
                     workflow.name, outcome.ok ? "ok" : "FAILED", outcome.seconds))
    }

    private func finish(notice text: String) {
        DispatchQueue.main.async { [self] in
            notice = text
            if text.isEmpty {
                hud.hide()
            } else {
                hud.show(text, autoHide: 3)
            }
            finishOnMain(title: "AX")
        }
    }

    private func finishOnMain(title: String) {
        busy = false
        statusItem.button?.title = title
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { [self] in
            if !busy && !holding { statusItem.button?.title = "AX" }
        }
    }

    // --- menu --------------------------------------------------------------

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let workflows = MacroLibrary.load()

        let header: String
        if busy {
            header = "Working…"
        } else if !notice.isEmpty {
            header = notice
        } else if let last = lastResult {
            header = String(format: "%@ %@ · %.1fs",
                            last.ok ? "✓" : "!", last.name, last.seconds)
        } else {
            header = workflows.isEmpty ? "No workflows recorded yet"
                                       : "Hold ⌃⌥ and speak a workflow"
        }
        menu.addItem(disabled(header))
        if !AXIsProcessTrusted() {
            let fix = NSMenuItem(title: "Enable ⌃⌥ hotkey (open Accessibility)…",
                                 action: #selector(openAccessibility), keyEquivalent: "")
            fix.target = self
            menu.addItem(fix)
        }
        menu.addItem(.separator())

        workflowsByTag.removeAll()
        for (offset, workflow) in workflows.prefix(10).enumerated() {
            let mark = workflow.verified ? "✓" : "○"
            let slots = workflow.requiredSlots.isEmpty ? ""
                : "  {" + workflow.requiredSlots.joined(separator: ", ") + "}"
            let item = NSMenuItem(title: "\(mark) \(workflow.name)\(slots)",
                                  action: #selector(runFromMenu(_:)), keyEquivalent: "")
            item.target = self
            item.tag = 100 + offset
            item.isEnabled = !busy
            item.toolTip = workflow.description.isEmpty ? workflow.whenToUse
                                                        : workflow.description
            workflowsByTag[item.tag] = workflow
            menu.addItem(item)
        }
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit Axstream", action: #selector(quit),
                              keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
    }

    private func disabled(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    @objc private func openAccessibility() {
        let url = URL(string: "x-apple.systempreferences:com.apple.preference"
                              + ".security?Privacy_Accessibility")!
        NSWorkspace.shared.open(url)
    }

    @objc private func runFromMenu(_ sender: NSMenuItem) {
        guard let workflow = workflowsByTag[sender.tag], !busy else { return }
        guard let slots = promptForSlots(workflow) else { return }
        pipeline.async { [self] in run(workflow: workflow, slots: slots) }
    }

    // Small NSAlert with one text field per required slot; example values
    // fill in for anything left blank.
    private func promptForSlots(_ workflow: Workflow) -> [String: String]? {
        let names = workflow.requiredSlots
        if names.isEmpty { return [:] }
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = workflow.name
        alert.informativeText = workflow.description.isEmpty
            ? "Fill in the inputs" : workflow.description
        alert.addButton(withTitle: "Run")
        alert.addButton(withTitle: "Cancel")
        let rowHeight: CGFloat = 56, width: CGFloat = 360
        let view = NSView(frame: NSRect(x: 0, y: 0, width: width,
                                        height: rowHeight * CGFloat(names.count)))
        var fields: [String: NSTextField] = [:]
        for (index, name) in names.enumerated() {
            let spec = workflow.slots[name] ?? [:]
            let y = rowHeight * CGFloat(names.count - index - 1)
            let label = NSTextField(labelWithString: spec["description"] as? String ?? name)
            label.frame = NSRect(x: 0, y: y + 33, width: width, height: 18)
            let field = NSTextField(frame: NSRect(x: 0, y: y + 3, width: width, height: 26))
            if let example = spec["example"] as? String {
                field.placeholderString = example
            }
            view.addSubview(label)
            view.addSubview(field)
            fields[name] = field
        }
        alert.accessoryView = view
        alert.window.initialFirstResponder = fields[names[0]]
        guard alert.runModal() == .alertFirstButtonReturn else { return nil }
        var values: [String: String] = [:]
        for (name, field) in fields {
            var value = field.stringValue.trimmingCharacters(in: .whitespaces)
            if value.isEmpty {
                value = (workflow.slots[name]?["example"] as? String) ?? ""
            }
            if value.isEmpty { return nil }
            values[name] = value
        }
        return values
    }

    @objc private func quit() { NSApp.terminate(nil) }
}
