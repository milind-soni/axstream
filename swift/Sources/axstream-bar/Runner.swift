// Burst loop: observe -> stream LLM through the compiler -> execute ops as
// lines complete (execution overlaps decode) -> repeat on {"op":"observe"}.
// Port of runner.py + the pipelined half of executor.py.

import Foundation

// MARK: - System prompt (ported VERBATIM from prompt.py)

let SYSTEM = """
You control a macOS computer by streaming actions as JSONL inside a ```spec fence.

OUTPUT FORMAT
- Think briefly in plain text if needed, then open a ```spec fence and emit
  ONE JSON object per line. Each line is executed THE MOMENT it is complete,
  while you are still generating -- so order lines exactly as they must run,
  and never emit an action you are not yet sure about.
- End the fence with ``` only after a {"op":"observe"} or {"op":"done",...} line.

ACTIONS (op="act")
{"op":"act","do":"click","target":T}            click an element
{"op":"act","do":"double_click","target":T}
{"op":"act","do":"type","text":"..."}           type into the focused field
{"op":"act","do":"key","keys":["cmd","s"]}      key or shortcut (keys: list)
{"op":"act","do":"scroll","direction":"down","clicks":3}   up|down|left|right
{"op":"act","do":"move","target":T}
{"op":"act","do":"open","target":"Safari"}      app name or URL
{"op":"act","do":"wait","ms":300}               small settle pause

TARGETS T
{"ax":{"id":"e12"}}                    element id from the OBSERVATION below (preferred)
{"ax":{"role":"AXButton","title":"Save"}}   resolved against the LIVE tree at run time
{"x":420,"y":312}                      raw coordinates, last resort

CONTROL
{"op":"assert","target":T}             abort burst if the element is missing
{"op":"observe"}                       stop; you'll be re-prompted with a fresh observation
{"op":"done","status":"success"}       task complete ("failure" + "reason" if stuck)

RULES
1. PLAN THE ENTIRE TASK IN ONE FENCE. You know how standard macOS apps behave:
   after "open", keyboard actions are delivered to the opened app, so
   open -> wait -> shortcuts -> type works blind. Do the whole job now.
2. {"op":"observe"} is a LAST RESORT -- only when the next action depends on
   content you cannot know (search results, a dialog's options, unknown page).
   Never observe just to "check" or "confirm"; the executor verifies targets.
3. Prefer keyboard shortcuts over clicks (cmd+n new, cmd+t tab, cmd+l address
   bar, cmd+s save, enter submit). They are faster and never miss.
4. After {"do":"open"}, add {"do":"wait","ms":500} before acting on the app.
5. Only reference element ids that appear in the observation.
6. Before typing into a visible field, click it first. Split long text into
   ~60-char {"do":"type"} lines so typing starts while you generate.
7. Mark destructive or hard-to-undo actions with "risk":"risky"
   (submitting forms, deleting, sending, purchasing).
8. If the task is already complete, emit done immediately.
9. No prose. Open the fence as your very first output.

EXAMPLE (task: "open Notes and write hi") -- one fence, no observe:
```spec
{"op":"act","do":"open","target":"Notes"}
{"op":"act","do":"wait","ms":500}
{"op":"act","do":"key","keys":["cmd","n"]}
{"op":"act","do":"type","text":"hi"}
{"op":"done","status":"success"}
```

EXAMPLE needing observe (task: "click the first search result"):
```spec
{"op":"act","do":"key","keys":["cmd","l"]}
{"op":"act","do":"type","text":"weather tokyo\\n"}
{"op":"act","do":"wait","ms":800}
{"op":"observe"}
```

"""

func buildUser(task: String, observation: String, history: String = "") -> String {
    var parts = ["TASK: \(task)"]
    if !history.isEmpty {
        parts.append("PROGRESS SO FAR:\n\(history)")
    }
    parts.append("OBSERVATION (accessibility tree):\n\(observation)")
    parts.append("Respond with your action stream now.")
    return parts.joined(separator: "\n\n")
}

// MARK: - Runner events (drive the bar UI)

enum RunnerEvent {
    case burstStart(index: Int, elements: Int, obsMs: Double)
    case narration(String)
    case invalidLine(line: String, error: String)
    case executed(op: [String: Any], ms: Double)
    case actionFailed(op: [String: Any], error: String)
    case observeRequested
    case done(status: String, reason: String)
    case aborted(reason: String)
}

enum BurstStatus {
    case done(String, String)  // status, reason
    case observe
    case aborted(String)
    case streamEnd
}

enum Runner {
    static let maxBursts = 6
    static let allowRisky = true

    static func run(
        driver: Driver,
        task: String,
        onEvent: @escaping @Sendable (RunnerEvent) -> Void
    ) async throws {
        var historyLines: [String] = []

        for burstIndex in 0..<maxBursts {
            try Task.checkCancellation()
            let tObs = Date()
            let observation = try await Observer.observe(driver: driver)
            let obsMs = Date().timeIntervalSince(tObs) * 1000
            onEvent(.burstStart(index: burstIndex, elements: observation.elements.count, obsMs: obsMs))

            let user = buildUser(
                task: task,
                observation: observation.summary,
                history: historyLines.suffix(20).joined(separator: "\n"))
            let executor = OpExecutor(driver: driver, observation: observation)
            let status = try await runBurst(
                executor: executor,
                stream: Llm.stream(system: SYSTEM, user: user),
                historyLines: &historyLines,
                onEvent: onEvent)

            switch status {
            case .done(let doneStatus, let reason):
                onEvent(.done(status: doneStatus, reason: reason))
                return
            case .aborted(let reason):
                onEvent(.aborted(reason: reason))
                return
            case .observe, .streamEnd:
                continue  // loop with a fresh observation
            }
        }
    }

    /// Pipelined burst: a producer task feeds LLM chunks through the
    /// StreamCompiler into an AsyncStream while this function executes the
    /// resulting ops in order -- actions run while the stream continues.
    private static func runBurst(
        executor: OpExecutor,
        stream: AsyncThrowingStream<String, Error>,
        historyLines: inout [String],
        onEvent: @escaping @Sendable (RunnerEvent) -> Void
    ) async throws -> BurstStatus {
        enum PipeEvent {
            case compiler(CompilerEvent)
            case streamError(String)
        }

        let (events, continuation) = AsyncStream.makeStream(of: PipeEvent.self)
        let producer = Task {
            let compiler = StreamCompiler(fenced: true)
            do {
                for try await chunk in stream {
                    for event in compiler.push(chunk) {
                        continuation.yield(.compiler(event))
                    }
                }
                for event in compiler.finish() {
                    continuation.yield(.compiler(event))
                }
            } catch is CancellationError {
                // cancelled mid-stream: just close
            } catch {
                continuation.yield(.streamError(String(describing: error)))
            }
            continuation.finish()
        }
        defer { producer.cancel() }

        for await pipeEvent in events {
            try Task.checkCancellation()
            switch pipeEvent {
            case .streamError(let message):
                return .aborted("llm stream failed: \(message.prefix(200))")

            case .compiler(.text(let line)):
                onEvent(.narration(line))

            case .compiler(.invalid(let line, let error)):
                onEvent(.invalidLine(line: line, error: error))

            case .compiler(.action(var op)):
                let opKind = op["op"] as? String
                if opKind == "observe" {
                    onEvent(.observeRequested)
                    return .observe
                }
                if opKind == "done" {
                    return .done(op["status"] as? String ?? "success",
                                 op["reason"] as? String ?? "")
                }
                if opKind == "assert" {
                    let target = op["target"] as? [String: Any] ?? [:]
                    let ok = (try? await executor.assertResolves(target)) ?? false
                    if !ok {
                        return .aborted("assert failed: \(String(describing: target))")
                    }
                    continue
                }

                // op == "act"
                if Spec.riskOf(op) == "risky" && !allowRisky {
                    return .aborted("risky action blocked by policy")
                }
                // record what an ax target resolved to, for history/chips
                if let target = op["target"] as? [String: Any],
                   let ax = target["ax"] as? [String: Any],
                   let resolved = Observer.resolve(ax: ax, in: executor.observation.elements) {
                    op["resolved"] = "\(resolved.role) '\(resolved.title)'"
                }
                let tStart = Date()
                do {
                    try await executor.execute(op)
                } catch is CancellationError {
                    throw CancellationError()
                } catch {
                    let message = String(describing: error)
                    onEvent(.actionFailed(op: op, error: message))
                    historyLines.append("FAILED \(opString(op)): \(message)")
                    return .aborted("\(op["do"] as? String ?? "?"): \(message)")
                }
                let ms = Date().timeIntervalSince(tStart) * 1000
                onEvent(.executed(op: op, ms: ms))
                historyLines.append("did \(opString(op))")
            }
        }
        return .streamEnd
    }
}

/// Run `operation` with a wall-clock deadline; cancels it on timeout.
func withTimeout<T: Sendable>(
    seconds: TimeInterval,
    operation: @escaping @Sendable () async throws -> T
) async throws -> T {
    try await withThrowingTaskGroup(of: T.self) { group in
        group.addTask { try await operation() }
        group.addTask {
            try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            throw CancellationError()
        }
        guard let result = try await group.next() else { throw CancellationError() }
        group.cancelAll()
        return result
    }
}
