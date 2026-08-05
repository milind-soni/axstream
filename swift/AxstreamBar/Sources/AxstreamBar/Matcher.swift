import Foundation

// Port of axstream/tiny.py — same prompt text, same JSON-schema constrained
// decoding, same verbatim-slot guard. The fine-tuned LFM2.5-350M behind
// llama-server keys on this exact prompt format; keep them in lockstep.
struct MatchHit {
    let template: String
    let slots: [String: String]
}

struct MatchTemplate {
    let id: String
    let description: String
    let slots: [String]
    let examples: [[String: Any]]  // [{utterance, slots}]

    static func from(_ workflow: Workflow) -> MatchTemplate {
        var examples = workflow.examples
        if examples.isEmpty {
            // The fine-tuned matcher answers "none" for example-less
            // templates, so synthesize one few-shot from the header.
            var slots: [String: String] = [:]
            for name in workflow.requiredSlots {
                slots[name] = (workflow.slots[name]?["example"] as? String) ?? name
            }
            let hint = (workflow.whenToUse.isEmpty ? workflow.description
                        : workflow.whenToUse).lowercased()
            let utterance = ([hint] + workflow.requiredSlots.compactMap { slots[$0] })
                .joined(separator: " ")
            examples = [["utterance": utterance, "slots": slots]]
        }
        return MatchTemplate(
            id: workflow.name,
            description: workflow.description.isEmpty
                ? (workflow.whenToUse.isEmpty ? workflow.name : workflow.whenToUse)
                : workflow.description,
            slots: workflow.requiredSlots,
            examples: examples)
    }
}

final class Matcher {
    let baseURL: String
    private let session: URLSession

    init(baseURL: String? = nil) {
        self.baseURL = baseURL
            ?? ProcessInfo.processInfo.environment["AXSTREAM_TINY_URL"]
            ?? "http://localhost:8791"
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 5
        session = URLSession(configuration: config)
    }

    func available() -> Bool {
        guard let url = URL(string: "\(baseURL)/health") else { return false }
        var ok = false
        let done = DispatchSemaphore(value: 0)
        session.dataTask(with: url) { _, response, _ in
            ok = (response as? HTTPURLResponse)?.statusCode == 200
            done.signal()
        }.resume()
        done.wait()
        return ok
    }

    func match(_ utterance: String, templates: [MatchTemplate]) -> MatchHit? {
        guard !templates.isEmpty,
              let url = URL(string: "\(baseURL)/v1/chat/completions") else { return nil }
        // The payload is hand-built: llama-server compiles the JSON schema
        // into a grammar that ENFORCES property order, and the fine-tune was
        // trained on {"template": ..., "slots": ...}. Swift dictionaries
        // randomize key order per process, which flipped matches to "none"
        // whenever "slots" serialized first — so no JSONSerialization here.
        let payload = "{\"messages\": ["
            + "{\"role\": \"system\", \"content\": \(json(buildPrompt(templates)))}, "
            + "{\"role\": \"user\", \"content\": \(json(utterance))}], "
            + "\"response_format\": {\"type\": \"json_schema\", "
            + "\"json_schema\": {\"schema\": \(buildSchemaJSON(templates))}}, "
            + "\"max_tokens\": 120, \"temperature\": 0}"
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = payload.data(using: .utf8)

        var content: String?
        let done = DispatchSemaphore(value: 0)
        session.dataTask(with: request) { data, response, _ in
            defer { done.signal() }
            guard (response as? HTTPURLResponse)?.statusCode == 200, let data,
                  let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let choices = body["choices"] as? [[String: Any]],
                  let message = choices.first?["message"] as? [String: Any]
            else { return }
            content = message["content"] as? String
        }.resume()
        done.wait()

        guard let content, let data = content.data(using: .utf8),
              let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let template = result["template"] as? String, template != "none"
        else { return nil }
        var slots: [String: String] = [:]
        for (key, value) in result["slots"] as? [String: Any] ?? [:] {
            slots[key] = "\(value)"
        }
        // The matcher's contract: every slot value is copied verbatim from
        // the utterance — anything else was hallucinated; reject the match.
        let low = utterance.lowercased()
        for value in slots.values where
            !low.contains(value.trimmingCharacters(in: .whitespaces).lowercased()) {
            return nil
        }
        return MatchHit(template: template, slots: slots)
    }

    // --- prompt/schema construction (verbatim port of tiny.py) ------------

    private func json(_ object: Any) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: object,
                                                     options: [.fragmentsAllowed]),
              let text = String(data: data, encoding: .utf8) else { return "\"\"" }
        return text
    }

    func buildPrompt(_ templates: [MatchTemplate]) -> String {
        var lines = [
            "Match the user's spoken command to ONE known template and extract its "
            + "slot values exactly as spoken.",
            "",
            "TEMPLATES:",
        ]
        for t in templates {
            let slotDesc = t.slots.isEmpty ? "no slots" : t.slots.joined(separator: ", ")
            lines.append("- \(t.id) — \(t.description) (slots: \(slotDesc))")
            for example in t.examples.prefix(5) {
                guard let utterance = example["utterance"] as? String else { continue }
                let slots = example["slots"] as? [String: Any] ?? [:]
                let slotPairs = slots.keys.sorted()
                    .map { "\(json($0)): \(json("\(slots[$0] ?? "")"))" }
                    .joined(separator: ", ")
                let expected = "{\"template\": \(json(t.id)), \"slots\": {\(slotPairs)}}"
                lines.append("  Example: \"\(utterance)\" -> \(expected)")
            }
        }
        lines.append(
            "- none — the command fits NO template above. Example: \"what time is it\" "
            + "-> {\"template\":\"none\",\"slots\":{}}")
        lines.append("")
        lines.append(
            "Rules: slot values are copied verbatim from the command. If unsure, "
            + "use \"none\". Output JSON only.")
        return lines.joined(separator: "\n")
    }

    // Ordered by hand for the same reason as the payload: property order in
    // the schema becomes token order in the grammar-constrained output.
    func buildSchemaJSON(_ templates: [MatchTemplate]) -> String {
        var branches: [String] = []
        for t in templates {
            let slotProps = t.slots
                .map { "\(json($0)): {\"type\": \"string\"}" }
                .joined(separator: ", ")
            let required = t.slots.map { json($0) }.joined(separator: ", ")
            branches.append(
                "{\"type\": \"object\", \"properties\": {"
                + "\"template\": {\"const\": \(json(t.id))}, "
                + "\"slots\": {\"type\": \"object\", "
                + "\"properties\": {\(slotProps)}, "
                + "\"required\": [\(required)], "
                + "\"additionalProperties\": false}}, "
                + "\"required\": [\"template\", \"slots\"], "
                + "\"additionalProperties\": false}")
        }
        branches.append(
            "{\"type\": \"object\", \"properties\": {"
            + "\"template\": {\"const\": \"none\"}, "
            + "\"slots\": {\"type\": \"object\", \"additionalProperties\": false}}, "
            + "\"required\": [\"template\", \"slots\"], "
            + "\"additionalProperties\": false}")
        return "{\"oneOf\": [\(branches.joined(separator: ", "))]}"
    }
}
