import Foundation
import AppKit
import ApplicationServices
import ScreenCaptureKit
import ImageIO
import UniformTypeIdentifiers
import Darwin

// This executable only targets Apple's iPhone Mirroring process. No AppleScript,
// shell execution, global event posting, display capture or arbitrary-app option.
private let allowedBundle = "com.apple.ScreenContinuity"
private let cacheURL = FileManager.default.temporaryDirectory
    .appendingPathComponent("xianyu-native-mobile-\(getuid())-geometry.json")

private struct NativeFailure: Error {
    let code: String
    let message: String
}

private struct Geometry: Codable {
    let bundleIdentifier: String
    let pid: Int32
    let windowId: UInt32
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let imageWidth: Int
    let imageHeight: Int
    let scale: Double
    let capturedAt: Double
}

@MainActor
private func permissions() -> [String: Bool] {
    // Both functions are nonprompting preflight checks.
    ["accessibility": AXIsProcessTrusted(), "screenRecording": CGPreflightScreenCaptureAccess()]
}

@MainActor
private func foregroundState() -> [String: Any] {
    let front = NSWorkspace.shared.frontmostApplication
    return ["targetFrontmost": front?.bundleIdentifier == allowedBundle,
            "frontmostBundleIdentifier": front?.bundleIdentifier ?? "",
            "frontmostPid": front?.processIdentifier ?? 0]
}

@MainActor
private func requireTargetFrontmost(_ pid: pid_t) throws {
    guard let front = NSWorkspace.shared.frontmostApplication,
          front.bundleIdentifier == allowedBundle && front.processIdentifier == pid else {
        throw NativeFailure(code: "target_not_frontmost", message: "镜像不是前台应用；已暂停输入，不会自动抢焦点。请本人切回镜像后再继续。")
    }
}

@MainActor
private func targetApp() throws -> NSRunningApplication {
    let apps = NSRunningApplication.runningApplications(withBundleIdentifier: allowedBundle)
        .filter { !$0.isTerminated }
    guard apps.count == 1, let app = apps.first else {
        throw NativeFailure(code: "target_unavailable", message: "需要一个正在运行的 iPhone 镜像应用；不会自动启动或切换应用。")
    }
    return app
}

private func portraitFrame(_ frame: CGRect) -> Bool {
    frame.width.isFinite && frame.height.isFinite && frame.width >= 180 && frame.height >= 350 &&
        frame.width <= 1600 && frame.height <= 3000 && frame.width / frame.height > 0.20 &&
        frame.width / frame.height < 0.95
}

@MainActor
private func windowMetadata(pid: pid_t) -> [[String: Any]] {
    // Window metadata only. This does not capture pixels or request permission.
    guard let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] else { return [] }
    return windows.filter { row in
        guard let owner = row[kCGWindowOwnerPID as String] as? NSNumber,
              let layer = row[kCGWindowLayer as String] as? NSNumber,
              let bounds = row[kCGWindowBounds as String] as? NSDictionary,
              let frame = CGRect(dictionaryRepresentation: bounds) else { return false }
        return owner.int32Value == pid && layer.intValue == 0 && portraitFrame(frame)
    }
}

@MainActor
private func health(command: String = "health") -> [String: Any] {
    let grants = permissions()
    let app = try? targetApp()
    let found = app.map { !windowMetadata(pid: $0.processIdentifier).isEmpty } ?? false
    var result: [String: Any] = ["ok": true, "command": command, "bundleIdentifier": allowedBundle,
            "permissions": grants, "appRunning": app != nil, "windowFound": found,
            "ready": grants["accessibility"] == true && grants["screenRecording"] == true && found,
            "deviceUnlocked": NSNull(),
            "message": "ready只表示权限和镜像窗口可用；手机是否解锁须由后续实际屏幕判断。"]
    result.merge(foregroundState()) { _, value in value }
    return result
}

@MainActor
private func requirePermissions(input: Bool) throws {
    guard CGPreflightScreenCaptureAccess() else {
        throw NativeFailure(code: "screen_recording_required", message: "缺少屏幕录制权限；请本人授权后重试。此命令不会自动弹窗。")
    }
    if input && !AXIsProcessTrusted() {
        throw NativeFailure(code: "accessibility_required", message: "缺少辅助功能权限；请本人授权后重试。此命令不会自动弹窗。")
    }
}

@available(macOS 14.2, *)
@MainActor
private func selectedWindow() async throws -> (SCWindow, SCContentFilter, Geometry) {
    let app = try targetApp()
    let available = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
    let candidates = available.windows.filter {
        $0.owningApplication?.bundleIdentifier == allowedBundle &&
        $0.owningApplication?.processID == app.processIdentifier &&
        $0.windowLayer == 0 && $0.isOnScreen && portraitFrame($0.frame)
    }.sorted { $0.frame.width * $0.frame.height > $1.frame.width * $1.frame.height }
    guard let window = candidates.first else {
        throw NativeFailure(code: "window_unavailable", message: "未找到可见的 iPhone 镜像主窗口；最小化、隐藏或断连时拒绝操作。")
    }
    if candidates.count > 1 {
        let a = candidates[0].frame, b = candidates[1].frame
        guard a.width*a.height > b.width*b.height*1.20 else {
            throw NativeFailure(code: "ambiguous_window", message: "存在多个相近的镜像窗口，无法安全确定目标。")
        }
    }
    let filter = SCContentFilter(desktopIndependentWindow: window)
    let scale = Double(filter.pointPixelScale)
    guard scale.isFinite && scale >= 1 && scale <= 4 else {
        throw NativeFailure(code: "invalid_scale", message: "镜像窗口的像素比例无效。")
    }
    let frame = window.frame
    // The untrimmed window is captured, including its title bar. Removing only
    // the shadow makes the image origin equal the SCWindow frame origin.
    let geometry = Geometry(bundleIdentifier: allowedBundle, pid: app.processIdentifier, windowId: window.windowID,
                            x: frame.minX, y: frame.minY, width: frame.width, height: frame.height,
                            imageWidth: Int((frame.width*scale).rounded()), imageHeight: Int((frame.height*scale).rounded()),
                            scale: scale, capturedAt: Date().timeIntervalSince1970)
    return (window, filter, geometry)
}

@MainActor
private func metadata(_ geometry: Geometry, command: String) -> [String: Any] {
    var result: [String: Any] = ["ok": true, "command": command, "bundleIdentifier": allowedBundle,
     "pid": geometry.pid, "windowId": geometry.windowId,
     "frame": ["x": geometry.x, "y": geometry.y, "width": geometry.width, "height": geometry.height],
     "imageWidth": geometry.imageWidth, "imageHeight": geometry.imageHeight, "scale": geometry.scale,
     "scaleX": Double(geometry.imageWidth)/geometry.width,
     "scaleY": Double(geometry.imageHeight)/geometry.height, "includesTitlebar": true,
     "coordinateOrigin": "window_top_left_pixels", "deviceUnlocked": NSNull()]
    result.merge(foregroundState()) { _, value in value }
    return result
}

@MainActor
private func accessibilityFrame(_ window: AXUIElement) -> CGRect? {
    var positionValue: CFTypeRef?, sizeValue: CFTypeRef?
    guard AXUIElementCopyAttributeValue(window, kAXPositionAttribute as CFString, &positionValue) == .success,
          AXUIElementCopyAttributeValue(window, kAXSizeAttribute as CFString, &sizeValue) == .success,
          let position = positionValue, let size = sizeValue,
          CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero, dimensions = CGSize.zero
    guard AXValueGetValue(unsafeBitCast(position, to: AXValue.self), .cgPoint, &point),
          AXValueGetValue(unsafeBitCast(size, to: AXValue.self), .cgSize, &dimensions) else { return nil }
    return CGRect(origin: point, size: dimensions)
}

@available(macOS 14.2, *)
@MainActor
private func focusTarget() async throws -> [String: Any] {
    try requirePermissions(input: true)
    let (_, _, geometry) = try await selectedWindow()
    let app = try targetApp()
    guard app.processIdentifier == geometry.pid && app.bundleIdentifier == allowedBundle else {
        throw NativeFailure(code: "target_changed", message: "镜像目标进程已改变，拒绝切换焦点。")
    }
    let before = foregroundState()
    let axApp = AXUIElementCreateApplication(geometry.pid)
    AXUIElementSetMessagingTimeout(axApp, 1.0)
    var windowsValue: CFTypeRef?
    let listResult = AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &windowsValue)
    let windows = (windowsValue as? [AXUIElement]) ?? []
    let matched = windows.first { window in
        guard let frame = accessibilityFrame(window) else { return false }
        return abs(frame.minX-geometry.x) <= 2 && abs(frame.minY-geometry.y) <= 2 &&
            abs(frame.width-geometry.width) <= 2 && abs(frame.height-geometry.height) <= 2
    }
    var errors: [String: Int] = ["windows": Int(listResult.rawValue)]
    if let window = matched {
        errors["focusedWindow"] = Int(AXUIElementSetAttributeValue(axApp, kAXFocusedWindowAttribute as CFString, window).rawValue)
        errors["focused"] = Int(AXUIElementSetAttributeValue(window, kAXFocusedAttribute as CFString, kCFBooleanTrue).rawValue)
    }
    try await Task.sleep(nanoseconds: 150_000_000)
    var activationRequested = false, activationAccepted = false
    if NSWorkspace.shared.frontmostApplication?.processIdentifier != geometry.pid {
        // Only this explicit command may request activation, and the target is
        // always the hardcoded Apple Mirroring bundle verified above.
        activationRequested = true
        activationAccepted = app.activate(options: [])
        try await Task.sleep(nanoseconds: 200_000_000)
    }
    var focusedWindowVerified = false
    var focusedValue: CFTypeRef?
    if AXUIElementCopyAttributeValue(axApp, kAXFocusedWindowAttribute as CFString, &focusedValue) == .success,
       let value = focusedValue, CFGetTypeID(value) == AXUIElementGetTypeID(),
       let frame = accessibilityFrame(unsafeBitCast(value, to: AXUIElement.self)) {
        focusedWindowVerified = abs(frame.minX-geometry.x) <= 2 && abs(frame.minY-geometry.y) <= 2 &&
            abs(frame.width-geometry.width) <= 2 && abs(frame.height-geometry.height) <= 2
    }
    var result = metadata(geometry, command: "focus")
    result["before"] = before
    result["after"] = foregroundState()
    result["axWindowMatched"] = matched != nil
    result["axResults"] = errors
    result["activationRequested"] = activationRequested
    result["activationRequestAccepted"] = activationAccepted
    result["focusedWindowVerified"] = focusedWindowVerified
    result["effectVerified"] = false
    result["requiresVisualVerification"] = true
    result["message"] = "仅显式请求镜像窗口焦点；未发送键盘或点击。输入能否生效仍需截图验证，后台工作不能据此宣称成立。"
    return result
}

private func writeGeometry(_ geometry: Geometry) throws {
    try JSONEncoder().encode(geometry).write(to: cacheURL, options: .atomic)
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: cacheURL.path)
}

@available(macOS 14.2, *)
@MainActor
private func capture(_ output: String) async throws -> [String: Any] {
    try requirePermissions(input: false)
    let (_, filter, intended) = try await selectedWindow()
    let url = URL(fileURLWithPath: output).standardizedFileURL
    guard url.pathExtension.lowercased() == "png" else {
        throw NativeFailure(code: "png_required", message: "截图输出必须使用.png扩展名。")
    }
    let config = SCStreamConfiguration()
    config.width = intended.imageWidth
    config.height = intended.imageHeight
    config.showsCursor = false
    config.capturesAudio = false
    config.ignoreShadowsSingleWindow = true
    config.ignoreGlobalClipSingleWindow = true
    config.includeChildWindows = false
    config.captureResolution = .best
    // sourceRect remains unset: Apple's independent-window default includes the
    // entire selected window. No display or arbitrary rectangle is captured.
    let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
    guard image.width > 0 && image.height > 0,
          abs(Double(image.width)/Double(image.height) - intended.width/intended.height) < 0.015 else {
        throw NativeFailure(code: "capture_geometry_mismatch", message: "截图比例与镜像完整窗口不一致，拒绝保存错误坐标系。")
    }
    let geometry = Geometry(bundleIdentifier: allowedBundle, pid: intended.pid, windowId: intended.windowId,
                            x: intended.x, y: intended.y, width: intended.width, height: intended.height,
                            imageWidth: image.width, imageHeight: image.height, scale: intended.scale,
                            capturedAt: Date().timeIntervalSince1970)
    let data = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(data, UTType.png.identifier as CFString, 1, nil) else {
        throw NativeFailure(code: "png_encode_failed", message: "无法编码PNG截图。")
    }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else {
        throw NativeFailure(code: "png_encode_failed", message: "PNG截图编码失败。")
    }
    try (data as Data).write(to: url, options: .atomic)
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    try writeGeometry(geometry)
    var result = metadata(geometry, command: "screenshot")
    result["path"] = url.path
    return result
}

@available(macOS 14.2, *)
@MainActor
private func click(_ x: Double, _ y: Double) async throws -> [String: Any] {
    try requirePermissions(input: true)
    let (_, _, current) = try await selectedWindow()
    try requireTargetFrontmost(current.pid)
    guard let bytes = try? Data(contentsOf: cacheURL), let saved = try? JSONDecoder().decode(Geometry.self, from: bytes),
          saved.bundleIdentifier == allowedBundle && saved.pid == current.pid && saved.windowId == current.windowId,
          Date().timeIntervalSince1970-saved.capturedAt < 600,
          abs(saved.width-current.width) < 0.5 && abs(saved.height-current.height) < 0.5 else {
        throw NativeFailure(code: "fresh_screenshot_required", message: "请先保存当前窗口截图；窗口更换、缩放或截图超过10分钟后必须重新截图。")
    }
    guard x.isFinite && y.isFinite && x >= 0 && y >= 0 && x < Double(saved.imageWidth) && y < Double(saved.imageHeight) else {
        throw NativeFailure(code: "outside_window", message: "点击像素坐标不在最后截图范围内。")
    }
    let inWindowX = x*current.width/Double(saved.imageWidth)
    let fromWindowTop = y*current.height/Double(saved.imageHeight)
    guard let primary = NSScreen.screens.first else {
        throw NativeFailure(code: "screen_geometry_unavailable", message: "无法取得Cocoa坐标转换所需的主屏几何信息。")
    }
    // A foreign process's window is not an NSWindow in this helper. Construct
    // the public NSEvent with its windowNumber before taking cgEvent, so AppKit
    // retains that association. Convert the screenshot's window-top-left points
    // to Cocoa's bottom-origin event location; never synthesize private fields.
    let location = NSPoint(x: inWindowX, y: primary.frame.height-fromWindowTop)
    let timestamp = ProcessInfo.processInfo.systemUptime
    guard let nativeDown = NSEvent.mouseEvent(with: .leftMouseDown, location: location, modifierFlags: [],
                                             timestamp: timestamp, windowNumber: Int(current.windowId), context: nil,
                                             eventNumber: 1, clickCount: 1, pressure: 1),
          let nativeUp = NSEvent.mouseEvent(with: .leftMouseUp, location: location, modifierFlags: [],
                                           timestamp: timestamp+0.09, windowNumber: Int(current.windowId), context: nil,
                                           eventNumber: 2, clickCount: 1, pressure: 0),
          let down = nativeDown.cgEvent, let up = nativeUp.cgEvent else {
        throw NativeFailure(code: "event_creation_failed", message: "无法创建限定目标进程的点击事件。")
    }
    for event in [down, up] {
        event.setIntegerValueField(.mouseEventWindowUnderMousePointer, value: Int64(current.windowId))
        event.setIntegerValueField(.mouseEventWindowUnderMousePointerThatCanHandleThisEvent, value: Int64(current.windowId))
        event.setIntegerValueField(.eventTargetUnixProcessID, value: Int64(current.pid))
        event.setIntegerValueField(.mouseEventClickState, value: 1)
    }
    down.postToPid(current.pid)
    try await Task.sleep(nanoseconds: 90_000_000)
    up.postToPid(current.pid)
    var result = metadata(current, command: "click")
    result["posted"] = true
    result["effectVerified"] = false
    result["eventMethod"] = "NSEvent.windowNumber.cgEvent.postToPid"
    result["requiresVisualVerification"] = true
    result["message"] = "事件仅投递到镜像PID；是否生效须由后续截图核实。"
    return result
}

@available(macOS 14.2, *)
@MainActor
private func typeText(_ text: String) async throws -> [String: Any] {
    try requirePermissions(input: true)
    let (_, _, current) = try await selectedWindow()
    try requireTargetFrontmost(current.pid)
    guard !text.isEmpty && text.utf16.count <= 2000 else {
        throw NativeFailure(code: "invalid_text_length", message: "输入文本须为1至2000个UTF-16码元。")
    }
    // Public ANSI virtual key codes from Carbon/HIToolbox Events.h. Mirroring
    // can forward physical key codes while ignoring a Unicode-only keycode 0.
    let unshifted: [Character: UInt16] = [
        "a":0x00,"s":0x01,"d":0x02,"f":0x03,"h":0x04,"g":0x05,"z":0x06,"x":0x07,
        "c":0x08,"v":0x09,"b":0x0B,"q":0x0C,"w":0x0D,"e":0x0E,"r":0x0F,"y":0x10,
        "t":0x11,"1":0x12,"2":0x13,"3":0x14,"4":0x15,"6":0x16,"5":0x17,"=":0x18,
        "9":0x19,"7":0x1A,"-":0x1B,"8":0x1C,"0":0x1D,
        "]":0x1E,"o":0x1F,"u":0x20,"[":0x21,"i":0x22,"p":0x23,
        "l":0x25,"j":0x26,"'":0x27,"k":0x28,";":0x29,"\\":0x2A,",":0x2B,
        "/":0x2C,"n":0x2D,"m":0x2E,".":0x2F,"\t":0x30," ":0x31,"`":0x32,
        "\n":0x24,"\r":0x24
    ]
    let shifted: [Character: Character] = [
        "!":"1","@":"2","#":"3","$":"4","%":"5","^":"6","&":"7","*":"8",
        "(":"9",")":"0","_":"-","+":"=","{":"[","}":"]",":":";","\"":"'",
        "|":"\\","<":",",">":".","?":"/","~":"`"
    ]
    guard !text.unicodeScalars.contains(where: { ($0.value < 32 && ![9,10,13].contains($0.value)) || $0.value == 127 }) else {
        throw NativeFailure(code: "unsupported_control_text", message: "不支持此控制字符；不会发送部分输入。")
    }
    var unicodeFallback = false
    // Iterate scalars without splitting surrogate pairs. Never log the supplied
    // text, its characters or generated event representations.
    for scalar in text.unicodeScalars {
        try requireTargetFrontmost(current.pid)
        let value = String(scalar)
        let character = Character(value)
        let lower = value.lowercased()
        let isUpperASCII = scalar.value >= 65 && scalar.value <= 90
        let base = shifted[character] ?? (isUpperASCII ? Character(lower) : character)
        let flags: NSEvent.ModifierFlags = shifted[character] != nil || isUpperASCII ? [.shift] : []
        let physical = unshifted[base]
        let keyCode: UInt16 = physical ?? 0
        let characters = scalar.value == 10 ? "\r" : value
        let timestamp = ProcessInfo.processInfo.systemUptime
        guard let nativeDown = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags,
                                               timestamp: timestamp, windowNumber: Int(current.windowId), context: nil,
                                               characters: characters, charactersIgnoringModifiers: String(base),
                                               isARepeat: false, keyCode: keyCode),
              let nativeUp = NSEvent.keyEvent(with: .keyUp, location: .zero, modifierFlags: flags,
                                             timestamp: timestamp+0.015, windowNumber: Int(current.windowId), context: nil,
                                             characters: characters, charactersIgnoringModifiers: String(base),
                                             isARepeat: false, keyCode: keyCode),
              let down = nativeDown.cgEvent, let up = nativeUp.cgEvent else {
            throw NativeFailure(code: "event_creation_failed", message: "无法创建限定目标进程的Unicode事件。")
        }
        if physical == nil {
            unicodeFallback = true
            let units = Array(value.utf16)
            units.withUnsafeBufferPointer { buffer in
                down.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress!)
                up.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress!)
            }
        }
        down.setIntegerValueField(.eventTargetUnixProcessID, value: Int64(current.pid))
        up.setIntegerValueField(.eventTargetUnixProcessID, value: Int64(current.pid))
        down.postToPid(current.pid)
        try await Task.sleep(nanoseconds: 15_000_000)
        up.postToPid(current.pid)
        try await Task.sleep(nanoseconds: 15_000_000)
    }
    var result = metadata(current, command: "type")
    result["posted"] = true
    result["effectVerified"] = false
    result["requiresVisualVerification"] = true
    result["eventMethod"] = "NSEvent.windowNumber.keyEvent.cgEvent.postToPid"
    result["unicodeFallbackUsed"] = unicodeFallback
    result["message"] = "键盘事件仅投递到镜像PID；未记录输入内容，Unicode路径可能被镜像忽略，必须截图核实。"
    return result
}

private func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
       let string = String(data: data, encoding: .utf8) { print(string) }
}

@main
@MainActor
struct NativeMobileMain {
    static func main() async {
        let args = Array(CommandLine.arguments.dropFirst())
        let command = args.first ?? "health"
        do {
            if ["state", "screenshot", "click", "type", "focus"].contains(command) {
                // ScreenCaptureKit/CGS requires an initialized AppKit connection
                // in a standalone CLI even when no NSWindow is ever created.
                // Initialize before the first await and never activate the app,
                // create a window, show a Dock icon or start a UI event loop.
                _ = NSApplication.shared
                NSApp.setActivationPolicy(.prohibited)
                NSApp.finishLaunching()
            }
            switch command {
            case "health":
                guard args.count <= 1 else { throw NativeFailure(code: "invalid_arguments", message: "health 不接受其他参数。") }
                emit(health())
            case "request-permissions":
                guard args.count == 1 else { throw NativeFailure(code: "invalid_arguments", message: "授权命令不接受其他参数。") }
                // This is the only command allowed to trigger permission UI.
                _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
                _ = CGRequestScreenCaptureAccess()
                emit(health(command: command))
            case "state":
                guard args.count == 1 else { throw NativeFailure(code: "invalid_arguments", message: "state 不接受其他参数。") }
                try requirePermissions(input: false)
                if #available(macOS 14.2, *) {
                    let (_, _, geometry) = try await selectedWindow()
                    var result = metadata(geometry, command: command)
                    result["permissions"] = permissions()
                    result["ready"] = AXIsProcessTrusted()
                    emit(result)
                } else { throw NativeFailure(code: "unsupported_macos", message: "需要macOS 14.2或更新版本。") }
            case "screenshot":
                guard args.count == 2 else { throw NativeFailure(code: "invalid_arguments", message: "用法：screenshot OUT.png") }
                if #available(macOS 14.2, *) { emit(try await capture(args[1])) }
                else { throw NativeFailure(code: "unsupported_macos", message: "需要macOS 14.2或更新版本。") }
            case "click":
                guard args.count == 3, let x = Double(args[1]), let y = Double(args[2]) else {
                    throw NativeFailure(code: "invalid_arguments", message: "用法：click X Y，坐标是最近截图的像素。")
                }
                if #available(macOS 14.2, *) { emit(try await click(x, y)) }
                else { throw NativeFailure(code: "unsupported_macos", message: "需要macOS 14.2或更新版本。") }
            case "type":
                guard args.count == 2 else { throw NativeFailure(code: "invalid_arguments", message: "用法：type TEXT；输入内容不会回显。") }
                if #available(macOS 14.2, *) { emit(try await typeText(args[1])) }
                else { throw NativeFailure(code: "unsupported_macos", message: "需要macOS 14.2或更新版本。") }
            case "focus":
                guard args.count == 1 else { throw NativeFailure(code: "invalid_arguments", message: "focus 不接受其他应用或参数。") }
                if #available(macOS 14.2, *) { emit(try await focusTarget()) }
                else { throw NativeFailure(code: "unsupported_macos", message: "需要macOS 14.2或更新版本。") }
            default:
                throw NativeFailure(code: "unknown_command", message: "仅支持health/state/screenshot/click/type/focus/request-permissions。")
            }
        } catch let failure as NativeFailure {
            emit(["ok": false, "command": command, "bundleIdentifier": allowedBundle,
                  "error": failure.code, "message": failure.message, "permissions": permissions()])
            exit(2)
        } catch {
            // Do not expose arbitrary framework errors, paths or command args.
            emit(["ok": false, "command": command, "bundleIdentifier": allowedBundle,
                  "error": "native_operation_failed", "message": "系统API操作未完成，请检查权限、镜像连接与窗口状态。", "permissions": permissions()])
            exit(2)
        }
    }
}
