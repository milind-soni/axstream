// The floating bottom bar: a frameless, non-activating NSPanel pinned to the
// bottom-center of the main screen. Status dot + transcript + action chips +
// right-aligned timing label. All updates must happen on the main thread.

import AppKit

enum BarStatus {
    case idle       // gray
    case listening  // red
    case thinking   // yellow
    case acting     // green

    var color: NSColor {
        switch self {
        case .idle: return NSColor.systemGray
        case .listening: return NSColor.systemRed
        case .thinking: return NSColor.systemYellow
        case .acting: return NSColor.systemGreen
        }
    }
}

@MainActor
final class Bar {
    private let panel: NSPanel
    private let dot = NSView()
    private let transcriptLabel = NSTextField(labelWithString: "")
    private let chipsStack = NSStackView()
    private let timingLabel = NSTextField(labelWithString: "")
    private static let maxChips = 8

    init() {
        let width: CGFloat = 640
        let height: CGFloat = 64
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: width, height: height),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )
        panel.level = .screenSaver
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isMovableByWindowBackground = true

        // dark rounded pill
        let effect = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        effect.material = .hudWindow
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 18
        effect.layer?.masksToBounds = true
        effect.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.55).cgColor
        panel.contentView = effect

        // status dot
        dot.wantsLayer = true
        dot.layer?.cornerRadius = 6
        dot.layer?.backgroundColor = BarStatus.idle.color.cgColor
        dot.translatesAutoresizingMaskIntoConstraints = false

        // transcript
        transcriptLabel.font = NSFont.systemFont(ofSize: 14)
        transcriptLabel.textColor = .white
        transcriptLabel.lineBreakMode = .byTruncatingHead
        transcriptLabel.maximumNumberOfLines = 1
        transcriptLabel.stringValue = "hold ⌃⌥ and speak"
        transcriptLabel.textColor = .systemGray
        transcriptLabel.translatesAutoresizingMaskIntoConstraints = false

        // action chips
        chipsStack.orientation = .horizontal
        chipsStack.spacing = 6
        chipsStack.alignment = .centerY
        chipsStack.translatesAutoresizingMaskIntoConstraints = false
        chipsStack.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        // timing
        timingLabel.font = NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        timingLabel.textColor = .systemGray
        timingLabel.alignment = .right
        timingLabel.translatesAutoresizingMaskIntoConstraints = false
        timingLabel.setContentHuggingPriority(.required, for: .horizontal)
        timingLabel.setContentCompressionResistancePriority(.required, for: .horizontal)

        effect.addSubview(dot)
        effect.addSubview(transcriptLabel)
        effect.addSubview(chipsStack)
        effect.addSubview(timingLabel)

        NSLayoutConstraint.activate([
            dot.leadingAnchor.constraint(equalTo: effect.leadingAnchor, constant: 16),
            dot.centerYAnchor.constraint(equalTo: effect.centerYAnchor),
            dot.widthAnchor.constraint(equalToConstant: 12),
            dot.heightAnchor.constraint(equalToConstant: 12),

            transcriptLabel.leadingAnchor.constraint(equalTo: dot.trailingAnchor, constant: 12),
            transcriptLabel.topAnchor.constraint(equalTo: effect.topAnchor, constant: 10),
            transcriptLabel.trailingAnchor.constraint(lessThanOrEqualTo: timingLabel.leadingAnchor, constant: -12),

            chipsStack.leadingAnchor.constraint(equalTo: dot.trailingAnchor, constant: 12),
            chipsStack.bottomAnchor.constraint(equalTo: effect.bottomAnchor, constant: -8),
            chipsStack.trailingAnchor.constraint(lessThanOrEqualTo: timingLabel.leadingAnchor, constant: -12),
            chipsStack.heightAnchor.constraint(equalToConstant: 18),

            timingLabel.trailingAnchor.constraint(equalTo: effect.trailingAnchor, constant: -16),
            timingLabel.centerYAnchor.constraint(equalTo: effect.centerYAnchor),
        ])

        position()
        panel.orderFrontRegardless()
    }

    private func position() {
        guard let screen = NSScreen.main else { return }
        let frame = panel.frame
        let x = screen.visibleFrame.midX - frame.width / 2
        let y = screen.visibleFrame.minY + 24
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    // MARK: - Updates (main thread)

    func setStatus(_ status: BarStatus) {
        dot.layer?.backgroundColor = status.color.cgColor
    }

    func setTranscript(_ text: String, partial: Bool) {
        if partial {
            transcriptLabel.font = NSFont.systemFont(ofSize: 14).italic()
            transcriptLabel.textColor = .systemGray
        } else {
            transcriptLabel.font = NSFont.systemFont(ofSize: 14)
            transcriptLabel.textColor = .white
        }
        transcriptLabel.stringValue = text
    }

    func addChip(_ text: String) {
        let chip = NSTextField(labelWithString: text)
        chip.font = NSFont.systemFont(ofSize: 10, weight: .medium)
        chip.textColor = NSColor.systemGreen
        chip.wantsLayer = true
        chip.layer?.backgroundColor = NSColor.systemGreen.withAlphaComponent(0.18).cgColor
        chip.layer?.cornerRadius = 6
        chip.drawsBackground = false
        chip.lineBreakMode = .byTruncatingTail
        chip.maximumNumberOfLines = 1

        // pad via a wrapper of fixed height
        chip.translatesAutoresizingMaskIntoConstraints = false
        let wrap = NSView()
        wrap.wantsLayer = true
        wrap.layer?.backgroundColor = NSColor.systemGreen.withAlphaComponent(0.18).cgColor
        wrap.layer?.cornerRadius = 6
        wrap.translatesAutoresizingMaskIntoConstraints = false
        wrap.addSubview(chip)
        NSLayoutConstraint.activate([
            chip.leadingAnchor.constraint(equalTo: wrap.leadingAnchor, constant: 6),
            chip.trailingAnchor.constraint(equalTo: wrap.trailingAnchor, constant: -6),
            chip.centerYAnchor.constraint(equalTo: wrap.centerYAnchor),
            wrap.heightAnchor.constraint(equalToConstant: 18),
            chip.widthAnchor.constraint(lessThanOrEqualToConstant: 140),
        ])

        chipsStack.addArrangedSubview(wrap)
        while chipsStack.arrangedSubviews.count > Self.maxChips {
            let oldest = chipsStack.arrangedSubviews[0]
            chipsStack.removeArrangedSubview(oldest)
            oldest.removeFromSuperview()
        }
    }

    func clearChips() {
        for view in chipsStack.arrangedSubviews {
            chipsStack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
    }

    func setTiming(_ text: String) {
        timingLabel.stringValue = text
    }
}

private extension NSFont {
    func italic() -> NSFont {
        NSFontManager.shared.convert(self, toHaveTrait: .italicFontMask)
    }
}
