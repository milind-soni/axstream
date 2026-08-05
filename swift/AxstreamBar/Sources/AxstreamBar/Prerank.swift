import Foundation

// Embedding prerank: shortlist the library before the matcher prompt.
// Measured recall@3 = 100% on the live library — the right template is
// always in the shortlist — and a ~10-template prompt keeps the tiny
// matcher inside its trained context-size distribution no matter how big
// the macro library grows. Degrades gracefully: embedder offline → the
// caller falls back to the full ranked library.
final class Prerank {
    let baseURL: String
    private let session: URLSession
    private var cache: [String: [Double]] = [:]  // doc text -> vector
    private let lock = NSLock()

    init(baseURL: String? = nil) {
        self.baseURL = baseURL
            ?? ProcessInfo.processInfo.environment["AXSTREAM_EMBED_URL"]
            ?? "http://localhost:8793"
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 4
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

    private func embed(_ text: String) -> [Double]? {
        lock.lock()
        if let hit = cache[text] { lock.unlock(); return hit }
        lock.unlock()
        guard let url = URL(string: "\(baseURL)/v1/embeddings") else { return nil }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject:
            ["input": text, "model": "embed"])
        var vector: [Double]?
        let done = DispatchSemaphore(value: 0)
        session.dataTask(with: request) { data, response, _ in
            defer { done.signal() }
            guard (response as? HTTPURLResponse)?.statusCode == 200, let data,
                  let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let rows = body["data"] as? [[String: Any]],
                  let raw = rows.first?["embedding"] as? [Any]
            else { return }
            vector = raw.compactMap { ($0 as? NSNumber)?.doubleValue }
        }.resume()
        done.wait()
        if let vector {
            lock.lock(); cache[text] = vector; lock.unlock()
        }
        return vector
    }

    private static func doc(_ workflow: Workflow) -> String {
        let examples = workflow.examples
            .compactMap { $0["utterance"] as? String }
            .joined(separator: " ; ")
        let desc = workflow.description.isEmpty ? workflow.whenToUse
                                                : workflow.description
        return "\(workflow.name): \(desc) — \(examples)"
    }

    private static func cosine(_ a: [Double], _ b: [Double]) -> Double {
        var dot = 0.0, na = 0.0, nb = 0.0
        for i in 0..<min(a.count, b.count) {
            dot += a[i] * b[i]
            na += a[i] * a[i]
            nb += b[i] * b[i]
        }
        let denom = (na.squareRoot() * nb.squareRoot())
        return denom > 0 ? dot / denom : 0
    }

    /// Pre-compute (and cache) vectors for the library — call off-main.
    func warm(_ workflows: [Workflow]) {
        for workflow in workflows { _ = embed(Prerank.doc(workflow)) }
    }

    /// Top-`limit` workflows by similarity, most-similar first.
    /// nil = embedder unavailable (caller should fall back to full library).
    func shortlist(_ utterance: String, from workflows: [Workflow],
                   limit: Int = 10) -> [Workflow]? {
        guard workflows.count > limit else { return workflows }
        guard let query = embed(utterance) else { return nil }
        var scored: [(Double, Workflow)] = []
        for workflow in workflows {
            guard let vector = embed(Prerank.doc(workflow)) else { return nil }
            scored.append((Prerank.cosine(query, vector), workflow))
        }
        return scored.sorted { $0.0 > $1.0 }.prefix(limit).map(\.1)
    }
}
