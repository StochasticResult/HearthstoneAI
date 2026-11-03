import AppKit

final class TrainingPanel: NSObject {
    unowned let owner: Controller
    var panel: NSWindow?
    var timer: Timer?
    var worker: Process?
    private var buffer = Data()
    let model = NSTextField(string: "gpt-6-astra")
    let effort = NSPopUpButton()
    let games = NSTextField(string: "3")
    let minutes = NSTextField(string: "60")
    let delay = NSTextField(string: "8")
    let scheduled = NSButton(checkboxWithTitle: "定时自训练", target: nil, action: nil)
    let useLessons = NSButton(checkboxWithTitle: "在对局中使用已学经验", target: nil, action: nil)
    let standby = NSButton(checkboxWithTitle: "Sol 超时时允许 Luna Decisions 接管", target: nil, action: nil)
    let multi = NSButton(checkboxWithTitle: "允许经游戏逐步确认的多步计划", target: nil, action: nil)
    let summary = NSTextField(wrappingLabelWithString: "")
    let report = NSTextView()
    let learn = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/HearthstoneAI/learning")
    init(owner: Controller) { self.owner = owner; super.init() }

    func json(_ name: String, limit: Int) -> [String: Any] {
        let file = learn.appendingPathComponent(name)
        guard let attrs = try? file.resourceValues(forKeys: [.fileSizeKey,.isSymbolicLinkKey]),
              attrs.isSymbolicLink != true, (attrs.fileSize ?? limit + 1) <= limit,
              let bytes = try? Data(contentsOf: file), bytes.count <= limit,
              let value = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any] else { return [:] }
        return value
    }
    func makeButton(_ title: String, _ action: Selector) -> NSButton {
        NSButton(title: title, target: self, action: action)
    }
    func field(_ field: NSTextField, _ name: String, _ width: CGFloat) {
        field.setAccessibilityLabel(name)
        field.widthAnchor.constraint(equalToConstant: width).isActive = true
    }
    func show() {
        if panel == nil {
            let w = NSWindow(contentRect: NSRect(x:0,y:0,width:665,height:595),
                             styleMask:[.titled,.closable,.miniaturizable],backing:.buffered,defer:false)
            w.title = "策略与自训练"
            w.isReleasedWhenClosed = false
            panel = w
            effort.addItems(withTitles:["low","medium","high","xhigh","max"])
            field(model,"评审模型",212); field(minutes,"训练间隔分钟",54)
            field(games,"每次复盘局数",45); field(delay,"启动备用的秒数",45)
            effort.setAccessibilityLabel("评审推理强度")
            report.isEditable=false; report.isSelectable=true
            report.font = .systemFont(ofSize:12)
            report.textContainerInset = NSSize(width:6,height:6)
            report.autoresizingMask = [.width]; report.textContainer?.widthTracksTextView=true
            let scroll = NSScrollView()
            scroll.hasVerticalScroller=true; scroll.borderType = .bezelBorder; scroll.documentView=report
            scroll.widthAnchor.constraint(equalToConstant:605).isActive=true
            scroll.heightAnchor.constraint(equalToConstant:192).isActive=true
            let stack = NSStackView(views:[
                owner.label("对局策略",17,bold:true), multi,
                owner.row([standby, owner.label("等待"),delay,owner.label("秒")]), useLessons,
                owner.label("赛后自训练",17,bold:true),
                owner.row([owner.label("评审模型"),model,owner.label("推理强度"),effort]),
                owner.row([owner.label("每次最多复盘"),games,owner.label("局"),scheduled,owner.label("每"),minutes,owner.label("分钟")]),
                owner.label("只分析已结算的新天梯。应用打开时可定时训练。经验从下一局生效。",12),
                owner.row([makeButton("保存设置",#selector(savePressed)),makeButton("保存并立即训练",#selector(manualPressed))]),
                summary,scroll])
            stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 10
            stack.translatesAutoresizingMaskIntoConstraints=false
            let content=w.contentView!
            content.addSubview(stack)
            NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo:content.leadingAnchor,constant:25),
                stack.topAnchor.constraint(equalTo:content.topAnchor,constant:23),
                stack.trailingAnchor.constraint(equalTo:content.trailingAnchor,constant:-25)])
            w.center()
        }
        if panel?.isVisible != true {
            let p=json("coach-settings.json",limit:4096)
            model.stringValue=p["coach_model"] as? String ?? "gpt-6-astra"
            effort.selectItem(withTitle:p["coach_effort"] as? String ?? "max")
            games.integerValue=p["games"] as? Int ?? 3; minutes.integerValue=p["interval_minutes"] as? Int ?? 60
            delay.integerValue=p["standby_delay"] as? Int ?? 8
            scheduled.state=(p["scheduled"] as? Bool ?? false) ? .on : .off
            useLessons.state=(p["use_lessons"] as? Bool ?? true) ? .on : .off
            standby.state=(p["standby"] as? Bool ?? true) ? .on : .off
            multi.state=(p["multi_step"] as? Bool ?? true) ? .on : .off
        }
        refresh()
        panel?.makeKeyAndOrderFront(nil)
    }
    func refresh() {
        let s=json("coach-state.json",limit:384*1024)
        let clock=json("coach-clock.json",limit:4096)
        summary.stringValue=clock["status"] as? String ?? "还没有训练记录"
        var text=[String]()
        if let r=(s["reports"] as? [[String:Any]])?.last {
            let at=Date(timeIntervalSince1970:r["at"] as? Double ?? 0)
            let date=DateFormatter.localizedString(from:at,dateStyle:.short,timeStyle:.short)
            text.append("\(date)　\(r["model"] ?? "") / \(r["effort"] ?? "")　\(r["games"] ?? 0) 局 · \(r["actions"] ?? 0) 步")
            for item in r["reviews"] as? [[String:Any]] ?? [] {
                text.append("\(item["run"] ?? "")\n\(item["review"] ?? "")")
            }
        }
        let lessons=s["lessons"] as? [[String:Any]] ?? []
        if !lessons.isEmpty { text.append("已保存经验 · \(lessons.count) 条") }
        for rule in lessons { text.append("\(rule["card"] ?? "")　\(rule["advice"] ?? "")\n适用例外　\(rule["exception"] ?? "")") }
        report.string=text.joined(separator:"\n\n")
    }
    @discardableResult func save() -> Bool {
        let name=model.stringValue.trimmingCharacters(in:.whitespacesAndNewlines)
        guard name.range(of:"^[A-Za-z0-9._-]{1,80}$",options:.regularExpression) != nil,
              let g=Int(games.stringValue), (1...5).contains(g),
              let m=Int(minutes.stringValue), (15...1440).contains(m),
              let d=Int(delay.stringValue), (0...12).contains(d) else {
            summary.stringValue="请核对模型名、复盘局数（1–5）、间隔（15–1440 分钟）和备用等待（0–12 秒）。"
            return false
        }
        let previous=json("coach-settings.json",limit:4096)
        let prefs:[String:Any]=["coach_model":name,"coach_effort":effort.titleOfSelectedItem ?? "max",
            "games":g,"interval_minutes":m,"scheduled":scheduled.state == .on,"use_lessons":useLessons.state == .on,
            "standby":standby.state == .on,"standby_delay":d,"multi_step":multi.state == .on]
        do {
            try FileManager.default.createDirectory(at:learn,withIntermediateDirectories:true)
            try JSONSerialization.data(withJSONObject:prefs).write(to:learn.appendingPathComponent("coach-settings.json"),options:.atomic)
            if scheduled.state == .on && previous["scheduled"] as? Bool != true {
                UserDefaults.standard.set(Date().timeIntervalSince1970,forKey:"coachScheduleStarted")
            }
            summary.stringValue="设置已保存"
            return true
        } catch { summary.stringValue="设置保存失败"; return false }
    }
    @objc func savePressed(){ save() }
    @objc func manualPressed(){ if save() { run(manual:true) } }
    func startTimer(){
        timer=Timer.scheduledTimer(withTimeInterval:60,repeats:true){[weak self] _ in self?.tick()}
        timer?.tolerance=8
        tick()
    }
    func tick(){
        guard worker==nil else{return}
        let prefs=json("coach-settings.json",limit:4096)
        guard prefs["scheduled"] as? Bool == true else{return}
        let last=json("coach-clock.json",limit:4096)["last_attempt"] as? Double ?? 0
        let enabled=UserDefaults.standard.double(forKey:"coachScheduleStarted")
        if Date().timeIntervalSince1970 >= max(last,enabled)+Double(prefs["interval_minutes"] as? Int ?? 60)*60 {
            run(manual:false)
        }
    }
    func run(manual: Bool){
        guard worker==nil else{ if manual { owner.append("已有一次训练正在进行") };return }
        let task=Process(), output=Pipe(), input=Pipe()
        task.executableURL=URL(fileURLWithPath:FileManager.default.isExecutableFile(atPath:"/opt/homebrew/bin/python3") ? "/opt/homebrew/bin/python3":"/usr/bin/python3")
        task.arguments=[owner.runtime.appendingPathComponent("self_training.py").path,"--trigger",manual ? "manual":"scheduled"]
        task.currentDirectoryURL=owner.runtime
        task.environment=["PATH":"/usr/bin:/bin:/opt/homebrew/bin","PYTHONUNBUFFERED":"1","PYTHONDONTWRITEBYTECODE":"1"]
        task.qualityOfService = .utility
        task.standardInput=input;task.standardOutput=output;task.standardError=FileHandle.nullDevice
        buffer=Data()
        output.fileHandleForReading.readabilityHandler={ [weak self] handle in
            let bytes=handle.availableData
            if bytes.isEmpty {handle.readabilityHandler=nil;return}
            DispatchQueue.main.async {
                guard let self=self, self.worker===task else{return}
                self.buffer.append(bytes)
                while let end=self.buffer.firstIndex(of:10) {
                    let line=String(decoding:self.buffer[..<end],as:UTF8.self)
                    self.buffer.removeSubrange(...end)
                    if line=="NEED_KEY" {
                        if let key=self.owner.readKey(interactive:manual && self.owner.process==nil) {
                            try? input.fileHandleForWriting.write(contentsOf:Data((key+"\n").utf8))
                        }
                        try? input.fileHandleForWriting.close()
                    } else if !line.isEmpty { self.summary.stringValue=line; self.owner.append(line) }
                }
            }
        }
        task.terminationHandler={ [weak self] finished in
            DispatchQueue.main.async {
                guard let self=self, self.worker===finished else{return}
                self.worker=nil
                try? input.fileHandleForWriting.close()
                self.refresh()
                if manual {self.owner.append(self.summary.stringValue)}
            }
        }
        do { try task.run();worker=task;summary.stringValue="正在检查已结算对局" }
        catch {output.fileHandleForReading.readabilityHandler=nil;summary.stringValue="无法启动训练组件";owner.append(summary.stringValue)}
    }
    func shutdown(){timer?.invalidate();if let task=worker, task.isRunning {task.terminate()}}
}
