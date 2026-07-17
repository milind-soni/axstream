// Raw SSE streaming to Groq (OpenAI-compatible), no SDK. Port of llm.py's
// stream_openai_compat with the same 429 retry ("try again in Xs|Xms") logic,
// plus the Anthropic fallback used when only CLAUDE_API / ANTHROPIC_API_KEY
// is available. Also owns .env key loading (bridge.py load_env_keys).

import Foundation

enum Env {
    private static var loaded = false
    private static let envPath = "/Users/milindsoni/Documents/mywork/axstream/.env"

    /// Load KEY=VALUE lines from the axstream .env into the process env
    /// (never overriding real environment variables). CLAUDE_API is
    /// aliased to ANTHROPIC_API_KEY, matching bridge.py.
    static func load() {
        guard !loaded else { return }
        loaded = true
        guard let content = try? String(contentsOfFile: envPath, encoding: .utf8) else { return }
        for rawLine in content.components(separatedBy: "\n") {
            guard let eq = rawLine.firstIndex(of: "=") else { continue }
            var key = String(rawLine[rawLine.startIndex..<eq]).trimmingCharacters(in: .whitespaces)
            var value = String(rawLine[rawLine.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
            value = value.trimmingCharacters(in: CharacterSet(charactersIn: "'\""))
            if key == "CLAUDE_API" { key = "ANTHROPIC_API_KEY" }
            if !key.isEmpty && !value.isEmpty && ProcessInfo.processInfo.environment[key] == nil {
                setenv(key, value, 0)
            }
        }
    }

    static func get(_ key: String) -> String? {
        load()
        return ProcessInfo.processInfo.environment[key]
    }
}

enum LlmError: Error, CustomStringConvertible {
    case http(Int, String)
    case noApiKey

    var description: String {
        switch self {
        case .http(let code, let body): return "llm \(code): \(body.prefix(500))"
        case .noApiKey: return "no GROQ_API_KEY or ANTHROPIC_API_KEY found (env or axstream/.env)"
        }
    }
}

enum Llm {
    static var model: String { Env.get("AXSTREAM_MODEL") ?? "qwen/qwen3.6-27b" }

    /// Stream text deltas for (system, user). Groq is primary; Anthropic is
    /// used only when no Groq key is available.
    static func stream(system: String, user: String) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    if let groqKey = Env.get("GROQ_API_KEY") {
                        try await streamOpenAICompat(
                            system: system, user: user, apiKey: groqKey,
                            baseURL: "https://api.groq.com/openai/v1", model: model
                        ) { continuation.yield($0) }
                    } else if let anthropicKey = Env.get("ANTHROPIC_API_KEY") {
                        try await streamAnthropic(
                            system: system, user: user, apiKey: anthropicKey
                        ) { continuation.yield($0) }
                    } else {
                        throw LlmError.noApiKey
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: - OpenAI-compatible SSE (Groq)

    private static func streamOpenAICompat(
        system: String, user: String, apiKey: String, baseURL: String, model: String,
        onDelta: (String) -> Void
    ) async throws {
        var payload: [String: Any] = [
            "model": model,
            "max_tokens": 2048,
            "messages": [
                ["role": "system", "content": system],
                ["role": "user", "content": user],
            ],
            "stream": true,
        ]
        // qwen thinks for 0.3-2s before the fence; our plans don't need it.
        // gpt-oss requires at least low effort.
        payload["reasoning_effort"] = model.contains("gpt-oss") ? "low" : "none"
        var request = URLRequest(url: URL(string: "\(baseURL)/chat/completions")!)
        request.httpMethod = "POST"
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        request.httpBody = try JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 120

        for attempt in 0..<4 {
            let (bytes, response) = try await URLSession.shared.bytes(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status == 429 && attempt < 3 {
                var body = ""
                for try await line in bytes.lines { body += line + "\n" }
                let delay = retryAfterSeconds(response: response, body: body)
                try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                continue
            }
            if status != 200 {
                var body = ""
                for try await line in bytes.lines { body += line + "\n" }
                throw LlmError.http(status, body)
            }
            for try await line in bytes.lines {
                guard line.hasPrefix("data:") else { continue }
                let dataStr = String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces)
                if dataStr == "[DONE]" { break }
                guard let data = dataStr.data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let choices = obj["choices"] as? [[String: Any]],
                      let delta = choices.first?["delta"] as? [String: Any],
                      let content = delta["content"] as? String, !content.isEmpty else { continue }
                onDelta(content)
            }
            return
        }
    }

    /// 429 backoff: honor Retry-After, else the "try again in Xs"/"Xms" hint, else 2s.
    private static func retryAfterSeconds(response: URLResponse, body: String) -> Double {
        if let http = response as? HTTPURLResponse,
           let header = http.value(forHTTPHeaderField: "retry-after"),
           let seconds = Double(header) {
            return min(seconds + 0.2, 15.0)
        }
        if let regex = try? NSRegularExpression(pattern: #"try again in ([\d.]+)(m?s)"#),
           let match = regex.firstMatch(in: body, range: NSRange(body.startIndex..., in: body)),
           let numRange = Range(match.range(at: 1), in: body),
           let unitRange = Range(match.range(at: 2), in: body),
           let value = Double(body[numRange]) {
            let seconds = body[unitRange] == "ms" ? value / 1000 : value
            return min(seconds + 0.2, 15.0)
        }
        return 2.0
    }

    // MARK: - Anthropic SSE (fallback)

    private static func streamAnthropic(
        system: String, user: String, apiKey: String,
        onDelta: (String) -> Void
    ) async throws {
        let payload: [String: Any] = [
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 2048,
            "system": system,
            "messages": [["role": "user", "content": user]],
            "stream": true,
        ]
        var request = URLRequest(url: URL(string: "https://api.anthropic.com/v1/messages")!)
        request.httpMethod = "POST"
        request.setValue(apiKey, forHTTPHeaderField: "x-api-key")
        request.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        request.httpBody = try JSONSerialization.data(withJSONObject: payload)
        request.timeoutInterval = 120

        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status != 200 {
            var body = ""
            for try await line in bytes.lines { body += line + "\n" }
            throw LlmError.http(status, body)
        }
        for try await line in bytes.lines {
            guard line.hasPrefix("data:") else { continue }
            let dataStr = String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces)
            guard let data = dataStr.data(using: .utf8),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  obj["type"] as? String == "content_block_delta",
                  let delta = obj["delta"] as? [String: Any],
                  delta["type"] as? String == "text_delta",
                  let text = delta["text"] as? String else { continue }
            onDelta(text)
        }
    }
}
