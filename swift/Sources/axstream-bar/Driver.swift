// MCP JSON-RPC 2.0 client for cua-driver, spoken over Process stdio.
//
// Wire framing (verified empirically against `cua-driver mcp` on this machine):
// newline-delimited JSON -- one JSON-RPC message per line, no Content-Length
// headers. Requests are written as a single JSON line + "\n"; responses arrive
// one per line on stdout.

import Foundation

enum DriverError: Error, CustomStringConvertible {
    case notRunning
    case timeout(String)
    case rpc(String)
    case tool(String, String)  // tool name, error text

    var description: String {
        switch self {
        case .notRunning: return "cua-driver process is not running"
        case .timeout(let method): return "cua-driver request timed out: \(method)"
        case .rpc(let message): return "cua-driver rpc error: \(message)"
        case .tool(let name, let text): return "\(name): \(text.prefix(300))"
        }
    }
}

struct ToolResult {
    let text: String                    // concatenated text content blocks
    let structured: [String: Any]?      // structuredContent, when present
    let isError: Bool
}

final class Driver: @unchecked Sendable {
    private let process = Process()
    private let stdinPipe = Pipe()
    private let stdoutPipe = Pipe()
    private let stderrPipe = Pipe()

    private let lock = NSLock()
    private var pending: [Int: CheckedContinuation<[String: Any], Error>] = [:]
    private var nextId = 1
    private var readBuffer = Data()
    private var started = false

    static let binaryPath = "/Users/milindsoni/.local/bin/cua-driver"
    private static let requestTimeout: TimeInterval = 120

    // MARK: - Lifecycle

    func start() async throws {
        process.executableURL = URL(fileURLWithPath: Self.binaryPath)
        process.arguments = ["mcp"]
        process.standardInput = stdinPipe
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            self?.consume(handle.availableData)
        }
        // pass driver diagnostics through (e.g. the daemon-relaunch notice)
        stderrPipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if !data.isEmpty, let text = String(data: data, encoding: .utf8) {
                FileHandle.standardError.write(Data("[cua-driver] \(text)".utf8))
            }
        }

        try process.run()
        started = true

        _ = try await request(method: "initialize", params: [
            "protocolVersion": "2024-11-05",
            "capabilities": [String: Any](),
            "clientInfo": ["name": "axstream-bar", "version": "0.1.0"],
        ])
        notify(method: "notifications/initialized")
    }

    func stop() {
        stdoutPipe.fileHandleForReading.readabilityHandler = nil
        stderrPipe.fileHandleForReading.readabilityHandler = nil
        if process.isRunning { process.terminate() }
    }

    // MARK: - Tool calls

    /// tools/call. Throws DriverError.tool when the tool reports isError.
    @discardableResult
    func callTool(_ name: String, _ arguments: [String: Any] = [:]) async throws -> ToolResult {
        let result = try await request(method: "tools/call", params: [
            "name": name,
            "arguments": arguments,
        ])
        var text = ""
        if let content = result["content"] as? [[String: Any]] {
            text = content.compactMap { $0["text"] as? String }.joined(separator: "\n")
        }
        let toolResult = ToolResult(
            text: text,
            structured: result["structuredContent"] as? [String: Any],
            isError: result["isError"] as? Bool ?? false
        )
        if toolResult.isError {
            throw DriverError.tool(name, text.isEmpty ? "unknown tool error" : text)
        }
        return toolResult
    }

    // MARK: - JSON-RPC plumbing

    private func request(method: String, params: [String: Any]) async throws -> [String: Any] {
        guard started, process.isRunning else { throw DriverError.notRunning }
        let id: Int = {
            lock.lock(); defer { lock.unlock() }
            let value = nextId
            nextId += 1
            return value
        }()

        return try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            pending[id] = continuation
            lock.unlock()

            // watchdog: never leave a continuation hanging forever
            DispatchQueue.global().asyncAfter(deadline: .now() + Self.requestTimeout) { [weak self] in
                guard let self else { return }
                self.lock.lock()
                let stuck = self.pending.removeValue(forKey: id)
                self.lock.unlock()
                stuck?.resume(throwing: DriverError.timeout(method))
            }

            let message: [String: Any] = [
                "jsonrpc": "2.0", "id": id, "method": method, "params": params,
            ]
            do {
                try write(message)
            } catch {
                lock.lock()
                let cont = pending.removeValue(forKey: id)
                lock.unlock()
                cont?.resume(throwing: error)
            }
        }
    }

    private func notify(method: String, params: [String: Any] = [:]) {
        let message: [String: Any] = ["jsonrpc": "2.0", "method": method, "params": params]
        try? write(message)
    }

    private func write(_ message: [String: Any]) throws {
        var data = try JSONSerialization.data(withJSONObject: message)
        data.append(0x0A)  // newline-delimited framing
        try stdinPipe.fileHandleForWriting.write(contentsOf: data)
    }

    private func consume(_ data: Data) {
        guard !data.isEmpty else { return }
        lock.lock()
        readBuffer.append(data)
        var lines: [Data] = []
        while let newline = readBuffer.firstIndex(of: 0x0A) {
            lines.append(readBuffer.subdata(in: readBuffer.startIndex..<newline))
            readBuffer.removeSubrange(readBuffer.startIndex...newline)
        }
        lock.unlock()
        for line in lines where !line.isEmpty {
            dispatch(line)
        }
    }

    private func dispatch(_ line: Data) {
        guard let parsed = try? JSONSerialization.jsonObject(with: line),
              let message = parsed as? [String: Any] else { return }
        guard let id = message["id"] as? Int else { return }  // server notifications: ignore

        lock.lock()
        let continuation = pending.removeValue(forKey: id)
        lock.unlock()
        guard let continuation else { return }

        if let error = message["error"] as? [String: Any] {
            let text = (error["message"] as? String) ?? "unknown error"
            continuation.resume(throwing: DriverError.rpc(text))
        } else if let result = message["result"] as? [String: Any] {
            continuation.resume(returning: result)
        } else {
            continuation.resume(returning: [:])
        }
    }
}
