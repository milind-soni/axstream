import Foundation

// The launcher's view of one .axstream macro file — parsed from the
// JSON header line, actions untouched (the replay engine owns those).
struct Workflow {
    let name: String
    let description: String
    let whenToUse: String
    let slots: [String: [String: Any]]  // slot name -> {description, example}
    let examples: [[String: Any]]       // [{utterance, slots}]
    let verified: Bool
    let voice: Bool  // header "voice": false keeps bench/smoke macros out of
                     // the matcher — sibling noise degrades the small model
    let path: String

    var requiredSlots: [String] { slots.keys.sorted() }
}

enum MacroLibrary {
    static let macrosDir = NSString(string: "~/.axstream/macros").expandingTildeInPath
    static let historyPath = NSString(string: "~/.axstream/runs.jsonl").expandingTildeInPath

    static func load() -> [Workflow] {
        let fm = FileManager.default
        guard let names = try? fm.contentsOfDirectory(atPath: macrosDir) else { return [] }
        var workflows: [Workflow] = []
        for file in names.sorted() where file.hasSuffix(".axstream") {
            let path = (macrosDir as NSString).appendingPathComponent(file)
            guard let text = try? String(contentsOfFile: path, encoding: .utf8),
                  let headerLine = text.split(separator: "\n", maxSplits: 1).first,
                  let data = String(headerLine).data(using: .utf8),
                  let header = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let name = header["name"] as? String
            else { continue }
            workflows.append(Workflow(
                name: name,
                description: header["description"] as? String ?? "",
                whenToUse: header["when_to_use"] as? String ?? "",
                slots: header["slots"] as? [String: [String: Any]] ?? [:],
                examples: header["examples"] as? [[String: Any]] ?? [],
                // Approximation: a present stamp counts as verified; the replay
                // engine still enforces the real hash-checked gate itself.
                verified: header["verified"] is [String: Any],
                voice: (header["voice"] as? Bool) ?? true,
                path: path))
        }
        return ranked(workflows)
    }

    /// The matcher's view: spoken-command macros only.
    static func voiceWorkflows() -> [Workflow] {
        load().filter(\.voice)
    }

    // "Recent" = last run OR last edit, whichever is newer — so a macro
    // recorded a minute ago tops the list even though it has never run.
    // Run history comes from the shared runs.jsonl the replay CLI writes.
    private static func ranked(_ workflows: [Workflow]) -> [Workflow] {
        let iso = ISO8601DateFormatter()
        var lastRun: [String: Date] = [:]
        if let text = try? String(contentsOfFile: historyPath, encoding: .utf8) {
            for line in text.split(separator: "\n") {
                guard let data = line.data(using: .utf8),
                      let row = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                      let macro = row["macro"] as? String,
                      let at = (row["at"] as? String).flatMap(iso.date(from:))
                else { continue }
                lastRun[macro] = max(lastRun[macro] ?? .distantPast, at)
            }
        }
        func recency(_ workflow: Workflow) -> Date {
            var mtime = Date.distantPast
            if let attrs = try? FileManager.default.attributesOfItem(atPath: workflow.path),
               let modified = attrs[.modificationDate] as? Date {
                mtime = modified
            }
            return max(lastRun[workflow.name] ?? .distantPast, mtime)
        }
        return workflows.sorted { a, b in
            let ra = recency(a), rb = recency(b)
            if ra != rb { return ra > rb }
            if a.verified != b.verified { return a.verified }
            return a.name < b.name
        }
    }
}

struct ReplayOutcome {
    let ok: Bool
    let seconds: Double
    let detail: String
}

enum Replay {
    static func axstreamBinary() -> [String] {
        for candidate in ["~/.local/bin/axstream", "/opt/homebrew/bin/axstream",
                          "/usr/local/bin/axstream"] {
            let path = NSString(string: candidate).expandingTildeInPath
            if FileManager.default.isExecutableFile(atPath: path) { return [path] }
        }
        return ["/usr/bin/env", "axstream"]
    }

    // Blocking — call off the main thread. The CLI is the replay engine;
    // this app only owns the voice hot path.
    static func run(workflow: Workflow, slots: [String: String]) -> ReplayOutcome {
        let started = Date()
        let process = Process()
        var command = axstreamBinary()
        command += ["replay", workflow.path]
        if !slots.isEmpty,
           let data = try? JSONSerialization.data(withJSONObject: slots),
           let json = String(data: data, encoding: .utf8) {
            command += ["--slots", json]
        }
        process.executableURL = URL(fileURLWithPath: command[0])
        process.arguments = Array(command.dropFirst())
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
        } catch {
            return ReplayOutcome(ok: false, seconds: 0, detail: "\(error)")
        }
        let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(),
                            encoding: .utf8) ?? ""
        process.waitUntilExit()
        let seconds = Date().timeIntervalSince(started)
        let ok = process.terminationStatus == 0
        let detail = output.split(separator: "\n").last.map(String.init) ?? ""
        // no history append here — the replay CLI records the run itself
        return ReplayOutcome(ok: ok, seconds: seconds, detail: detail)
    }

    // No file-macro matched: hand the raw utterance to the axstream engine —
    // its Session tier matches the (separate) learned-template store
    // instantly, and plans/executes/learns via the LLM tier when an API key
    // is configured. Blocking; call off the main thread.
    static func utterance(_ text: String) -> ReplayOutcome {
        let started = Date()
        let process = Process()
        let command = axstreamBinary() + [text]
        process.executableURL = URL(fileURLWithPath: command[0])
        process.arguments = Array(command.dropFirst())
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
        } catch {
            return ReplayOutcome(ok: false, seconds: 0, detail: "\(error)")
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + 120) {
            if process.isRunning { process.terminate() }
        }
        let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(),
                            encoding: .utf8) ?? ""
        process.waitUntilExit()
        let seconds = Date().timeIntervalSince(started)
        guard let line = output.split(separator: "\n").last,
              let data = line.data(using: .utf8),
              let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else {
            return ReplayOutcome(ok: false, seconds: seconds,
                                 detail: "engine gave no result")
        }
        let tier = result["tier"] as? String ?? "?"
        if tier == "none" {
            return ReplayOutcome(ok: false, seconds: seconds,
                                 detail: "nothing matched and the LLM tier is "
                                 + "off (set OPENROUTER_API_KEY or GROQ_API_KEY)")
        }
        let status = result["status"] as? String ?? "?"
        let what = result["template"] as? String ?? "\(tier) tier"
        if status == "done" {
            return ReplayOutcome(ok: true, seconds: seconds, detail: what)
        }
        let reason = result["reason"] as? String ?? status
        return ReplayOutcome(ok: false, seconds: seconds,
                             detail: "\(what): \(reason)")
    }
}
