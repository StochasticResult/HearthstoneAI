import AppKit
import ApplicationServices
import Security

final class Controller: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var window: NSWindow!
    let keyField = NSSecureTextField()
    let modelField = NSTextField(string: "gpt-6.1-terra")
    let status = NSTextField(labelWithString: "就绪 · 尚未启动游戏")
    let resources = NSTextField(labelWithString: "资源采样将在测试期间显示")
    let keyStatus = NSTextField(labelWithString: "密钥不会写入日志或项目文件")
    let log = NSTextView()
    var startButton: NSButton!
    var stopButton: NSButton!
    var checkButton: NSButton!
    var process: Process?
    var pipe: Pipe?
    var pending = Data()
    var terminating = false
    let service = "local.hearthstoneai.openai"

    var runtime: URL {
        // Local development updates replace only this user-owned runtime;
        // the native launcher's signing identity and Keychain ACL stay stable.
        let installed = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/HearthstoneAI/runtime")
        if FileManager.default.fileExists(atPath: installed.appendingPathComponent("session.py").path) { return installed }
        return Bundle.main.resourceURL!.appendingPathComponent("runtime")
    }

    func label(_ text: String, _ size: CGFloat = 13, bold: Bool = false) -> NSTextField {
        let v = NSTextField(wrappingLabelWithString: text)
        v.preferredMaxLayoutWidth = 604
        v.font = bold ? .boldSystemFont(ofSize: size) : .systemFont(ofSize: size)
        return v
    }

    func button(_ title: String, _ action: Selector) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .rounded
        return b
    }

    func row(_ views: [NSView]) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 10
        return stack
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let item = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "退出 Hearthstone AI", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.submenu = appMenu
        menu.addItem(item)
        let edit = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        edit.submenu = editMenu
        menu.addItem(edit)
        NSApp.mainMenu = menu
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 660, height: 720),
            styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "Hearthstone AI · 人机测试版"
        window.delegate = self
        let content = window.contentView!
        let title = label("Hearthstone AI", 27, bold: true)
        let subtitle = label("独立运行的炉石 AI · 动物园术 · 实验版", 14)
        subtitle.textColor = .secondaryLabelColor
        let note = label("本版只进入练习模式。每次最多运行 5 分钟、调用 API 40 次，结束时关闭本次启动的炉石。请先退出炉石并接通电源。", 13)
        let beta = label("API 完整对局尚未验收；可能因无法确认局面而提前停止。", 12)
        beta.textColor = .secondaryLabelColor
        keyField.placeholderString = "在本机粘贴 OpenAI API Key"
        keyField.setAccessibilityLabel("OpenAI API Key")
        keyField.widthAnchor.constraint(equalToConstant: 360).isActive = true
        modelField.setAccessibilityLabel("OpenAI 模型")
        modelField.widthAnchor.constraint(equalToConstant: 250).isActive = true
        modelField.stringValue = UserDefaults.standard.string(forKey: "model") ?? "gpt-6.1-terra"
        keyStatus.font = .systemFont(ofSize: 11)
        keyStatus.textColor = .secondaryLabelColor
        let save = button("存入钥匙串", #selector(saveKey))
        let forget = button("删除密钥", #selector(forgetKey))
        startButton = button("开始人机测试", #selector(start))
        startButton.bezelColor = .controlAccentColor
        stopButton = button("停止", #selector(stop))
        stopButton.isEnabled = false
        checkButton = button("离线自检", #selector(check))
        let permission = button("辅助功能权限", #selector(permissionSettings))
        let logs = button("查看运行记录", #selector(showLogs))
        status.font = .boldSystemFont(ofSize: 14)
        resources.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        resources.textColor = .secondaryLabelColor
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        log.isEditable = false
        log.isSelectable = true
        log.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        log.textContainerInset = NSSize(width: 8, height: 8)
        log.autoresizingMask = [.width]
        log.textContainer?.widthTracksTextView = true
        scroll.documentView = log
        scroll.heightAnchor.constraint(equalToConstant: 200).isActive = true
        scroll.widthAnchor.constraint(equalToConstant: 604).isActive = true
        let stack = NSStackView(views: [title, subtitle, note, beta,
            row([label("API Key"), keyField, save]), row([keyStatus, forget]),
            row([label("模型"), modelField]), row([startButton, stopButton, checkButton]),
            status, resources, scroll, row([permission, logs])])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 28),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -28),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 26)])
        append("离线自检不连接 API、不启动游戏。密钥仅在开始测试时用于 api.openai.com。")
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func keyQuery() -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
         kSecAttrAccount as String: "default"]
    }

    @objc func saveKey() {
        let value = keyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard value.count >= 20 else { keyStatus.stringValue = "请先填写有效的 API Key"; return }
        let query = keyQuery()
        let attrs = [kSecValueData as String: Data(value.utf8)]
        var result = SecItemUpdate(query as CFDictionary, attrs as CFDictionary)
        if result == errSecItemNotFound {
            var add = query
            add[kSecValueData as String] = Data(value.utf8)
            add[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            result = SecItemAdd(add as CFDictionary, nil)
        }
        keyStatus.stringValue = result == errSecSuccess ? "已保存到本机钥匙串" : "保存失败（\(result)）"
        if result == errSecSuccess { keyField.placeholderString = "已保存；留空即可使用钥匙串密钥" }
    }

    @objc func forgetKey() {
        let result = SecItemDelete(keyQuery() as CFDictionary)
        keyField.stringValue = ""
        keyStatus.stringValue = result == errSecSuccess || result == errSecItemNotFound ? "本应用保存的密钥已删除" : "删除失败（\(result)）"
    }

    func readKey() -> String? {
        let typed = keyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if !typed.isEmpty { return typed }
        var query = keyQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var output: CFTypeRef?
        let result = SecItemCopyMatching(query as CFDictionary, &output)
        guard result == errSecSuccess, let data = output as? Data else {
            keyStatus.stringValue = "钥匙串读取失败（\(result)）；请允许钥匙串访问或在安全框中填写密钥"
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    @objc func start() {
        guard process == nil else { return }
        guard let key = readKey(), key.count >= 20 else { status.stringValue = "请先填写 API Key，或存入钥匙串"; return }
        let model = modelField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        UserDefaults.standard.set(model, forKey: "model")
        launch(script: "session.py", config: ["api_key": key, "model": model])
    }

    @objc func check() { launch(script: "selfcheck.py", config: nil) }

    func launch(script: String, config: [String: String]?) {
        guard process == nil else { return }
        let task = Process()
        // Use an already installed interpreter; never install packages at launch.
        let python = FileManager.default.isExecutableFile(atPath: "/opt/homebrew/bin/python3") ? "/opt/homebrew/bin/python3" : "/usr/bin/python3"
        task.executableURL = URL(fileURLWithPath: python)
        task.arguments = [runtime.appendingPathComponent(script).path]
        if config != nil { task.arguments! += ["--parent-pid", "\(ProcessInfo.processInfo.processIdentifier)"] }
        task.currentDirectoryURL = runtime
        var environment = ProcessInfo.processInfo.environment
        environment.removeValue(forKey: "OPENAI_API_KEY")
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["HEARTHSTONE_AI_NATIVE"] = Bundle.main.executableURL!.path
        task.environment = environment
        let input = Pipe(), output = Pipe()
        task.standardInput = input
        task.standardOutput = output
        task.standardError = output
        pending = Data()
        pipe = output
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async { self?.receive(data) }
        }
        task.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.process = nil
                self.startButton.isEnabled = true
                self.checkButton.isEnabled = true
                self.stopButton.isEnabled = false
                self.status.stringValue = finished.terminationStatus == 0 ? "本次运行结束" : "已停止 · 请查看运行记录"
                if self.terminating { NSApp.reply(toApplicationShouldTerminate: true) }
            }
        }
        do {
            try task.run()
            process = task
            startButton.isEnabled = false; checkButton.isEnabled = false; stopButton.isEnabled = true
            status.stringValue = config == nil ? "正在离线自检" : "正在准备人机测试"
            if let config = config {
                var data = try JSONSerialization.data(withJSONObject: config)
                data.append(10)
                try input.fileHandleForWriting.write(contentsOf: data)
            }
            try input.fileHandleForWriting.close()
        } catch {
            task.terminate()
            process = nil
            startButton.isEnabled = true; checkButton.isEnabled = true; stopButton.isEnabled = false
            status.stringValue = "启动失败"
            append("未能启动程序组件。请检查 Python 与应用是否完整。")
        }
    }

    func receive(_ data: Data) {
        pending.append(data)
        if pending.count > 65536 { pending.removeAll(); stop(); return }
        while let end = pending.firstIndex(of: 10) {
            let line = pending[..<end]
            pending.removeSubrange(...end)
            guard let object = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any] else {
                append(String(decoding: line, as: UTF8.self)); continue
            }
            if let metrics = object["metrics"] as? [String: Any], let rss = metrics["rss_mib"] as? Double {
                let kind = metrics["game_seconds"] != nil ? "炉石" : "AI 进程"
                resources.stringValue = "\(kind)：\(Int(rss)) MiB　CPU：\(metrics["cpu_pct"] ?? "—")%　热状态：\(metrics["thermal_state"] ?? "—")"
            } else if let text = object["message"] as? String {
                if let inner = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any],
                   let reason = inner["reason"] as? String, let calls = inner["api_calls"] {
                    append("AI 第 \(calls) 次决策：\(reason)")
                } else { append(text) }
            }
        }
    }

    func append(_ text: String) {
        let old = log.string
        let joined = old + (old.isEmpty ? "" : "\n") + String(text.prefix(2048))
        log.string = String(joined.suffix(48000))
        log.scrollToEndOfDocument(nil)
    }

    @objc func stop() {
        guard let task = process, task.isRunning else { return }
        status.stringValue = "正在停止并恢复设置…"
        stopButton.isEnabled = false
        task.terminate()
    }

    @objc func permissionSettings() {
        _ = CGRequestPostEventAccess()
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
    }

    @objc func showLogs() {
        let folder = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/HearthstoneAI/runs")
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        NSWorkspace.shared.open(folder)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard process != nil else { return .terminateNow }
        terminating = true
        stop()
        return .terminateLater
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct Program {
    static func main() {
        if CommandLine.arguments.count >= 2 && CommandLine.arguments[1] == "--input" {
            runGameInput([CommandLine.arguments[0]] + Array(CommandLine.arguments.dropFirst(2)))
        }
        let app = NSApplication.shared
        let controller = Controller()
        app.delegate = controller
        app.setActivationPolicy(.regular)
        app.run()
    }
}
