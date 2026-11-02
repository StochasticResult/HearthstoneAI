// An explicitly requested alternative when the computer-use click does not
// reach Hearthstone. Uses global display-point coordinates, not screenshot
// pixels. Refuses mouse events outside the visible Hearthstone window.
import AppKit
import CoreGraphics
import Foundation

func fail(_ message: String) -> Never {
    fputs(message + "\n", stderr)
    exit(1)
}
if CommandLine.arguments == [CommandLine.arguments[0], "access"] {
    print(CGPreflightPostEventAccess() ? "true" : "false")
    exit(0)
}
guard CGPreflightPostEventAccess() else { fail("macOS has not granted event-posting access") }
guard let apps = NSRunningApplication.runningApplications(withBundleIdentifier: "unity.Blizzard Entertainment.Hearthstone") as [NSRunningApplication]?, apps.count == 1 else {
    fail("Expected exactly one Hearthstone process")
}
let pid = apps[0].processIdentifier
let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
let frames: [CGRect] = windows.compactMap { row in
    guard (row[kCGWindowOwnerPID as String] as? Int32) == pid,
          (row[kCGWindowLayer as String] as? Int) == 0,
          let bounds = row[kCGWindowBounds as String] as? [String: NSNumber],
          let x = bounds["X"]?.doubleValue, let y = bounds["Y"]?.doubleValue,
          let width = bounds["Width"]?.doubleValue, let height = bounds["Height"]?.doubleValue,
          width > 200, height > 200 else { return nil }
    let rect = CGRect(x: x, y: y, width: width, height: height)
    return rect
}
guard frames.count == 1 else { fail("Could not uniquely locate a visible Hearthstone window") }
guard let source = CGEventSource(stateID: .hidSystemState) else { fail("Cannot create event source") }

func point(_ x: String, _ y: String) -> CGPoint {
    guard let x = Double(x), let y = Double(y), x.isFinite, y.isFinite else { fail("Invalid coordinates") }
    let p = CGPoint(x: x, y: y)
    guard frames[0].contains(p) else { fail("Point is outside the Hearthstone window") }
    return p
}
func post(_ type: CGEventType, _ p: CGPoint, _ button: CGMouseButton = .left) {
    guard let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: p, mouseButton: button) else {
        fail("Cannot create mouse event")
    }
    if type == .leftMouseDown || type == .leftMouseUp { event.setIntegerValueField(.mouseEventClickState, value: 1) }
    event.post(tap: .cghidEventTap)
}
func key(_ code: CGKeyCode, flags: CGEventFlags = []) {
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else { fail("Hearthstone is not the frontmost app") }
    guard let down = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true),
          let up = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false) else { fail("Cannot create key event") }
    down.flags = flags; up.flags = flags
    down.post(tap: .cghidEventTap); usleep(70_000); up.post(tap: .cghidEventTap)
}
let a = CommandLine.arguments
guard a.count >= 2 else { fail("Usage: quartz_input click X Y | drag X1 Y1 X2 Y2 | key KEYCODE") }
switch a[1] {
case "focus":
    guard a.count == 2 else { fail("Usage: quartz_input focus") }
    guard apps[0].activate(options: [.activateIgnoringOtherApps]) else { fail("Cannot activate Hearthstone") }
case "geometry":
    guard a.count == 2 else { fail("Usage: quartz_input geometry") }
    let frame = frames[0]
    print("{\"x\":\(frame.origin.x),\"y\":\(frame.origin.y),\"width\":\(frame.width),\"height\":\(frame.height)}")
case "move":
    guard a.count == 4 else { fail("Usage: quartz_input move X Y") }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else { fail("Hearthstone is not the frontmost app") }
    post(.mouseMoved, point(a[2], a[3]))
case "click":
    guard a.count == 4 else { fail("Usage: quartz_input click X Y") }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else { fail("Hearthstone is not the frontmost app") }
    let p = point(a[2], a[3])
    post(.mouseMoved, p); usleep(100_000)
    post(.leftMouseDown, p); usleep(100_000); post(.leftMouseUp, p)
case "drag":
    guard a.count == 6 else { fail("Usage: quartz_input drag X1 Y1 X2 Y2") }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else { fail("Hearthstone is not the frontmost app") }
    let start = point(a[2], a[3]), end = point(a[4], a[5])
    post(.mouseMoved, start); usleep(100_000); post(.leftMouseDown, start); usleep(100_000)
    for i in 1...25 {
        let t = CGFloat(i) / 25
        post(.leftMouseDragged, CGPoint(x: start.x + (end.x - start.x)*t, y: start.y + (end.y - start.y)*t))
        usleep(20_000)
    }
    post(.leftMouseUp, end)
case "key":
    guard a.count == 3, let code = UInt16(a[2]), code <= 127 else { fail("Usage: quartz_input key KEYCODE") }
    key(code)
case "paste":
    guard a.count == 3 else { fail("Usage: quartz_input paste TEXT") }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else { fail("Hearthstone is not the frontmost app") }
    let board = NSPasteboard.general
    let prior = (board.pasteboardItems ?? []).map { item -> [(NSPasteboard.PasteboardType, Data)] in
        item.types.compactMap { type in item.data(forType: type).map { (type, $0) } }
    }
    board.clearContents(); board.setString(a[2], forType: .string)
    key(0, flags: .maskCommand); key(9, flags: .maskCommand); key(36)
    usleep(250_000)
    board.clearContents()
    let restored = prior.map { pairs -> NSPasteboardItem in
        let item = NSPasteboardItem()
        for (type, data) in pairs { item.setData(data, forType: type) }
        return item
    }
    if !restored.isEmpty { board.writeObjects(restored) }
default:
    fail("Unknown command")
}
