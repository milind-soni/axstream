import AppKit

// Wispr-style pill at the bottom-center of the screen: shows "listening"
// while ⌃⌥ is held, then what was heard and what ran. Never takes focus,
// never takes clicks.
final class Hud {
    private let panel: NSPanel
    private let label = NSTextField(labelWithString: "")
    private var hideWork: DispatchWorkItem?

    init() {
        panel = NSPanel(contentRect: .zero,
                        styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: true)
        panel.isFloatingPanel = true
        panel.level = .statusBar
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.ignoresMouseEvents = true
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let effect = NSVisualEffectView()
        effect.material = .hudWindow
        effect.state = .active
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 14
        effect.layer?.masksToBounds = true

        label.font = .systemFont(ofSize: 15, weight: .medium)
        label.textColor = .white
        label.alignment = .center
        label.lineBreakMode = .byTruncatingTail
        label.maximumNumberOfLines = 1
        label.translatesAutoresizingMaskIntoConstraints = false
        effect.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: effect.leadingAnchor, constant: 18),
            label.trailingAnchor.constraint(equalTo: effect.trailingAnchor, constant: -18),
            label.topAnchor.constraint(equalTo: effect.topAnchor, constant: 10),
            label.bottomAnchor.constraint(equalTo: effect.bottomAnchor, constant: -10),
        ])
        panel.contentView = effect
    }

    /// Show `text`; keeps showing until the next call or `autoHide` elapses.
    func show(_ text: String, autoHide seconds: Double? = nil) {
        assert(Thread.isMainThread)
        hideWork?.cancel()
        hideWork = nil
        label.stringValue = text
        reposition()
        panel.orderFrontRegardless()
        if let seconds {
            let work = DispatchWorkItem { [weak self] in self?.panel.orderOut(nil) }
            hideWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: work)
        }
    }

    func hide() {
        assert(Thread.isMainThread)
        hideWork?.cancel()
        hideWork = nil
        panel.orderOut(nil)
    }

    private func reposition() {
        guard let content = panel.contentView,
              let screen = NSScreen.main ?? NSScreen.screens.first else { return }
        let fit = content.fittingSize
        let width = min(max(fit.width, 180), screen.visibleFrame.width - 80)
        let frame = NSRect(x: screen.frame.midX - width / 2,
                           y: screen.frame.minY + 72,
                           width: width, height: fit.height)
        panel.setFrame(frame, display: true)
    }
}
