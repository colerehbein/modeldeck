//
//  ModelDeck — a menu-bar control for local model lanes.
//  Talks to the modeldeck server (default 127.0.0.1:8790).
//
//  Build:  ./build.sh        (compiles to ~/.local/bin/ModelDeck + installs .app)
//  Run:    ~/.local/bin/ModelDeck   (LSUIElement app, no dock icon)
//
//  Config via env:
//    MODELDECK_API   base URL of the server (default http://127.0.0.1:8790)
//

import AppKit
import Foundation

let apiBase = ProcessInfo.processInfo.environment["MODELDECK_API"] ?? "http://127.0.0.1:8790"

struct Lane: Decodable {
    let id: String
    let label: String
    let kind: String
    let on: Bool
    let port: Int?
    let ram_mb: Int?
    let impact: String
}

struct Status: Decodable {
    let memory: [String: Double?]
    let lanes: [Lane]
    let ts: Int?
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var statusItem: NSStatusItem!
    var timer: Timer?
    var lastStatus: Status?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        rebuildMenu()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { _ in
            self.refresh()
        }
    }

    // MARK: - networking

    func refresh() {
        guard let url = URL(string: "\(apiBase)/api/status") else { return }
        let task = URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let self = self, let data = data else { return }
            if let st = try? JSONDecoder().decode(Status.self, from: data) {
                self.lastStatus = st
                DispatchQueue.main.async { self.updateBarIcon(with: st) }
            }
        }
        task.resume()
    }

    func toggle(id: String, on: Bool) {
        guard let url = URL(string: "\(apiBase)/api/toggle") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["id": id, "on": on])
        let task = URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            if let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let statusDict = obj["status"] as? [String: Any],
               let statusData = try? JSONSerialization.data(withJSONObject: statusDict),
               let st = try? JSONDecoder().decode(Status.self, from: statusData) {
                self?.lastStatus = st
                DispatchQueue.main.async { self?.updateBarIcon(with: st) }
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 5) { self?.refresh() }
            }
        }
        task.resume()
    }

    // MARK: - UI

    func updateBarIcon(with st: Status) {
        let availGB = (st.memory["available_mb"]?.value ?? 0) / 1024.0
        let onCount = st.lanes.filter { $0.on }.count
        let icon = availGB < 6 ? "🔥" : "🧠"
        if let btn = statusItem.button {
            btn.title = "\(icon) \(String(format: "%.0f", availGB))G · \(onCount)on"
        }
        rebuildMenu()
    }

    func rebuildMenu() {
        let menu = NSMenu()
        menu.delegate = self

        if let st = lastStatus {
            let availGB = (st.memory["available_mb"]?.value ?? 0) / 1024.0
            let totalGB = (st.memory["total_mb"]?.value ?? 0) / 1024.0
            let wiredGB = (st.memory["wired_mb"]?.value ?? 0) / 1024.0
            let compGB = (st.memory["compressed_mb"]?.value ?? 0) / 1024.0
            let head = NSMenuItem(title: String(format: "%.1f GB free of %.0f GB", availGB, totalGB),
                                  action: nil, keyEquivalent: "")
            head.isEnabled = false
            menu.addItem(head)
            let sub = NSMenuItem(title: String(format: "wired %.1f GB · compressed %.1f GB", wiredGB, compGB),
                                 action: nil, keyEquivalent: "")
            sub.isEnabled = false
            menu.addItem(sub)
            menu.addItem(.separator())

            for lane in st.lanes {
                let ram = lane.ram_mb.map { $0 >= 1024 ? "\($0/1024)GB" : "\($0)MB" } ?? "—"
                let title = "\(lane.on ? "●" : "○")  \(lane.label)"
                let item = NSMenuItem(title: title, action: #selector(menuToggle(_:)),
                                      keyEquivalent: "")
                item.target = self
                item.representedObject = ["id": lane.id, "on": !lane.on, "ram": ram]
                menu.addItem(item)
            }
        } else {
            let load = NSMenuItem(title: "connecting to \(apiBase)…", action: nil, keyEquivalent: "")
            load.isEnabled = false
            menu.addItem(load)
        }

        menu.addItem(.separator())
        let open = NSMenuItem(title: "Open Control Panel…", action: #selector(openPanel(_:)),
                              keyEquivalent: "o")
        open.target = self
        menu.addItem(open)
        let ref = NSMenuItem(title: "Refresh", action: #selector(doRefresh(_:)), keyEquivalent: "r")
        ref.target = self
        menu.addItem(ref)
        let quit = NSMenuItem(title: "Quit ModelDeck", action: #selector(NSApplication.terminate(_:)),
                              keyEquivalent: "q")
        menu.addItem(quit)

        statusItem.menu = menu
    }

    // MARK: - actions

    @objc func menuToggle(_ sender: NSMenuItem) {
        guard let obj = sender.representedObject as? [String: Any],
              let id = obj["id"] as? String,
              let on = obj["on"] as? Bool else { return }
        toggle(id: id, on: on)
    }

    @objc func openPanel(_ sender: NSMenuItem) {
        if let url = URL(string: "\(apiBase)/panel") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc func doRefresh(_ sender: NSMenuItem) {
        refresh()
    }
}

// Optional<Double> isn't Decodable by default; this extension fixes it.
extension Optional where Wrapped == Double {
    var value: Double { self ?? 0 }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
