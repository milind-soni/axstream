// Eyes + hands over cua-driver.
//
// Observation (port of ax.py Snapshot against cua-driver's tool shapes):
//   get_accessibility_tree -> apps + visible windows in z-order; the first
//   window owned by a regular app is treated as frontmost. get_window_state
//   {pid, window_id} then yields structured elements; we filter to
//   interactable roles, assign per-burst ids e0.., and keep a map
//   id -> (pid, window_id, element_index).
//
// Execution (port of executor.py against cua-driver tools):
//   click/double_click -> element_index form for ax targets, coordinate form
//   for raw x/y; type -> type_text (AXSelectedText insert); key -> press_key /
//   hotkey; open -> launch_app (+ bring_to_front); scroll -> scroll;
//   move -> move_cursor; wait -> Task.sleep.

import Foundation

struct ObservedElement {
    let id: String
    let pid: Int
    let windowId: Int
    let elementIndex: Int
    let role: String
    let title: String
    let value: String
    let frame: CGRect?

    var center: CGPoint? {
        guard let frame else { return nil }
        return CGPoint(x: frame.midX, y: frame.midY)
    }
}

struct Observation {
    let summary: String
    let elements: [ObservedElement]
    let byId: [String: ObservedElement]
    let frontPid: Int
    let frontWindowId: Int
    let frontAppName: String
}

private let interactableRoles: Set<String> = [
    "AXButton", "AXTextField", "AXTextArea", "AXSearchField", "AXCheckBox",
    "AXRadioButton", "AXPopUpButton", "AXComboBox", "AXMenuItem",
    "AXMenuBarItem", "AXMenuButton", "AXLink", "AXTab", "AXSlider",
    "AXIncrementor", "AXDisclosureTriangle", "AXCell", "AXRow",
]

enum ObserveError: Error, CustomStringConvertible {
    case noWindows
    var description: String { "no visible app windows to observe" }
}

enum Observer {
    static func observe(driver: Driver) async throws -> Observation {
        let tree = try await driver.callTool("get_accessibility_tree")
        let apps = (tree.structured?["apps"] as? [[String: Any]]) ?? []
        let windows = (tree.structured?["windows"] as? [[String: Any]]) ?? []
        let appPids = Set(apps.compactMap { ($0["pid"] as? NSNumber)?.intValue })

        // windows arrive in z-order; frontmost = first window owned by a
        // regular app (skips system overlays like universalAccessAuthWarn)
        guard let front = windows.first(where: { win in
            guard let pid = (win["pid"] as? NSNumber)?.intValue else { return false }
            return appPids.contains(pid)
        }) ?? windows.first,
            let frontPid = (front["pid"] as? NSNumber)?.intValue,
            let frontWindowId = (front["window_id"] as? NSNumber)?.intValue
        else { throw ObserveError.noWindows }
        let frontAppName = (front["app_name"] as? String) ?? "app"
        let frontTitle = (front["title"] as? String) ?? ""

        let elements = try await windowElements(
            driver: driver, pid: frontPid, windowId: frontWindowId)

        var lines: [String] = []
        let appNames = apps.compactMap { app -> String? in
            guard let name = app["name"] as? String else { return nil }
            let pid = (app["pid"] as? NSNumber)?.intValue
            return pid == frontPid ? "\(name) (frontmost)" : name
        }
        lines.append("APPS: " + appNames.joined(separator: ", "))
        let otherWindows = windows.dropFirst().prefix(6).compactMap { $0["app_name"] as? String }
        if !otherWindows.isEmpty {
            lines.append("OTHER VISIBLE WINDOWS: " + otherWindows.joined(separator: ", "))
        }
        let titlePart = frontTitle.isEmpty ? "" : " '\(frontTitle)'"
        lines.append("# \(frontAppName)\(titlePart) (frontmost window)")
        let maxElements = 300
        for element in elements.prefix(maxElements) {
            let label = element.title.isEmpty ? (element.value.isEmpty ? "?" : element.value) : element.title
            var line = "\(element.id) \(element.role) '\(label)'"
            if !element.value.isEmpty && element.value != label {
                line += " value='\(element.value)'"
            }
            lines.append(line)
        }
        if elements.count > maxElements {
            lines.append("... \(elements.count - maxElements) more elements omitted")
        }

        return Observation(
            summary: lines.joined(separator: "\n"),
            elements: Array(elements.prefix(maxElements)),
            byId: Dictionary(uniqueKeysWithValues: elements.prefix(maxElements).map { ($0.id, $0) }),
            frontPid: frontPid,
            frontWindowId: frontWindowId,
            frontAppName: frontAppName
        )
    }

    static func windowElements(driver: Driver, pid: Int, windowId: Int) async throws -> [ObservedElement] {
        let state = try await driver.callTool("get_window_state", [
            "pid": pid, "window_id": windowId, "include_screenshot": false,
        ])
        let rawElements = (state.structured?["elements"] as? [[String: Any]]) ?? []
        var kept: [ObservedElement] = []
        for raw in rawElements {
            guard let role = raw["role"] as? String,
                  let index = (raw["element_index"] as? NSNumber)?.intValue else { continue }
            let title = (raw["label"] as? String) ?? ""
            let value = valueString(raw["value"])
            let isStatic = role == "AXStaticText" && (!title.isEmpty || !value.isEmpty)
            guard interactableRoles.contains(role) || isStatic else { continue }
            var frame: CGRect?
            if let f = raw["frame"] as? [String: Any],
               let x = (f["x"] as? NSNumber)?.doubleValue,
               let y = (f["y"] as? NSNumber)?.doubleValue {
                let w = (f["w"] as? NSNumber)?.doubleValue ?? 0
                let h = (f["h"] as? NSNumber)?.doubleValue ?? 0
                frame = CGRect(x: x, y: y, width: w, height: h)
            }
            kept.append(ObservedElement(
                id: "e\(kept.count)", pid: pid, windowId: windowId,
                elementIndex: index, role: role, title: title, value: value,
                frame: frame
            ))
        }
        return kept
    }

    private static func valueString(_ value: Any?) -> String {
        if let s = value as? String { return s.trimmingCharacters(in: .whitespaces) }
        if let n = value as? NSNumber { return n.stringValue }
        return ""
    }

    /// Port of Snapshot.resolve_element's fuzzy role/title scoring.
    static func resolve(ax: [String: Any], in elements: [ObservedElement]) -> ObservedElement? {
        if let id = ax["id"] as? String, !id.isEmpty {
            return elements.first { $0.id == id }
        }
        let role = ax["role"] as? String
        let title = ((ax["title"] as? String) ?? "").lowercased()
        var best: (score: Int, element: ObservedElement?) = (0, nil)
        for element in elements {
            var score = 0
            if let role, !role.isEmpty {
                if element.role != role { continue }
                score += 1
            }
            if !title.isEmpty {
                let hay = "\(element.title) \(element.value)".lowercased()
                if title == element.title.lowercased() {
                    score += 4
                } else if hay.contains(title) {
                    score += 2
                } else {
                    continue
                }
            }
            if score > best.score { best = (score, element) }
        }
        return best.element
    }
}

// MARK: - Op execution

enum ExecError: Error, CustomStringConvertible {
    case unresolvedTarget(String)
    case badOp(String)

    var description: String {
        switch self {
        case .unresolvedTarget(let detail): return "could not resolve target \(detail)"
        case .badOp(let detail): return "bad op: \(detail)"
        }
    }
}

final class OpExecutor {
    private let driver: Driver
    private(set) var observation: Observation

    init(driver: Driver, observation: Observation) {
        self.driver = driver
        self.observation = observation
    }

    func execute(_ op: [String: Any]) async throws {
        guard let doName = op["do"] as? String else {
            throw ExecError.badOp(String(describing: op))
        }
        switch doName {
        case "wait":
            let ms = (op["ms"] as? NSNumber)?.doubleValue ?? 300
            try await Task.sleep(nanoseconds: UInt64(ms * 1_000_000))

        case "type":
            let text = op["text"] as? String ?? ""
            try await driver.callTool("type_text", [
                "pid": observation.frontPid, "text": text,
            ])

        case "key":
            var keys: [String] = []
            if let list = op["keys"] as? [String] { keys = list }
            else if let single = op["keys"] as? String { keys = [single] }
            guard !keys.isEmpty else { throw ExecError.badOp("key with no keys") }
            if keys.count == 1 {
                try await driver.callTool("press_key", [
                    "pid": observation.frontPid, "key": keys[0],
                ])
            } else {
                try await driver.callTool("hotkey", [
                    "pid": observation.frontPid, "keys": keys,
                ])
            }

        case "scroll":
            let direction = op["direction"] as? String ?? "down"
            let clicks = (op["clicks"] as? NSNumber)?.intValue ?? 1
            try await driver.callTool("scroll", [
                "pid": observation.frontPid,
                "direction": direction,
                "amount": max(1, min(50, clicks)),
            ])

        case "open":
            try await open(op["target"] as? String ?? "")

        case "click", "double_click", "move":
            try await pointerAction(doName, op: op)

        default:
            throw ExecError.badOp("unhandled action: \(doName)")
        }
    }

    /// launch_app in the background, then bring_to_front: an "open X" voice
    /// command implies the user wants to see the app.
    private func open(_ target: String) async throws {
        var args: [String: Any]
        let isURL = target.contains("://") || target.hasPrefix("www.")
        if isURL {
            let url = target.contains("://") ? target : "https://\(target)"
            args = ["name": "Safari", "urls": [url]]
        } else {
            args = ["name": target]
        }
        let result = try await driver.callTool("launch_app", args)
        if let pid = (result.structured?["pid"] as? NSNumber)?.intValue {
            _ = try? await driver.callTool("bring_to_front", ["pid": pid])
        }
    }

    private func pointerAction(_ doName: String, op: [String: Any]) async throws {
        guard let target = op["target"] as? [String: Any] else {
            throw ExecError.badOp("\(doName) with no target")
        }

        // raw coordinate target
        if let x = (target["x"] as? NSNumber)?.doubleValue,
           let y = (target["y"] as? NSNumber)?.doubleValue {
            switch doName {
            case "click":
                // no pid/window: desktop scope, true screen pixels
                try await driver.callTool("click", ["x": x, "y": y, "scope": "desktop"])
            case "double_click":
                try await driver.callTool("double_click", [
                    "pid": observation.frontPid, "x": x, "y": y,
                ])
            case "move":
                try await driver.callTool("move_cursor", ["x": x, "y": y])
            default: break
            }
            return
        }

        // ax target: element_index path, with one late-binding refresh on miss
        guard let ax = target["ax"] as? [String: Any] else {
            throw ExecError.badOp("\(doName): target has neither x/y nor ax")
        }
        var element = Observer.resolve(ax: ax, in: observation.elements)
        if element == nil {
            element = try await refreshAndResolve(ax)
        }
        guard let element else {
            throw ExecError.unresolvedTarget(String(describing: ax))
        }

        switch doName {
        case "click":
            try await driver.callTool("click", [
                "pid": element.pid, "window_id": element.windowId,
                "element_index": element.elementIndex,
            ])
        case "double_click":
            try await driver.callTool("double_click", [
                "pid": element.pid, "window_id": element.windowId,
                "element_index": element.elementIndex,
            ])
        case "move":
            guard let center = element.center else {
                throw ExecError.unresolvedTarget("\(element.id) has no frame")
            }
            try await driver.callTool("move_cursor", [
                "x": center.x, "y": center.y,
            ])
        default: break
        }
    }

    /// assert support: true when the target resolves (with one live refresh).
    func assertResolves(_ target: [String: Any]) async throws -> Bool {
        guard let ax = target["ax"] as? [String: Any] else {
            // coordinate asserts are vacuously true
            return target["x"] != nil && target["y"] != nil
        }
        if Observer.resolve(ax: ax, in: observation.elements) != nil { return true }
        return try await refreshAndResolve(ax) != nil
    }

    /// Late-binding fallback: re-fetch the frontmost window's tree once and
    /// retry. Ids are only stable within the original snapshot, so id-only
    /// targets cannot be re-resolved (matches executor.py).
    private func refreshAndResolve(_ ax: [String: Any]) async throws -> ObservedElement? {
        let hasSemantic = ((ax["role"] as? String).map { !$0.isEmpty } ?? false)
            || ((ax["title"] as? String).map { !$0.isEmpty } ?? false)
        let fresh = try await Observer.windowElements(
            driver: driver, pid: observation.frontPid, windowId: observation.frontWindowId)
        // refreshed snapshot replaces the element list for subsequent ops
        observation = Observation(
            summary: observation.summary,
            elements: fresh,
            byId: Dictionary(uniqueKeysWithValues: fresh.map { ($0.id, $0) }),
            frontPid: observation.frontPid,
            frontWindowId: observation.frontWindowId,
            frontAppName: observation.frontAppName
        )
        guard hasSemantic else { return nil }
        var semantic = ax
        semantic.removeValue(forKey: "id")
        return Observer.resolve(ax: semantic, in: fresh)
    }
}

/// Short human string for an op, used for chips + progress history
/// (port of runner.py _op_str).
func opString(_ op: [String: Any]) -> String {
    let doName = (op["do"] as? String) ?? (op["op"] as? String) ?? "?"
    var detail = ""
    if let target = op["target"] as? String {
        detail = target
    } else if let target = op["target"] as? [String: Any] {
        if let ax = target["ax"] as? [String: Any] {
            detail = (op["resolved"] as? String)
                ?? (ax["title"] as? String)
                ?? (ax["id"] as? String)
                ?? ""
        } else if let x = target["x"], let y = target["y"] {
            detail = "(\(x),\(y))"
        }
    } else if let text = op["text"] as? String {
        detail = text.count > 24 ? String(text.prefix(24)) + "…" : text
    } else if let keys = op["keys"] as? [String] {
        detail = keys.joined(separator: "+")
    } else if let direction = op["direction"] as? String {
        detail = direction
    }
    return detail.isEmpty ? doName : "\(doName) \(detail)"
}
