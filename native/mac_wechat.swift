// Narrow, local AX driver. No networking, injection, database access or OCR.
import AppKit
import ApplicationServices
import CryptoKit
import Foundation

func attr(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}
func string(_ element: AXUIElement, _ name: String) -> String? { attr(element,name) as? String }
func children(_ element: AXUIElement) -> [AXUIElement] { attr(element,"AXChildren") as? [AXUIElement] ?? [] }
func walk(_ root: AXUIElement, depth: Int = 0) -> [AXUIElement] {
    if depth > 20 { return [] }
    return [root] + children(root).prefix(200).flatMap { walk($0,depth:depth+1) }
}
func digest(_ object: [String: Any]) -> String {
    guard let data = try? JSONSerialization.data(withJSONObject:object,options:[.sortedKeys]) else { return "" }
    return SHA256.hash(data:data).map { String(format:"%02x",$0) }.joined()
}
func output(_ value: [String:Any]) {
    let data = try! JSONSerialization.data(withJSONObject:value,options:[.sortedKeys])
    FileHandle.standardOutput.write(data);FileHandle.standardOutput.write(Data([10]))
}
func direction(_ description: String) -> String {
    if description.hasPrefix("发出的消息") || description.lowercased().hasPrefix("sent message:") { return "self" }
    if description.hasPrefix("收到的消息") || description.lowercased().hasPrefix("received message:") { return "incoming" }
    return "unknown"
}
struct Context {
    let app: NSRunningApplication
    let input: AXUIElement
    let snapshot: [String:Any]
}
func inspect(_ expected: String, doctor: Bool = false) -> (Context?, [String:Any]) {
    guard AXIsProcessTrusted() else { return (nil,["status":"accessibility_required"]) }
    let apps=NSRunningApplication.runningApplications(withBundleIdentifier:"com.tencent.xinWeChat")
    guard apps.count==1, let app=apps.first else { return (nil,["status":"wechat_not_running_or_ambiguous"]) }
    let root=AXUIElementCreateApplication(app.processIdentifier)
    guard let windows=attr(root,"AXWindows") as? [AXUIElement],windows.count==1,let window=windows.first else {
        return (nil,["status":"window_missing_or_ambiguous"])
    }
    let elements=walk(window)
    let inputs=elements.filter { string($0,"AXRole")=="AXTextArea" && string($0,"AXTitle")==expected }
    let lists=elements.filter { string($0,"AXRole")=="AXList" && ["消息","Messages"].contains(string($0,"AXTitle") ?? "") }
    guard inputs.count==1,lists.count==1,let input=inputs.first,let list=lists.first else {
        return (nil,["status":"unsupported_ui","input_matches":inputs.count,"message_lists":lists.count,
                     "element_count":elements.count])
    }
    if doctor { return (nil,["status":"controls_available","input_matches":1,"message_lists":1]) }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier==app.processIdentifier else {
        return (nil,["status":"wechat_not_frontmost"])
    }
    guard let draft=string(input,"AXValue"),draft.isEmpty else { return (nil,["status":"draft_not_empty_or_unreadable"]) }
    var rows: [[String:String]]=[]
    for row in children(list).suffix(500) {
        let direct=string(row,"AXValue") ?? ""
        let texts=walk(row).filter { string($0,"AXRole")=="AXStaticText" }.compactMap { string($0,"AXValue") }.filter { !$0.isEmpty }
        // No "longest text" guess: multiple labels may be names/quotes, not the body.
        let text = !direct.isEmpty ? direct : (texts.count==1 ? texts[0] : "")
        if text.isEmpty { return (nil,["status":"unsupported_message_row"]) }
        rows.append(["text":text,"direction":direction(string(row,"AXDescription") ?? "")])
    }
    guard let windowInfo=CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements],kCGNullWindowID) as? [[String:Any]] else {
        return (nil,["status":"window_identity_unavailable"])
    }
    let mainWindows=windowInfo.filter { ($0[kCGWindowOwnerPID as String] as? Int)==Int(app.processIdentifier) && ($0[kCGWindowLayer as String] as? Int)==0 }
    guard mainWindows.count==1,let number=mainWindows[0][kCGWindowNumber as String] as? Int else {
        return (nil,["status":"window_identity_ambiguous"])
    }
    var snapshot: [String:Any] = ["status":"ready","chat_name":expected,"session_id":"\(app.processIdentifier):\(number)","messages":rows,"draft_empty":true,"identity_verified":false]
    snapshot["fingerprint"]=digest(snapshot)
    return (Context(app:app,input:input,snapshot:snapshot),snapshot)
}
func handle(_ request: [String:Any]) -> [String:Any] {
    guard let action=request["action"] as? String,["doctor","snapshot","send"].contains(action),
          let chat=request["chat_name"] as? String,!chat.isEmpty,chat.count<=100 else {
        return ["status":"invalid_request"]
    }
    if action=="send" { return ["status":"send_blocked_unverified_identity"] }
    let (_,result)=inspect(chat,doctor:action=="doctor")
    return result
}
if CommandLine.arguments.contains("--self-test") {
    assert(direction("received message: x")=="incoming")
    assert(direction("ordinary text")=="unknown")
    assert(handle(["action":"send","chat_name":"文件传输助手","text":"test"])["status"] as? String == "send_blocked_unverified_identity")
    assert(digest(["a":1])==digest(["a":1]))
    output(["status":"self_test_passed"])
} else {
    let data=FileHandle.standardInput.readDataToEndOfFile()
    if data.count<=32768,let request=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any] { output(handle(request)) }
    else { output(["status":"invalid_request"]) }
}
