// Port of axstream/spec.py + compiler.py: the JSONL action spec and the
// newline-committed stream compiler. Ops are kept as loose [String: Any]
// dictionaries (JSONSerialization) to mirror the Python semantics exactly.

import Foundation

enum Spec {
    // do-name -> (required fields, optional fields, default risk)
    static let actions: [String: (required: Set<String>, optional: Set<String>, risk: String)] = [
        "click": (["target"], ["risk"], "safe"),
        "double_click": (["target"], ["risk"], "safe"),
        "type": (["text"], ["risk"], "safe"),
        "key": (["keys"], ["risk"], "safe"),
        "scroll": (["direction"], ["clicks", "risk"], "safe"),
        "move": (["target"], ["risk"], "safe"),
        "open": (["target"], ["risk"], "safe"),
        "wait": (["ms"], [], "safe"),
    ]

    static let ops: Set<String> = ["act", "assert", "observe", "done"]

    /// Cheap structural validation of a parsed line. Returns (ok, error, normalizedOp).
    /// The op-shorthand alias {"op":"click",...} -> {"op":"act","do":"click",...} is
    /// applied to the returned dictionary.
    static func validate(_ input: [String: Any]) -> (ok: Bool, err: String, op: [String: Any]) {
        var obj = input
        // models naturally shorten {"op":"act","do":X,...} to {"op":X,...}; accept it
        if let op = obj["op"] as? String, actions[op] != nil, obj["do"] == nil {
            obj["do"] = op
            obj["op"] = "act"
        }
        guard let op = obj["op"] as? String, ops.contains(op) else {
            return (false, "unknown op: \(String(describing: obj["op"]))", obj)
        }
        if op == "act" {
            guard let doName = obj["do"] as? String, let spec = actions[doName] else {
                return (false, "unknown action: \(String(describing: obj["do"]))", obj)
            }
            let missing = spec.required.subtracting(obj.keys)
            if !missing.isEmpty {
                return (false, "\(doName): missing \(missing.sorted())", obj)
            }
            if spec.required.contains("target"), !validTarget(obj["target"], doName: doName) {
                return (false, "\(doName): bad target \(String(describing: obj["target"]))", obj)
            }
        }
        if op == "assert", !validTarget(obj["target"], doName: "assert") {
            return (false, "assert: bad target \(String(describing: obj["target"]))", obj)
        }
        if op == "done" {
            let status = obj["status"] as? String
            if status != "success" && status != "failure" {
                return (false, "done: bad status \(String(describing: obj["status"]))", obj)
            }
        }
        return (true, "", obj)
    }

    private static func validTarget(_ target: Any?, doName: String) -> Bool {
        if doName == "open" {
            if let s = target as? String { return !s.isEmpty }
            return false
        }
        guard let dict = target as? [String: Any] else { return false }
        if dict["x"] != nil && dict["y"] != nil {
            return dict["x"] is NSNumber && dict["y"] is NSNumber
        }
        if let ax = dict["ax"] as? [String: Any] {
            func nonEmpty(_ key: String) -> Bool {
                if let s = ax[key] as? String { return !s.isEmpty }
                return false
            }
            return nonEmpty("id") || nonEmpty("role") || nonEmpty("title")
        }
        return false
    }

    static func riskOf(_ op: [String: Any]) -> String {
        guard op["op"] as? String == "act" else { return "safe" }
        let defaultRisk = actions[op["do"] as? String ?? ""]?.risk ?? "safe"
        return op["risk"] as? String ?? defaultRisk
    }
}

// MARK: - Stream compiler

enum CompilerEvent {
    case action([String: Any])          // a validated op, ready to execute
    case text(String)                   // narration outside the ```spec fence
    case invalid(line: String, error: String)  // a fence line that failed parse/validation
}

/// Newline-committed stream compiler. Adapted from json-render's
/// createSpecStreamCompiler with one deliberate divergence: NO dedup of
/// identical lines -- clicking the same button twice is a legitimate plan.
final class StreamCompiler {
    private var buffer = ""
    private var inFence: Bool
    private let fenced: Bool
    private static let fenceClose = "```"

    init(fenced: Bool = true) {
        self.fenced = fenced
        self.inFence = !fenced  // unfenced mode treats the whole stream as spec lines
    }

    func push(_ chunk: String) -> [CompilerEvent] {
        buffer += chunk
        var lines = buffer.components(separatedBy: "\n")
        buffer = lines.removeLast()  // keep the incomplete tail buffered
        return lines.flatMap { line($0) }
    }

    /// Flush the trailing buffered line at end of stream.
    func finish() -> [CompilerEvent] {
        defer { buffer = "" }
        if !buffer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return line(buffer)
        }
        return []
    }

    private func line(_ raw: String) -> [CompilerEvent] {
        let stripped = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if stripped.isEmpty { return [] }
        if fenced {
            // be liberal in what we accept: models label the fence ```spec,
            // ```jsonl, ```json, or nothing at all
            if !inFence && stripped.hasPrefix("```") {
                inFence = true
                return []
            }
            if stripped == Self.fenceClose && inFence {
                inFence = false
                return []
            }
        }
        if !inFence {
            return [.text(stripped)]
        }
        if !stripped.hasPrefix("{") {
            return [.invalid(line: stripped, error: "not a JSON object")]
        }
        guard let data = stripped.data(using: .utf8),
              let parsed = try? JSONSerialization.jsonObject(with: data),
              let obj = parsed as? [String: Any] else {
            return [.invalid(line: stripped, error: "parse error")]
        }
        let (ok, err, op) = Spec.validate(obj)
        return ok ? [.action(op)] : [.invalid(line: stripped, error: err)]
    }
}
