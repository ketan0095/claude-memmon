// MemmonBar — menu-bar front end for memmon.
//
// Deliberately does no background work. `memmon owners --json` runs ONLY when
// the popover opens or Sync is pressed. The title is refreshed from the tiny
// cached sample the launchd sampler already writes — a file read, never a spawn.
//
// memmon.py is the only component that signals processes: every stop goes
// through `memmon act`, which re-checks identity before each signal. This file
// never calls kill(), not even on its own memmon children: a call that times
// out is abandoned and reaped in the background. The one thing done here is
// quitting a GUI app, through NSRunningApplication, between two
// `memmon act verify-app` checks made under actions.lock.
//
// Build:  swiftc -O -o MemmonBar MemmonBar.swift -framework Cocoa

import Cocoa
import SwiftUI

// MARK: - units

let GB = 1024.0 * 1024.0 * 1024.0
let MB = 1024.0 * 1024.0

/// Render and test modes pin the clock so fixture ages stay stable.
var clockOverride: Double?
func nowTs() -> Double { clockOverride ?? Date().timeIntervalSince1970 }

func gb(_ b: Double) -> String { String(format: "%.1f GB", b / GB) }

func growthText(_ b: Double) -> String {
    let sign = b < 0 ? "−" : "+"
    let a = abs(b)
    return a >= GB ? sign + String(format: "%.1f GB", a / GB)
                   : sign + String(format: "%.0f MB", a / MB)
}

func coresText(_ c: Double) -> String { String(format: "%.1f cores", c) }

func ageText(_ seconds: Double) -> String {
    let s = max(0, Int(seconds))
    if s < 60 { return "\(s)s" }
    if s < 3600 { return "\(s / 60) min" }
    if s < 86400 { return "\(s / 3600) h" }
    return "\(s / 86400) d"
}

func relative(_ ts: Double) -> String { ageText(nowTs() - ts) + " ago" }

func eventTime(_ ts: Double) -> String {
    let f = DateFormatter(); f.dateFormat = "d MMM, HH:mm"
    return f.string(from: Date(timeIntervalSince1970: ts))
}

func retainedDate(_ ts: Double, includeTime: Bool = false) -> String {
    let f = DateFormatter(); f.dateFormat = includeTime ? "d MMM, HH:mm" : "d MMM"
    return f.string(from: Date(timeIntervalSince1970: ts))
}

func eventClock(_ ts: Double, seconds: Bool = false) -> String {
    let f = DateFormatter(); f.dateFormat = seconds ? "HH:mm:ss.SSS" : "HH:mm"
    return f.string(from: Date(timeIntervalSince1970: ts))
}

func plural(_ n: Int, _ one: String, _ many: String? = nil) -> String {
    "\(n) " + (n == 1 ? one : (many ?? one + "s"))
}

// MARK: - palette

/// One token set per appearance, resolved by the view's own appearance, so the
/// popover follows the system theme and a render can force either one.
private func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255,
            green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
}

private func token(_ light: NSColor, _ dark: NSColor) -> Color {
    Color(nsColor: NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
    })
}

enum P {
    static let bg = token(rgb(0xfaf9fe), rgb(0x171621))
    static let panel = token(rgb(0xffffff), rgb(0x201f2e))
    static let soft = token(rgb(0xefedf7), rgb(0x292738))
    static let text = token(rgb(0x262332), rgb(0xf1eefb))
    static let muted = token(rgb(0x696377), rgb(0xaca6bd))
    static let border = token(rgb(0xe2deec), rgb(0x363242))
    static let accent = token(rgb(0x6450be), rgb(0xbaa7ff))
    static let selected = token(rgb(0xeee8ff), rgb(0x30283f))
    static let green = token(rgb(0x257049), rgb(0x87d7a5))
    static let amber = token(rgb(0x886009), rgb(0xefc570))
    static let red = token(rgb(0xab3a4a), rgb(0xffa0ae))
    static let scrim = token(rgb(0x39334d, 0x55 / 255.0), rgb(0x080610, 0xa8 / 255.0))
    static let onTint = token(rgb(0xffffff), rgb(0x221a35))

    /// An unknown level is muted, never green: a missing reading is not health.
    static func tint(_ level: String?) -> Color {
        switch level {
        case "CRITICAL", "DANGER": return red
        case "WATCH": return amber
        case "HEALTHY": return green
        default: return muted
        }
    }
}

func ft(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
    .system(size: size, weight: weight)
}

extension View {
    func panel(_ radius: CGFloat = 12) -> some View {
        background(RoundedRectangle(cornerRadius: radius).fill(P.panel))
            .overlay(RoundedRectangle(cornerRadius: radius).stroke(P.border, lineWidth: 1))
    }
}

// MARK: - JSON helpers

/// JSONSerialization hands booleans over as NSNumber; a flag is never a metric.
func num(_ v: Any?) -> Double? {
    guard let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return nil }
    let d = n.doubleValue
    return d.isFinite ? d : nil
}
func int(_ v: Any?) -> Int? { num(v).map { Int($0) } }
func str(_ v: Any?) -> String? { (v as? String).flatMap { $0.isEmpty ? nil : $0 } }
func strs(_ v: Any?) -> [String]? { (v as? [Any])?.compactMap { $0 as? String } }

// MARK: - gate model (legacy `gate` object, embedded verbatim in owners --json)

struct GateClassification {
    var source: String, rule: String, shape: String
    var samples: Int?
    var observedPeak: Double?
    var blockEligible: Bool
}

struct GateEvent: Identifiable {
    let id = UUID()
    var ts: Double, action: String, mode: String
    var sessionID: String, sessionName: String?
    var commandRaw: String, commandDisplay: String
    var classification: GateClassification?
    var legacy: Bool
    var level: String, score: Int?, reasons: [String]
    var retryStatus: String
    var ms: Int
}

struct PendingRetry: Identifiable {
    let id = UUID()
    var ts: Double, sessionID: String, sessionName: String?
    var commandRaw: String, commandDisplay: String, pressureLevel: String
    var eventRetained: Bool
}

struct GateStats {
    var installed = false
    var paused = false
    var pausedUntil: Double? = nil
    var mode = "block-critical"
    var since = 0.0, historyFrom = 0.0, historyTo: Double? = nil
    var complete = false, truncated = false
    var evaluated = 0, warned = 0, stopped = 0, errors = 0
    var events: [GateEvent] = []
    var pending: [PendingRetry] = []

    static func decode(_ g: [String: Any]) -> GateStats {
        func number(_ value: Any?) -> Double { num(value) ?? 0 }
        func integer(_ value: Any?) -> Int { int(value) ?? 0 }
        var s = GateStats()
        s.installed = g["installed"] as? Bool ?? false
        s.paused = g["paused"] as? Bool ?? false
        if let until = g["paused_until"], !(until is NSNull) {
            s.pausedUntil = number(until)
        }
        if let policy = g["policy"] as? [String: Any] {
            s.mode = policy["mode"] as? String ?? "block-critical"
        }
        if let counts = g["counts"] as? [String: Any] {
            s.since = number(counts["since"])
            s.complete = counts["complete"] as? Bool ?? false
            s.evaluated = integer(counts["evaluated"])
            s.warned = integer(counts["warned"])
            s.stopped = integer(counts["stopped"])
            s.errors = integer(counts["errors"])
        }
        if let history = g["history"] as? [String: Any] {
            s.historyFrom = number(history["from"])
            if let to = history["to"], !(to is NSNull) {
                s.historyTo = number(to)
            }
            s.truncated = history["truncated"] as? Bool ?? false
            s.events = (history["events"] as? [[String: Any]] ?? []).map { d in
                let session = d["session"] as? [String: Any] ?? [:]
                let command = d["command"] as? [String: Any] ?? [:]
                let pressure = d["pressure"] as? [String: Any] ?? [:]
                var match: GateClassification?
                if let c = d["classification"] as? [String: Any] {
                    match = GateClassification(
                        source: c["source"] as? String ?? "none",
                        rule: c["rule"] as? String ?? "",
                        shape: c["shape"] as? String ?? "",
                        samples: c["samples"] is NSNull ? nil : integer(c["samples"]),
                        observedPeak: c["observed_peak_bytes"] is NSNull
                            ? nil : number(c["observed_peak_bytes"]),
                        blockEligible: c["block_eligible"] as? Bool ?? false)
                }
                return GateEvent(
                    ts: number(d["ts"]),
                    action: d["action"] as? String ?? "warn",
                    mode: d["mode"] as? String ?? "block-critical",
                    sessionID: session["id"] as? String ?? "",
                    sessionName: session["name"] as? String,
                    commandRaw: command["raw"] as? String ?? "",
                    commandDisplay: command["display"] as? String ?? "",
                    classification: match,
                    legacy: (d["legacy"] as? Bool) ?? (match == nil),
                    level: pressure["level"] as? String ?? "?",
                    score: pressure["score"] is NSNull ? nil : integer(pressure["score"]),
                    reasons: pressure["reasons"] as? [String] ?? [],
                    retryStatus: d["retry_status"] as? String ?? "not_waiting",
                    ms: integer(d["ms"]))
            }
        }
        s.pending = (g["pending_retry"] as? [[String: Any]] ?? []).map { d in
            let session = d["session"] as? [String: Any] ?? [:]
            let command = d["command"] as? [String: Any] ?? [:]
            return PendingRetry(
                ts: number(d["ts"]),
                sessionID: session["id"] as? String ?? "",
                sessionName: session["name"] as? String,
                commandRaw: command["raw"] as? String ?? "",
                commandDisplay: command["display"] as? String ?? "",
                pressureLevel: d["pressure_level"] as? String ?? "?",
                eventRetained: d["event_retained"] as? Bool ?? false)
        }
        return s
    }
}

// MARK: - owners model (memmon owners --json, schema 2)

/// A `memmon run` lease, waiting or running; the same shape as legacy --json jobs.
struct ManagedJob: Identifiable {
    var id: String, resource: String, label: String, state: String, reason: String
    var elapsed: Int
}

struct SystemInfo {
    var ramBytes: Double?, usedBytes: Double?
    var pressureLevel: String?, scoreLevel: String?
    var ncpu: Double?, cpuCores: Double?, cpuCoverage: Double?
    var reason: String?
}

struct Protection {
    var summary: String?, gate: String?, route: String?
    var unmanagedHeavy: Int?
}

struct OwnerJob: Identifiable {
    var id: String
    var kind: String, label: String
    var footprint: Double?, memberCount: Int?
    var token: String?, action: String?

    var isConversation: Bool { kind == "conversation" }
    var rootPid: String? { id.split(separator: ".").first.map(String.init) }

    var stopAction: String {
        if let action { return action }
        return kind == "server" ? "stop-server" : "stop-job"
    }
    var stopLabel: String {
        switch stopAction {
        case "stop-server": return "Stop server"
        case "stop-managed-job": return "Stop job"
        default:
            switch kind {
            case "build": return "Stop build"
            case "test": return "Stop tests"
            default: return "Stop job"
            }
        }
    }
    /// "typecheck" + build → "Typecheck · build".
    var displayName: String {
        let head = label.prefix(1).uppercased() + label.dropFirst()
        if isConversation || label.lowercased().contains(kind) { return head }
        return "\(head) · \(kind)"
    }
}

struct AppInstanceInfo {
    var pid: Int, launchDate: Double?
}

struct Owner: Identifiable {
    var id: String
    var kind: String, agent: String, title: String
    var project: String?, worktree: String?
    var activity: String?, confidence: String?
    var footprint: Double?, footprintReason: String?
    var cpu: Double?, cpuCoverage: Double?, cpuReason: String?
    var growth: Double?, growthReason: String?
    var memberCount: Int?
    var rootPid: Int?, rootStart: [Double]?
    var token: String?
    var jobs: [OwnerJob] = []
    var actions: [String] = []
    var instances: [AppInstanceInfo]?
    var sharedWith: [String]?
    var stopCommand: String?
    var usedBy: [String]?
    /// Other owners whose roots run inside this app (a terminal hosting
    /// sessions). memmon offers no quit for such an app.
    var hosts: [String] = []
    /// The app runs plain shells: quitting it ends every one of them.
    var hostsShells = false

    static let shellWarning = "Quitting a terminal ends every shell and agent session in it."
    /// Set only on the synthetic "Unattributed" row that collapses unknown owners.
    var group: [Owner] = []

    var isUnattributed: Bool { kind == "unknown" || agent == "unknown" }

    /// An app hosting other owners' sessions is never offered a quit, even if
    /// a payload were to list one.
    func can(_ action: String) -> Bool {
        actions.contains(action) && token != nil && !(action == "quit-app" && !hosts.isEmpty)
    }

    var agentLabel: String {
        switch kind {
        case "codex-app": return "Codex app"
        case "codex-ui": return "Codex thread"
        default: break
        }
        switch agent {
        case "claude": return "Claude"
        case "codex": return "Codex"
        case "app": return "App"
        case "service": return "Shared service"
        case "job": return "Managed job"
        default: return "Unattributed"
        }
    }

    var line2: String {
        if !group.isEmpty {
            return "\(plural(memberCount ?? 0, "process", "processes")) · no owning session or app"
        }
        if let activity { return "\(agentLabel) · \(activity)" }
        if let n = memberCount { return "\(agentLabel) · \(plural(n, "process", "processes"))" }
        return agentLabel
    }

    var line3: String {
        let parts = [project, worktree.map { "\($0) worktree" }].compactMap { $0 }
        if !parts.isEmpty { return parts.joined(separator: " · ") }
        if !group.isEmpty || isUnattributed { return "Ownership could not be traced" }
        switch kind {
        case "service": return "Not assigned to a session"
        case "codex-app": return "Shared process — memory not split by thread"
        case "codex-ui": return "Frontend only — no stop action"
        case "claude", "codex": return "No project detected"
        case "app":
            if !hosts.isEmpty { return "Hosts \(plural(hosts.count, "session"))" }
            return instances.map { plural($0.count, "instance") } ?? "Not assigned to a session"
        default: return agentLabel
        }
    }

    var detailTag: String {
        if !group.isEmpty { return "Unattributed processes" }
        switch kind {
        case "claude", "codex": return "Session details"
        case "codex-app": return "Shared process"
        case "codex-ui": return "Codex frontend"
        case "service": return "Shared service"
        case "app": return "App details"
        case "job": return "Managed job"
        default: return "Details"
        }
    }

    static func decode(_ d: [String: Any]) -> Owner? {
        guard let id = str(d["owner_id"]) else { return nil }
        let kind = str(d["kind"]) ?? "unknown"
        var o = Owner(id: id, kind: kind, agent: str(d["agent"]) ?? kind,
                      title: str(d["title"]) ?? id)
        o.project = str(d["project"]); o.worktree = str(d["worktree"])
        o.activity = str(d["activity"]); o.confidence = str(d["confidence"])
        o.footprint = num(d["footprint_bytes"]); o.footprintReason = str(d["footprint_reason"])
        o.cpu = num(d["cpu_cores"]); o.cpuCoverage = num(d["cpu_coverage"])
        o.cpuReason = str(d["cpu_reason"])
        o.growth = num(d["growth_bytes_per_10min"]); o.growthReason = str(d["growth_reason"])
        o.memberCount = int(d["member_count"])
        if let root = d["root"] as? [String: Any] {
            o.rootPid = int(root["pid"])
            o.rootStart = (root["start"] as? [Any])?.compactMap { num($0) }
        }
        o.token = str(d["token"])
        o.actions = strs(d["actions"]) ?? []
        o.jobs = (d["jobs"] as? [[String: Any]] ?? []).compactMap { j in
            guard let jid = str(j["job_id"]) else { return nil }
            return OwnerJob(id: jid, kind: str(j["kind"]) ?? "other",
                            label: str(j["label"]) ?? "job",
                            footprint: num(j["footprint_bytes"]),
                            memberCount: int(j["member_count"]),
                            token: str(j["token"]), action: str(j["action"]))
        }
        o.instances = (d["instances"] as? [[String: Any]])?.compactMap { i in
            int(i["pid"]).map { AppInstanceInfo(pid: $0, launchDate: launchDate(i["launch_date"])) }
        }
        o.sharedWith = strs(d["shared_with"])
        o.stopCommand = str(d["stop_command"])
        o.usedBy = strs(d["used_by"])
        o.hosts = strs(d["hosts"]) ?? []
        o.hostsShells = d["hosts_shells"] as? Bool ?? false
        return o
    }
}

/// launch_date is epoch seconds, the process start as a float.
func launchDate(_ v: Any?) -> Double? { num(v) }

struct OwnersSnap {
    var ts: Double?, source: String?, inventory: String?, cpuWindow: Double?
    var inventoryReason: String?, hiddenProcesses: Int?
    /// memmon's own totals for the collapsed Unattributed row.
    var unattributed: [String: Any]?
    var system = SystemInfo()
    var protection: Protection?
    var gate = GateStats()
    /// No gate object at all: its state is unknown, which is not "not installed".
    var gateMissing = false
    var runnerJobs: [ManagedJob] = []
    var owners: [Owner] = []

    var degraded: Bool { inventory == "degraded" }

    var age: Double? { ts.map { nowTs() - $0 } }
    var stale: Bool {
        guard let age else { return true }
        return age > (source == "sampler" ? 180 : 90)
    }

    static func decode(_ j: [String: Any]) -> OwnersSnap? {
        guard let v = num(j["schema_version"]), v >= 2,
              let list = j["owners"] as? [Any] else { return nil }
        var s = OwnersSnap()
        s.ts = num(j["ts"]); s.source = str(j["source"])
        s.inventory = str(j["inventory"]); s.cpuWindow = num(j["cpu_window_s"])
        s.inventoryReason = str(j["inventory_reason"]); s.hiddenProcesses = int(j["hidden_process_count"])
        s.unattributed = j["unattributed"] as? [String: Any]
        s.runnerJobs = (j["runner_jobs"] as? [[String: Any]] ?? []).map { d in
            ManagedJob(id: str(d["id"]) ?? UUID().uuidString, resource: str(d["resource"]) ?? "heavy",
                       label: str(d["label"]) ?? "command", state: str(d["state"]) ?? "unknown",
                       reason: str(d["reason"]) ?? "", elapsed: int(d["elapsed_seconds"]) ?? 0)
        }
        if let y = j["system"] as? [String: Any] {
            s.system = SystemInfo(ramBytes: num(y["ram_bytes"]), usedBytes: num(y["used_bytes"]),
                                  pressureLevel: str(y["pressure_level"]),
                                  scoreLevel: str(y["score_level"]),
                                  ncpu: num(y["ncpu"]), cpuCores: num(y["cpu_cores"]),
                                  cpuCoverage: num(y["cpu_coverage"]), reason: str(y["reason"]))
        }
        if let p = j["protection"] as? [String: Any] {
            s.protection = Protection(summary: str(p["summary"]), gate: str(p["gate"]),
                                      route: str(p["route"]),
                                      unmanagedHeavy: int(p["unmanaged_heavy"]))
        }
        if let g = j["gate"] as? [String: Any] { s.gate = GateStats.decode(g) } else { s.gateMissing = true }
        s.owners = list.compactMap { ($0 as? [String: Any]).flatMap(Owner.decode) }
        return s
    }

    /// The owner list as displayed: every unattributed subtree collapses into one
    /// "Unattributed" row, and totals stay partitioned because nothing is copied.
    var rows: [Owner] {
        let unknown = owners.filter { $0.isUnattributed }
        var out = owners.filter { !$0.isUnattributed }
        guard !unknown.isEmpty else { return out }
        func sum(_ xs: [Double?]) -> Double? {
            let have = xs.compactMap { $0 }
            return have.isEmpty ? nil : have.reduce(0, +)
        }
        var g = Owner(id: "unknown:*", kind: "unknown", agent: "unknown", title: "Unattributed")
        g.confidence = "unknown"
        g.group = unknown
        g.footprint = sum(unknown.map { $0.footprint })
        // A partial sum would understate the row, so CPU shows only when every
        // unattributed tree was measured.
        let cpus = unknown.compactMap { $0.cpu }
        g.cpu = cpus.count == unknown.count ? cpus.reduce(0, +) : nil
        g.cpuReason = unknown.first { $0.cpu == nil }?.cpuReason ?? "not measured"
        g.growth = nil
        g.growthReason = "not tracked"
        g.memberCount = unknown.reduce(0) { $0 + ($1.memberCount ?? 1) }
        if let u = unattributed {
            g.footprint = num(u["footprint_bytes"]) ?? g.footprint
            g.memberCount = int(u["member_count"]) ?? g.memberCount
            g.growth = num(u["growth_bytes_per_10min"])
            g.growthReason = str(u["growth_reason"]) ?? "not enough history"
        }
        out.append(g)
        return out
    }
}

enum SortKey: String, CaseIterable {
    case memory, cpu, growth
    var label: String { rawValue == "cpu" ? "CPU" : rawValue.capitalized }
    var columnHeader: String {
        switch self {
        case .memory: return "Memory · CPU cores"
        case .cpu: return "CPU cores · Memory"
        case .growth: return "Growth per 10 min · Memory"
        }
    }
    func metric(_ o: Owner) -> Double? {
        switch self {
        case .memory: return o.footprint
        case .cpu: return o.cpu
        case .growth: return o.growth
        }
    }
}

/// Unavailable values sort last whichever metric is chosen; ties fall back to
/// memory, then title, so the order does not shuffle between refreshes.
func sortOwners(_ owners: [Owner], by key: SortKey) -> [Owner] {
    owners.sorted { a, b in
        switch (key.metric(a), key.metric(b)) {
        case let (x?, y?) where x != y: return x > y
        case (nil, _?): return false
        case (_?, nil): return true
        default:
            let fa = a.footprint ?? -1, fb = b.footprint ?? -1
            return fa != fb ? fa > fb : a.title < b.title
        }
    }
}

// MARK: - running memmon

struct CLIResult {
    var exit: Int32?
    var stdout: Data
    var timedOut = false
    var launchError: String?
}

enum CLI {
    static var python = "/usr/bin/python3"
    static var script = NSString(string: "~/.claude/memmon/memmon.py").expandingTildeInPath
    static var ownersTimeout = 10.0
    static var actTimeout = 25.0

    /// posix_spawn rather than Process: quit-app hands the actions.lock
    /// descriptor to `memmon act verify-app`, and Process closes every fd above 2.
    ///
    /// A call that outlives its timeout is never signalled: the caller gets a
    /// timeout at once and the child is reaped on a background thread whenever
    /// it ends. `onExit` runs once the child is reaped, either way, so a
    /// caller can avoid starting another while one is still running.
    static func run(_ args: [String], timeout: Double, inheritFD: Int32? = nil,
                    onExit: (() -> Void)? = nil) -> CLIResult {
        var fds: [Int32] = [0, 0]
        guard pipe(&fds) == 0 else {
            return CLIResult(exit: nil, stdout: Data(), launchError: "could not create a pipe")
        }
        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_adddup2(&actions, fds[1], 1)
        posix_spawn_file_actions_addopen(&actions, 2, "/dev/null", O_WRONLY, 0)
        if let fd = inheritFD { posix_spawn_file_actions_addinherit_np(&actions, fd) }
        var attr: posix_spawnattr_t?
        posix_spawnattr_init(&attr)
        posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_CLOEXEC_DEFAULT))
        var argv: [UnsafeMutablePointer<CChar>?] = ([python, script] + args).map { strdup($0) }
        argv.append(nil)
        var pid: pid_t = 0
        let rc = posix_spawn(&pid, python, &actions, &attr, &argv, environ)
        argv.forEach { free($0) }
        posix_spawn_file_actions_destroy(&actions)
        posix_spawnattr_destroy(&attr)
        close(fds[1])
        guard rc == 0 else {
            close(fds[0])
            return CLIResult(exit: nil, stdout: Data(),
                             launchError: "could not start memmon (\(String(cString: strerror(rc))))")
        }

        let readFD = fds[0]
        let collected = DataBox()
        let readDone = DispatchSemaphore(value: 0)
        Thread.detachNewThread {
            var buf = [UInt8](repeating: 0, count: 65536)
            while true {
                let n = read(readFD, &buf, buf.count)
                if n > 0 { collected.append(buf, n) } else if n < 0 && errno == EINTR { continue } else { break }
            }
            close(readFD)
            readDone.signal()
        }

        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        var status: Int32 = 0
        while true {
            let r = waitpid(pid, &status, WNOHANG)
            if r == pid { break }
            if r < 0 && errno != EINTR {
                onExit?()
                return CLIResult(exit: nil, stdout: Data(), launchError: "lost track of memmon")
            }
            if ProcessInfo.processInfo.systemUptime >= deadline {
                DispatchQueue.global(qos: .utility).async {
                    var s: Int32 = 0
                    while waitpid(pid, &s, 0) < 0 && errno == EINTR {}
                    onExit?()
                }
                return CLIResult(exit: nil, stdout: Data(), timedOut: true)
            }
            usleep(20_000)
        }
        onExit?()
        // A grandchild that kept stdout open must not hang the caller.
        _ = readDone.wait(timeout: .now() + 2)
        let exited = (status & 0x7f) == 0
        return CLIResult(exit: exited ? (status >> 8) & 0xff : nil, stdout: collected.data,
                         launchError: exited ? nil : "memmon was terminated by a signal")
    }
}

final class DataBox: @unchecked Sendable {
    private let lock = NSLock()
    private var buf = Data()
    func append(_ bytes: [UInt8], _ n: Int) { lock.lock(); buf.append(bytes, count: n); lock.unlock() }
    var data: Data { lock.lock(); defer { lock.unlock() }; return buf }
}

/// The flock memmon act takes for every action. Quit-app holds it here, across
/// verify-app, terminate() and the watch, so no other action interleaves.
enum ActionsLock {
    static var path = NSString(string: "~/.claude/memmon/runner/coord/actions.lock")
        .expandingTildeInPath

    enum Acquired { case held(Int32), busy, failed(String) }

    /// LOCK_NB polling with the same 5 s limit as memmon act.
    static func acquire(timeout: Double = 5) -> Acquired {
        let dir = (path as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC, 0o600)
        guard fd >= 0 else { return .failed("cannot open actions.lock") }
        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        while flock(fd, LOCK_EX | LOCK_NB) != 0 {
            if ProcessInfo.processInfo.systemUptime >= deadline { close(fd); return .busy }
            usleep(100_000)
        }
        return .held(fd)
    }

    static func release(_ fd: Int32) { flock(fd, LOCK_UN); close(fd) }
}

// MARK: - action outcomes

struct ActOutcome {
    var result: String
    var reason: String?
    var exited: Int?
    var captured: Int?
    var remaining: Int = 0
    /// Survivors the force token names; the rest of `remaining` was only
    /// observed (outside what was stopped) and will never be signalled.
    var forceable: Int?
    var observed: Int = 0
    var kept: [String] = []
    var forceToken: String?
    var usedBefore: Double?, usedAfter: Double?
    /// Per-instance liveness from verify-app's instances[].status.
    var alive: InstanceLiveness?

    /// How many listed survivors Force would act on, and how many it would not.
    var forceSplit: (forceable: Int, outside: Int) {
        let n = forceable ?? max(remaining - observed, 0)
        return (n, observed > 0 ? observed : max(remaining - n, 0))
    }

    static func decode(_ data: Data) -> ActOutcome? {
        guard let j = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let result = str(j["result"]) else { return nil }
        var o = ActOutcome(result: result, reason: str(j["reason"]), exited: int(j["exited"]),
                           captured: int(j["captured"]),
                           remaining: (j["remaining"] as? [Any])?.count ?? 0,
                           forceable: int(j["forceable"]),
                           observed: (j["observed"] as? [Any])?.count ?? int(j["observed"]) ?? 0,
                           kept: strs(j["kept"]) ?? [], forceToken: str(j["force_token"]),
                           usedBefore: num(j["used_bytes_before"]),
                           usedAfter: num(j["used_bytes_after"]))
        if let list = j["instances"] as? [[String: Any]] {
            var alive: InstanceLiveness = [:]
            for i in list {
                guard let pid = int(i["pid"]), let status = str(i["status"]) else { continue }
                alive[Int32(pid)] = status != "exited"
            }
            o.alive = alive
        }
        return o
    }
}

enum ActView {
    case success(ActOutcome)
    case partial(ActOutcome)
    case refused(ActOutcome)
    case error(String)

    var name: String {
        switch self {
        case .success: return "success"
        case .partial: return "partial"
        case .refused: return "refused"
        case .error: return "error"
        }
    }

    /// Stdout JSON first, then the exit code it must agree with. Exit 0, 3 and 4
    /// carry a result; anything else, a mismatch, a timeout or unreadable output
    /// is an error, because the real outcome is unknown.
    static func classify(_ r: CLIResult, timeout: Double) -> ActView {
        if r.timedOut {
            return .error("memmon did not answer within \(String(format: "%g", timeout)) s. It may still be finishing.")
        }
        if let e = r.launchError { return .error(e) }
        let outcome = ActOutcome.decode(r.stdout)
        guard let exit = r.exit else { return .error("memmon exited abnormally.") }
        guard let o = outcome else {
            return .error(exit == 1 ? "memmon reported an error." : "memmon's answer could not be read.")
        }
        switch (exit, o.result) {
        case (0, "stopped"), (0, "force_stopped"), (0, "already_exited"), (0, "respawned"),
             (3, "respawned"):
            return .success(o)
        case (3, "partial"):
            return .partial(o)
        case (4, "refused"):
            return .refused(o)
        case (1, _):
            return .error(o.reason.map { "memmon reported an error: \($0)." } ?? "memmon reported an error.")
        default:
            return .error("memmon's answer did not match its exit status.")
        }
    }
}

// MARK: - quitting GUI apps

/// What the quit flow needs from a running app. NSRunningApplication already
/// has this shape; tests substitute a fake so no real app is ever touched.
protocol RunningAppHandle: AnyObject {
    var bundleIdentifier: String? { get }
    var launchDate: Date? { get }
    var isTerminated: Bool { get }
    func terminate() -> Bool
    func forceTerminate() -> Bool
}

extension NSRunningApplication: RunningAppHandle {}

protocol AppControl {
    func app(pid: Int32) -> RunningAppHandle?
    func now() -> Double
    func sleep(_ seconds: Double)
}

struct SystemApps: AppControl {
    func app(pid: Int32) -> RunningAppHandle? { NSRunningApplication(processIdentifier: pid) }
    func now() -> Double { ProcessInfo.processInfo.systemUptime }
    func sleep(_ seconds: Double) { Thread.sleep(forTimeInterval: seconds) }
}

struct AppTokenInstance { var pid: Int32; var launchDate: Double? }

/// The quit-app token minted by owners --json: unsigned base64 JSON naming the
/// bundle and each instance's PID and launch date.
struct AppToken {
    var bundleId: String
    var instances: [AppTokenInstance]

    static func decode(_ token: String) -> AppToken? {
        var b64 = token.replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        while b64.count % 4 != 0 { b64 += "=" }
        guard let data = Data(base64Encoded: b64),
              let j = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              str(j["action"]) == "quit-app",
              let bundle = str(j["bundle_id"]),
              let list = j["instances"] as? [[String: Any]], !list.isEmpty else { return nil }
        let instances = list.compactMap { i -> AppTokenInstance? in
            guard let pid = int(i["pid"]) else { return nil }
            return AppTokenInstance(pid: Int32(pid), launchDate: launchDate(i["launch_date"]))
        }
        guard instances.count == list.count else { return nil }
        return AppToken(bundleId: bundle, instances: instances)
    }
}

enum InstanceState: String {
    case exited, running, changed
    case alreadyExited = "already_exited"
    case forceStopped = "force_stopped"
    /// No NSRunningApplication for this PID: a helper or widget, not something
    /// that can be asked to quit. It is never reported as quit.
    case notAnApp = "not_an_app"
    /// The final check by process identity could not be made.
    case unverified

    var done: Bool { self == .exited || self == .alreadyExited || self == .forceStopped }
    var label: String {
        switch self {
        case .exited: return "quit"
        case .running: return "still running"
        case .changed: return "changed since the sample, not touched"
        case .alreadyExited: return "had already quit"
        case .forceStopped: return "force-quit"
        case .notAnApp: return "is not an app memmon can quit, not touched"
        case .unverified: return "could not be verified"
        }
    }
}

struct InstanceOutcome {
    var pid: Int32
    var state: InstanceState
}

/// Per-instance liveness by process identity (pid → still running), as
/// `memmon act verify-app` reports it; nil when that check failed.
typealias InstanceLiveness = [Int32: Bool]

struct QuitApp {
    let control: AppControl
    /// Asks memmon, by libproc identity, which instances are still running.
    /// Only this decides that an instance has exited.
    let verify: () -> InstanceLiveness?
    var watch = 10.0
    var poll = 0.2
    var launchTolerance = 2.0

    private enum Identity { case notAnApp, changed, same(RunningAppHandle) }

    /// An instance is only touched while its bundle identifier and launch date
    /// still match the token; a PID reused by anything else is left alone.
    private func identity(_ i: AppTokenInstance, _ bundle: String) -> Identity {
        guard let h = control.app(pid: i.pid) else { return .notAnApp }
        guard h.bundleIdentifier == bundle,
              let expected = i.launchDate, let actual = h.launchDate?.timeIntervalSince1970,
              abs(actual - expected) <= launchTolerance else { return .changed }
        return .same(h)
    }

    func quit(_ t: AppToken, alive initial: InstanceLiveness?) -> [InstanceOutcome] {
        var out = t.instances.map { InstanceOutcome(pid: $0.pid, state: .running) }
        var sent: [(Int, RunningAppHandle)] = []
        for (k, inst) in t.instances.enumerated() {
            if initial?[inst.pid] == false { out[k].state = .alreadyExited; continue }
            switch identity(inst, t.bundleId) {
            case .notAnApp: out[k].state = .notAnApp
            case .changed: out[k].state = .changed
            case .same(let h): _ = h.terminate(); sent.append((k, h))
            }
        }
        settle(sent, t, &out, as: .exited)
        return out
    }

    /// forceTerminate() is a separate choice, and only for instances the quit
    /// reported as still running.
    func force(_ t: AppToken, after prior: [InstanceOutcome]) -> [InstanceOutcome] {
        var out = prior
        var sent: [(Int, RunningAppHandle)] = []
        var vanished: [Int] = []
        for (k, o) in prior.enumerated() where o.state == .running {
            guard let inst = t.instances.first(where: { $0.pid == o.pid }) else { continue }
            switch identity(inst, t.bundleId) {
            case .notAnApp: vanished.append(k)
            case .changed: out[k].state = .changed
            case .same(let h): _ = h.forceTerminate(); sent.append((k, h))
            }
        }
        settle(sent, t, &out, as: .forceStopped, vanished: vanished)
        return out
    }

    /// Watches the signalled instances for up to `watch` seconds, then lets
    /// memmon's identity check, not AppKit, decide which of them exited. An
    /// instance AppKit lost between the quit and Force (`vanished`) was not
    /// signalled: it quit on its own or it is no longer reachable as an app.
    private func settle(_ sent: [(Int, RunningAppHandle)], _ t: AppToken,
                        _ out: inout [InstanceOutcome], as finished: InstanceState,
                        vanished: [Int] = []) {
        guard !sent.isEmpty || !vanished.isEmpty else { return }
        let deadline = control.now() + watch
        var pending = sent
        while true {
            pending.removeAll { k, h in
                if h.isTerminated { return true }
                if case .same = identity(t.instances[k], t.bundleId) { return false }
                return true
            }
            if pending.isEmpty || control.now() >= deadline { break }
            control.sleep(poll)
        }
        let alive = verify()
        for (k, _) in sent {
            switch alive?[t.instances[k].pid] {
            case false?: out[k].state = finished
            case true?: out[k].state = .running
            case nil: out[k].state = .unverified
            }
        }
        for k in vanished {
            switch alive?[t.instances[k].pid] {
            case false?: out[k].state = .exited
            case true?: out[k].state = .notAnApp
            case nil: out[k].state = .unverified
            }
        }
    }
}

// MARK: - copy for outcomes

struct Banner {
    enum Tone { case success, warning, error }
    var tone: Tone
    var title: String
    var body: String
    var note: String?
    var offersRefresh = false
}

enum Copy {
    static let staleForce = "this result is more than 2 minutes old. Refresh and try again."

    static func measured(_ o: ActOutcome) -> (String?, String?) {
        guard let before = o.usedBefore, let after = o.usedAfter else { return (nil, nil) }
        let drop = before - after
        let text = drop >= 0.05 * GB
            ? "used memory \(gb(drop)) lower at the next sample"
            : "used memory not lower at the next sample"
        return (text, "(measured; other apps also change)")
    }

    static func refusal(_ reason: String?, noun: String, forcing: Bool) -> (String, Bool) {
        switch reason {
        case "stale_token" where forcing:
            return (staleForce, true)
        case "target_changed", "ownership_changed", "stale_token", "instance_changed", "lease_mismatch":
            return ("this \(noun) changed since the list was sampled. Refresh and try again.", true)
        case "busy":
            return ("another stop is still in progress. Try again in a moment.", false)
        case "protected":
            return ("this \(noun) is protected: it is the session itself or belongs to another owner.", false)
        case "degraded_identity":
            return ("process identity is unavailable (limited inventory), so nothing can be stopped safely.", false)
        case "hosts_sessions":
            return ("this app hosts agent sessions; quit it from the app itself.", false)
        case "not_stoppable":
            return ("memmon cannot stop this kind of owner.", false)
        case let r?:
            return ("memmon declined (\(r.replacingOccurrences(of: "_", with: " "))).", false)
        default:
            return ("memmon declined.", false)
        }
    }

    /// Survivors are never reported as success: whatever the result word, a
    /// listed survivor makes the banner a partial one.
    static func partial(_ o: ActOutcome, subject: String) -> Banner {
        let left = plural(o.remaining, "process", "processes")
        switch o.reason {
        case "root_exited":
            return Banner(tone: .warning, title: "\(subject) had exited",
                          body: "— but \(o.remaining) of its processes are still running · nothing was signalled",
                          offersRefresh: true)
        case "outside_force":
            return Banner(tone: .warning, title: "\(subject) partly stopped",
                          body: "· \(o.exited ?? 0) force-stopped · \(o.remaining) still running outside what was stopped",
                          offersRefresh: true)
        default:
            return Banner(tone: .warning, title: "\(subject) partly stopped",
                          body: "· \(left) still running", offersRefresh: true)
        }
    }

    static func banner(_ view: ActView, subject: String, noun: String, forcing: Bool = false) -> Banner {
        switch view {
        case .success(let o):
            if o.remaining > 0 { return partial(o, subject: subject) }
            let (m, note) = measured(o)
            var parts: [String] = []
            let title: String
            switch o.result {
            case "already_exited":
                title = "\(subject) had already exited"
                parts.append("nothing was signalled")
            case "respawned":
                // The worker came back under the same job, so nothing is
                // left to force; the honest outcome is that it runs again.
                return Banner(tone: .warning, title: "Session restarted by Claude",
                              body: "— it is running again.", offersRefresh: true)
            case "force_stopped":
                title = "\(subject) force-stopped"
            default:
                title = "\(subject) stopped"
            }
            if let n = o.exited, o.result != "already_exited" {
                parts.append("\(n) of \(o.captured ?? n + o.remaining) processes exited")
            }
            if !o.kept.isEmpty { parts.append("\(plural(o.kept.count, "nested session")) kept running") }
            if let m { parts.append(m) }
            return Banner(tone: .success, title: title, body: "· " + parts.joined(separator: " · "), note: note)
        case .refused(let o):
            let (text, refresh) = refusal(o.reason, noun: noun, forcing: forcing)
            return Banner(tone: .warning, title: forcing ? "Not force-stopped" : "Not stopped",
                          body: "— " + text, offersRefresh: refresh)
        case .partial(let o):
            return partial(o, subject: subject)
        case .error(let message):
            return Banner(tone: .error, title: "Result unknown",
                          body: "— \(message) Refresh to see what is still running.", offersRefresh: true)
        }
    }

    static func appBanner(_ title: String, _ results: [InstanceOutcome], forced: Bool) -> Banner {
        let n = results.count
        let done = results.filter { $0.state.done }.count
        let detail = results.enumerated().map { k, r in "instance \(k + 1) \(r.state.label)" }
            .joined(separator: " · ")
        if done == n {
            return Banner(tone: .success, title: forced ? "\(title) force-quit" : "\(title) quit",
                          body: "· \(done) of \(n) instances exited · " + detail,
                          note: "Used memory updates at the next sample.")
        }
        if results.contains(where: { $0.state == .running }) {
            return Banner(tone: .warning, title: "\(title) still running",
                          body: "— " + detail, offersRefresh: true)
        }
        return Banner(tone: .warning, title: "Not fully quit",
                      body: "— " + detail + ". Refresh and try again.", offersRefresh: true)
    }
}

// MARK: - view model

struct ConfirmRequest {
    enum Kind {
        case job(OwnerJob)
        case endSession
        case quitApp
        case stopCommand
    }
    enum Phase {
        case ask
        case working
        case partial(ActOutcome)
        /// `watchEnded` is the uptime when the 10 s watch finished; Force is
        /// refused once that is more than 120 s ago.
        case appPartial(AppToken, [InstanceOutcome], watchEnded: Double)
    }
    var kind: Kind
    var owner: Owner
    var phase: Phase = .ask
    /// The pending result came from Force, so a refusal is about Force.
    var forcing = false
}

final class Model: ObservableObject {
    @Published var snap: OwnersSnap?
    @Published var loadError: String?
    @Published var refreshing = false
    /// A scan that timed out is still running; no second one is started.
    @Published var stillSampling = false
    @Published var sort: SortKey = .memory
    @Published var expanded: String?
    @Published var techOpen: Set<String> = []
    @Published var confirm: ConfirmRequest?
    @Published var banner: Banner?
    @Published var tick = 0

    var appControl: AppControl = SystemApps()
    /// Off for fixtures: a rendered or audited state must never call memmon.
    /// Confirmed actions are recorded in `actionLog` instead.
    var live = true
    var actionLog: [String] = []
    /// Which overlay button holds keyboard focus, as reported by the overlay.
    var overlayFocus: String?
    /// The clock the app-Force age bound reads; tests move it.
    var uptime: () -> Double = { ProcessInfo.processInfo.systemUptime }
    static let forceTTL = 120.0
    private var ticker: Timer?
    private var scannerBusy = false
    private var refreshQueued = false
    /// Every owners scan spawned, for the single-flight self-test.
    var scansStarted = 0

    /// Keeps "Sampled Ns ago" honest while the popover stays open.
    func startTicking(every seconds: Double = 5) {
        ticker?.invalidate()
        ticker = Timer.scheduledTimer(withTimeInterval: seconds, repeats: true) { [weak self] _ in
            self?.tick += 1
        }
    }

    func stopTicking() { ticker?.invalidate(); ticker = nil }

    var loaded: Bool { snap != nil }

    /// Full sync. The previous snapshot stays on screen meanwhile so the UI never
    /// blanks; a failure keeps it and says so instead of silently ageing.
    ///
    /// Single flight: while a scan (even a timed-out one) is still running, a
    /// request is queued and runs once that scan ends, so a post-action refresh
    /// is never dropped and scanners never pile up on a loaded machine.
    func refresh() {
        guard live else { return }
        if refreshing || scannerBusy { refreshQueued = true; return }
        refreshing = true
        scannerBusy = true
        scansStarted += 1
        // Listening-port lookups cost CPU, so memmon only runs them for the
        // owner that is open here; that is what tells a server from a build.
        var args = ["owners", "--json", "--cpu-window", "1.0"]
        if let open = expanded, open != "unknown:*" { args += ["--expand", open] }
        DispatchQueue.global(qos: .userInitiated).async {
            let r = CLI.run(args, timeout: CLI.ownersTimeout, onExit: {
                DispatchQueue.main.async {
                    self.scannerBusy = false
                    self.stillSampling = false
                    self.runQueued()
                }
            })
            var parsed: OwnersSnap?
            var failure: String?
            if r.timedOut {
                failure = "memmon did not answer within \(String(format: "%g", CLI.ownersTimeout)) s; it is still sampling"
            } else if let e = r.launchError {
                failure = e
            } else if let j = (try? JSONSerialization.jsonObject(with: r.stdout)) as? [String: Any],
                      let s = OwnersSnap.decode(j) {
                parsed = s
            } else {
                failure = r.exit == 0 ? "memmon's answer could not be read" : "memmon reported an error"
            }
            DispatchQueue.main.async {
                if let parsed { self.snap = parsed; self.loadError = nil } else { self.loadError = failure }
                self.refreshing = false
                self.stillSampling = self.scannerBusy
                self.runQueued()
            }
        }
    }

    private func runQueued() {
        guard refreshQueued, !refreshing, !scannerBusy else { return }
        refreshQueued = false
        refresh()
    }

    func toggleGate(_ pause: Bool) {
        guard live else { return }
        DispatchQueue.global(qos: .userInitiated).async {
            _ = CLI.run([pause ? "--off" : "--on"], timeout: CLI.ownersTimeout)
            DispatchQueue.main.async { self.refresh() }
        }
    }

    func ask(_ kind: ConfirmRequest.Kind, _ owner: Owner) {
        banner = nil
        confirm = ConfirmRequest(kind: kind, owner: owner)
    }

    /// Cancel, Leave running or Esc. Leaving a partial result says what was
    /// left running instead of dropping the outcome.
    func cancel() {
        guard let c = confirm else { return }
        if case .working = c.phase { return }
        confirm = nil
        switch c.phase {
        case .partial(let o):
            banner = Copy.partial(o, subject: subject(c).0)
            refresh()
        case .appPartial(_, let results, _):
            banner = Copy.appBanner(c.owner.title, results, forced: false)
            refresh()
        default:
            break
        }
    }

    func subject(_ c: ConfirmRequest) -> (String, String) {
        switch c.kind {
        case .job(let j):
            let noun = j.kind == "server" ? "server" : (j.kind == "test" ? "test run" : "build")
            return (j.displayName.components(separatedBy: " · ").first ?? j.label, noun)
        case .endSession: return (c.owner.title, "session")
        case .quitApp, .stopCommand: return (c.owner.title, "app")
        }
    }

    func perform() {
        guard var c = confirm else { return }
        guard live else { actionLog.append("perform"); return }
        switch c.kind {
        case .stopCommand:
            confirm = nil
            return
        case .quitApp:
            guard let tok = c.owner.token, let t = AppToken.decode(tok) else {
                confirm = nil
                banner = Banner(tone: .error, title: "Not quit",
                                body: "— the quit request could not be read. Refresh and try again.",
                                offersRefresh: true)
                return
            }
            c.phase = .working; confirm = c
            let control = appControl
            DispatchQueue.global(qos: .userInitiated).async {
                let result = Model.quitApp(token: tok, parsed: t, control: control)
                DispatchQueue.main.async { self.applyApp(result, c, t, forced: false) }
            }
        case .job(let j):
            run(["act", j.stopAction, "--target", j.token ?? ""], c)
        case .endSession:
            run(["act", "end-session", "--target", c.owner.token ?? ""], c)
        }
    }

    func force() {
        guard var c = confirm else { return }
        if case .appPartial(_, _, let ended) = c.phase, uptime() - ended > Model.forceTTL {
            confirm = nil
            banner = Banner(tone: .warning, title: "Not force-quit",
                            body: "— " + Copy.staleForce, offersRefresh: true)
            refresh()
            return
        }
        guard live else { actionLog.append("force"); return }
        c.forcing = true
        switch c.phase {
        case .partial(let o):
            guard let tok = o.forceToken else { cancel(); return }
            run(["act", "force", "--target", tok], c)
        case .appPartial(let t, let prior, _):
            c.phase = .working; confirm = c
            let control = appControl
            DispatchQueue.global(qos: .userInitiated).async {
                let result = Model.forceApp(token: c.owner.token ?? "", t, prior, control: control)
                DispatchQueue.main.async { self.applyApp(result, c, t, forced: true) }
            }
        default:
            break
        }
    }

    private func run(_ args: [String], _ c: ConfirmRequest) {
        var c = c
        c.phase = .working; confirm = c
        DispatchQueue.global(qos: .userInitiated).async {
            let r = CLI.run(args, timeout: CLI.actTimeout)
            let view = ActView.classify(r, timeout: CLI.actTimeout)
            DispatchQueue.main.async { self.apply(view, c) }
        }
    }

    func apply(_ view: ActView, _ c: ConfirmRequest) {
        if !c.forcing, case .partial(let o) = view, o.forceToken != nil, o.forceSplit.forceable > 0 {
            var next = c
            next.phase = .partial(o)
            confirm = next
            return
        }
        let (subject, noun) = self.subject(c)
        confirm = nil
        banner = Copy.banner(view, subject: subject, noun: noun, forcing: c.forcing)
        refresh()
    }

    /// The overlay is not kept across a closed popover: a partial result
    /// reopened later would offer a Force on a stale picture.
    func popoverClosed() {
        if case .working? = confirm?.phase { return }
        if confirm != nil { cancel() }
    }

    enum AppResult {
        case refused(ActOutcome)
        case error(String)
        case done([InstanceOutcome])
    }

    /// verify-app with the held lock's descriptor inherited.
    static func verifyApp(_ token: String, fd: Int32) -> ActView {
        verifyView(CLI.run(["act", "verify-app", "--target", token, "--lock-fd", "\(fd)"],
                           timeout: CLI.actTimeout, inheritFD: fd))
    }

    /// Liveness from a verify-app answer: every instance gone, or the
    /// per-instance statuses; nil when memmon could not say.
    static func liveness(_ view: ActView, _ t: AppToken) -> InstanceLiveness? {
        guard case .success(let o) = view else { return nil }
        if o.result == "already_exited" {
            return Dictionary(uniqueKeysWithValues: t.instances.map { ($0.pid, false) })
        }
        return o.alive
    }

    /// Lock, have memmon verify every instance, quit and watch, then have
    /// memmon check again by process identity, all while holding the lock.
    static func quitApp(token: String, parsed: AppToken, control: AppControl,
                        watch: Double = 10) -> AppResult {
        switch ActionsLock.acquire() {
        case .busy:
            return .refused(ActOutcome(result: "refused", reason: "busy"))
        case .failed(let e):
            return .error(e)
        case .held(let fd):
            defer { ActionsLock.release(fd) }
            let first = verifyApp(token, fd: fd)
            switch first {
            case .refused(let o): return .refused(o)
            case .error(let e): return .error(e)
            default: break
            }
            let engine = QuitApp(control: control, verify: { liveness(verifyApp(token, fd: fd), parsed) },
                                 watch: watch)
            return .done(engine.quit(parsed, alive: liveness(first, parsed)))
        }
    }

    static func verifyView(_ r: CLIResult) -> ActView {
        if r.timedOut { return .error("memmon did not answer within \(String(format: "%g", CLI.actTimeout)) s.") }
        if let e = r.launchError { return .error(e) }
        guard let o = ActOutcome.decode(r.stdout), let exit = r.exit else {
            return .error("memmon's answer could not be read.")
        }
        switch (exit, o.result) {
        case (0, "verified"), (0, "already_exited"): return .success(o)
        case (4, "refused"): return .refused(o)
        default: return .error("memmon could not verify the app.")
        }
    }

    static func forceApp(token: String, _ t: AppToken, _ prior: [InstanceOutcome],
                         control: AppControl, watch: Double = 10) -> AppResult {
        switch ActionsLock.acquire() {
        case .busy: return .refused(ActOutcome(result: "refused", reason: "busy"))
        case .failed(let e): return .error(e)
        case .held(let fd):
            defer { ActionsLock.release(fd) }
            let engine = QuitApp(control: control, verify: { liveness(verifyApp(token, fd: fd), t) },
                                 watch: watch)
            return .done(engine.force(t, after: prior))
        }
    }

    func applyApp(_ result: AppResult, _ c: ConfirmRequest, _ t: AppToken, forced: Bool) {
        switch result {
        case .refused(let o):
            confirm = nil
            banner = Copy.banner(.refused(o), subject: c.owner.title, noun: "app", forcing: forced)
        case .error(let e):
            confirm = nil
            banner = Copy.banner(.error(e), subject: c.owner.title, noun: "app")
        case .done(let outcomes):
            if !forced && outcomes.contains(where: { $0.state == .running }) {
                var next = c
                next.phase = .appPartial(t, outcomes, watchEnded: uptime())
                confirm = next
                return
            }
            confirm = nil
            banner = Copy.appBanner(c.owner.title, outcomes, forced: forced)
        }
        refresh()
    }
}

// MARK: - building blocks

struct Chip: View {
    var text: String
    var tint: Color? = nil
    var body: some View {
        Text(text)
            .font(ft(11))
            .foregroundColor(tint ?? P.muted)
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, 6)
            .frame(height: 16)
            .background(Capsule().fill(tint.map { $0.opacity(0.16) } ?? P.soft))
    }
}

struct ConfidenceChip: View {
    var confidence: String?
    var body: some View {
        let c = confidence ?? "unknown"
        Chip(text: c.capitalized)
            .accessibilityElement()
            .accessibilityLabel("ownership confidence: \(c)")
    }
}

struct Meter: View {
    var value: Double?           // 0…1; nil draws the empty track only
    var tint: Color
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 8).fill(P.soft)
                if let value {
                    RoundedRectangle(cornerRadius: 8).fill(tint)
                        .frame(width: max(2, g.size.width * min(max(value, 0), 1)))
                }
            }
        }
        .frame(height: 6)
    }
}

struct Chevron: View {
    var open: Bool
    var body: some View {
        Image(systemName: "chevron.right")
            .font(.system(size: 9, weight: .bold))
            .foregroundColor(P.muted)
            .rotationEffect(.degrees(open ? 90 : 0))
    }
}

struct ActionButton: View {
    enum Variant { case primary, danger, secondaryDanger, secondary, link, icon }
    var title: String
    var icon: String? = nil
    var variant: Variant = .secondary
    var action: () -> Void
    @State private var hover = false

    private var fg: Color {
        switch variant {
        case .primary, .danger: return P.onTint
        case .secondaryDanger: return P.red
        case .secondary: return P.text
        case .link, .icon: return hover ? P.text : P.muted
        }
    }
    private var fill: Color {
        switch variant {
        case .primary: return P.accent
        case .danger: return P.red
        case .secondary, .secondaryDanger: return hover ? P.selected : P.soft
        case .link, .icon: return .clear
        }
    }
    private var outlined: Bool { variant == .secondary || variant == .secondaryDanger }

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                if let icon { Image(systemName: icon).font(.system(size: 11, weight: .semibold)) }
                if variant != .icon { Text(title).font(ft(12)).lineLimit(1) }
            }
            .foregroundColor(fg)
            .padding(.horizontal, variant == .icon ? 4 : (variant == .link ? 6 : 11))
            .padding(.vertical, variant == .icon ? 4 : 6)
            .background(RoundedRectangle(cornerRadius: 8).fill(fill))
            .overlay(RoundedRectangle(cornerRadius: 8)
                .stroke(outlined ? P.border : .clear, lineWidth: 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .fixedSize()
        .onHover { hover = $0 }
        .accessibilityLabel(title)
    }
}

struct OwnerIcon: View {
    var owner: Owner
    var selected: Bool
    var symbol: String {
        if !owner.group.isEmpty || owner.isUnattributed { return "questionmark" }
        switch owner.agent {
        case "claude", "job": return "terminal"
        case "codex": return "curlybraces"
        case "app": return "globe"
        case "service": return "shippingbox"
        default: return "questionmark"
        }
    }
    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: 13, weight: .medium))
            .foregroundColor(selected ? P.accent : P.muted)
            .frame(width: 32, height: 32)
            .background(RoundedRectangle(cornerRadius: 9).fill(P.panel))
            .overlay(RoundedRectangle(cornerRadius: 9).stroke(P.border, lineWidth: 1))
            .accessibilityHidden(true)
    }
}

/// The right-hand column: the sort metric first, then memory or CPU, and a
/// visible reason wherever a value is unavailable — never a zero.
struct UsageColumn: View {
    var owner: Owner
    var sort: SortKey

    private var memText: String { owner.footprint.map(gb) ?? "— \(memReason)" }
    private var memReason: String { owner.footprintReason ?? "not measured" }
    private var cpuReason: String { owner.cpuReason ?? "warming up" }
    private var growthReason: String { owner.growthReason ?? "not enough history" }

    var lines: (String, String, String?) {
        switch sort {
        case .memory:
            if owner.footprint == nil { return ("—", owner.cpu.map(coresText) ?? "— cores", memReason) }
            return (memText, owner.cpu.map(coresText) ?? "— cores",
                    owner.cpu == nil ? "CPU \(cpuReason)" : nil)
        case .cpu:
            if let c = owner.cpu { return (coresText(c), memText, nil) }
            return ("—", memText, "CPU \(cpuReason)")
        case .growth:
            if let g = owner.growth { return (growthText(g), memText, nil) }
            return ("—", memText, growthReason)
        }
    }

    var spoken: String {
        let mem = owner.footprint.map(gb) ?? "memory not available, \(memReason)"
        let cpu = owner.cpu.map(coresText) ?? "CPU not available, \(cpuReason)"
        let growth = owner.growth.map { "growth \(growthText($0)) per 10 minutes" }
            ?? "growth not available, \(growthReason)"
        switch sort {
        case .memory: return "\(mem), \(cpu)"
        case .cpu: return "\(cpu), \(mem)"
        case .growth: return "\(growth), \(mem)"
        }
    }

    var body: some View {
        let (primary, secondary, reason) = lines
        VStack(alignment: .trailing, spacing: 2) {
            Text(primary).font(ft(16, .medium)).foregroundColor(P.text)
            Text(secondary).font(ft(12)).foregroundColor(P.muted)
                .multilineTextAlignment(.trailing).fixedSize(horizontal: false, vertical: true)
            if let reason {
                Text(reason).font(ft(11)).foregroundColor(P.muted)
                    .multilineTextAlignment(.trailing).fixedSize(horizontal: false, vertical: true)
            }
        }
        .monospacedDigit()
    }
}

struct OwnerRow: View {
    var owner: Owner
    var sort: SortKey
    var expanded: Bool
    var onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack(alignment: .top, spacing: 10) {
                OwnerIcon(owner: owner, selected: expanded)
                VStack(alignment: .leading, spacing: 2) {
                    Text(owner.title).font(ft(14, .medium)).foregroundColor(P.text).lineLimit(1)
                    Text(owner.line2).font(ft(12)).foregroundColor(P.muted)
                        .lineLimit(2).truncationMode(.tail)
                        .fixedSize(horizontal: false, vertical: true)
                    // Like an inline chip in running text: it follows line 3
                    // when both fit, and drops below it rather than clipping it.
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 4) {
                            Text(owner.line3 + " ·").lineLimit(1).fixedSize()
                            ConfidenceChip(confidence: owner.confidence)
                        }
                        VStack(alignment: .leading, spacing: 3) {
                            Text(owner.line3).lineLimit(2).truncationMode(.tail)
                                .fixedSize(horizontal: false, vertical: true)
                            ConfidenceChip(confidence: owner.confidence)
                        }
                    }
                    .font(ft(11)).foregroundColor(P.muted)
                    .padding(.top, 1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                UsageColumn(owner: owner, sort: sort).frame(width: 88, alignment: .trailing)
            }
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10).fill(expanded ? P.selected : Color.clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(owner.title), \(owner.line2), \(owner.line3), "
            + "ownership confidence: \(owner.confidence ?? "unknown"), "
            + UsageColumn(owner: owner, sort: sort).spoken)
        .accessibilityValue(expanded ? "expanded" : "collapsed")
        .accessibilityHint(expanded ? "Hides details" : "Shows details")
        .accessibilityAddTraits(.isButton)
    }
}

struct KeptMarker: View {
    var text = "Kept"
    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: "checkmark.shield").font(.system(size: 11, weight: .semibold))
            Text(text).font(ft(11))
        }
        .foregroundColor(P.green)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("kept running")
    }
}

struct ChildJobRow: View {
    var job: OwnerJob
    var owner: Owner
    var enabled: Bool
    var onStop: () -> Void

    private var meta: String {
        var parts = [job.footprint.map(gb) ?? "— not measured"]
        if job.isConversation {
            parts.append("stays open when you stop a build")
        } else if let n = job.memberCount {
            parts.append(plural(n, "process", "processes"))
        }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(job.isConversation ? "Conversation" : job.displayName)
                    .font(ft(13, .medium)).foregroundColor(P.text)
                Text(meta).font(ft(12)).foregroundColor(P.muted).monospacedDigit()
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel((job.isConversation ? "Conversation" : job.displayName) + ", " + meta)
            if job.isConversation {
                KeptMarker()
            } else if job.token != nil && enabled {
                ActionButton(title: job.stopLabel, icon: "stop.circle", variant: .secondaryDanger,
                             action: onStop)
                    .accessibilityLabel("\(job.stopLabel): \(job.displayName) in \(owner.title)")
            }
        }
        .padding(.vertical, 10)
        .overlay(Rectangle().fill(P.border).frame(height: 1), alignment: .bottom)
    }
}

struct DetailLine: View {
    var left: String, right: String
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(left).foregroundColor(P.muted).frame(width: 96, alignment: .leading)
            Text(right).foregroundColor(P.text).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .font(ft(11)).monospacedDigit()
        .accessibilityElement(children: .combine)
    }
}

struct OwnerDetailCard: View {
    var owner: Owner
    var degraded: Bool
    @Binding var techOpen: Bool
    var animation: Animation?
    var onAsk: (ConfirmRequest.Kind) -> Void

    private var jobs: [OwnerJob] { owner.jobs }

    private var footText: String? {
        if owner.can("end-session") { return "Conversation + all its processes" }
        if !owner.hosts.isEmpty {
            return "Hosts \(plural(owner.hosts.count, "session")) — quit it from the app itself"
        }
        if owner.can("quit-app") {
            if owner.kind == "service" { return "The VM and all its containers" }
            return plural(owner.instances?.count ?? 1, "instance")
        }
        if owner.can("stop-managed-job") { return "The job and its runner" }
        if owner.stopCommand != nil { return "No app to quit" }
        if owner.kind == "codex-app" { return "Shared by its threads — stop it from Codex" }
        if owner.kind == "service" { return "Shared — stop it from the app that owns it" }
        if owner.kind == "codex-ui" { return "Ending this window does not end the thread" }
        return nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Text(owner.title).font(ft(14, .medium)).foregroundColor(P.text).lineLimit(1)
                Spacer(minLength: 4)
                Text(owner.detailTag).font(ft(11)).foregroundColor(P.accent)
            }
            .padding(.horizontal, 13).padding(.vertical, 11)
            .overlay(Rectangle().fill(P.border).frame(height: 1), alignment: .bottom)

            VStack(alignment: .leading, spacing: 0) {
                ForEach(jobs) { j in
                    ChildJobRow(job: j, owner: owner, enabled: !degraded) {
                        onAsk(.job(j))
                    }
                }
                if !owner.group.isEmpty {
                    ForEach(Array(owner.group.enumerated()), id: \.offset) { k, o in
                        ChildJobRow(job: OwnerJob(id: o.id, kind: "tree",
                                                  label: "Process tree \(k + 1)",
                                                  footprint: o.footprint,
                                                  memberCount: o.memberCount, token: nil, action: nil),
                                    owner: owner, enabled: false) {}
                    }
                }
                if let shared = owner.sharedWith, !shared.isEmpty {
                    Text(sharedLine(shared)).font(ft(12)).foregroundColor(P.muted)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 10)
                }
                if let inst = owner.instances, inst.count > 1 {
                    Text("\(inst.count) instances · each is checked before it is asked to quit")
                        .font(ft(12)).foregroundColor(P.muted).padding(.top, 10)
                }
                if degraded && !owner.actions.isEmpty {
                    Text("Stopping is off while process identity is unavailable.")
                        .font(ft(12)).foregroundColor(P.amber).padding(.top, 10)
                        .fixedSize(horizontal: false, vertical: true)
                }
                technical
            }
            .padding(.horizontal, 13).padding(.bottom, 10)

            if let footText {
                HStack(spacing: 10) {
                    Text(footText).font(ft(12)).foregroundColor(P.muted)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 4)
                    footButton
                }
                .padding(.leading, 13).padding(.trailing, 9).padding(.vertical, 7)
                .overlay(Rectangle().fill(P.border).frame(height: 1), alignment: .top)
            }
        }
        .panel(12)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(owner.title) details")
    }

    private func sharedLine(_ names: [String]) -> String {
        let head = names.prefix(3).joined(separator: ", ")
        let more = names.count > 3 ? " … (+\(names.count - 3))" : ""
        let what = owner.kind == "codex-app" ? "Threads" : "Containers"
        return "\(what): \(head)\(more)"
    }

    @ViewBuilder private var footButton: some View {
        if degraded {
            EmptyView()
        } else if owner.can("end-session") {
            ActionButton(title: "End session…", variant: .link) { onAsk(.endSession) }
                .accessibilityLabel("End session \(owner.title) (asks to confirm)")
        } else if owner.can("quit-app") {
            ActionButton(title: "Quit app…", variant: .link) { onAsk(.quitApp) }
                .accessibilityLabel("Quit \(owner.title) (asks to confirm)")
        } else if owner.can("stop-managed-job") {
            ActionButton(title: "Stop job…", variant: .link) {
                onAsk(.job(OwnerJob(id: owner.id, kind: "managed", label: owner.title,
                                    footprint: owner.footprint, memberCount: owner.memberCount,
                                    token: owner.token, action: "stop-managed-job")))
            }
            .accessibilityLabel("Stop job \(owner.title) (asks to confirm)")
        } else if owner.stopCommand != nil {
            ActionButton(title: "Stop command…", variant: .link) { onAsk(.stopCommand) }
                .accessibilityLabel("Show the command that stops \(owner.title)")
        }
    }

    private var technical: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button { withAnimation(animation) { techOpen.toggle() } } label: {
                HStack(spacing: 5) {
                    Chevron(open: techOpen)
                    Text("Technical details").font(ft(11)).foregroundColor(P.muted)
                }
                .padding(.vertical, 3)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Technical details")
            .accessibilityValue(techOpen ? "expanded" : "collapsed")
            if techOpen {
                VStack(alignment: .leading, spacing: 4) {
                    if let pid = owner.rootPid { DetailLine(left: "Root PID", right: "\(pid)") }
                    if let start = owner.rootStart, let sec = start.first {
                        let usec = start.count > 1 ? start[1] : 0
                        DetailLine(left: "Started", right: eventClock(sec + usec / 1e6, seconds: true))
                    }
                    if let n = owner.memberCount { DetailLine(left: "Processes", right: "\(n)") }
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text("Confidence").foregroundColor(P.muted).frame(width: 96, alignment: .leading)
                        ConfidenceChip(confidence: owner.confidence)
                        Text(confidenceNote).foregroundColor(P.text)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .font(ft(11))
                    ForEach(jobs.filter { !$0.isConversation }) { j in
                        if let pid = j.rootPid {
                            DetailLine(left: "\(j.displayName.components(separatedBy: " · ")[0]) PID",
                                       right: pid)
                        }
                    }
                    ForEach(Array(owner.group.enumerated()), id: \.offset) { k, o in
                        DetailLine(left: "Tree \(k + 1) root", right: o.rootPid.map { "PID \($0)" } ?? o.id)
                    }
                    if let inst = owner.instances {
                        ForEach(Array(inst.enumerated()), id: \.offset) { k, i in
                            DetailLine(left: "Instance \(k + 1)",
                                       right: "PID \(i.pid)" + (i.launchDate.map { " · launched \(eventClock($0))" } ?? ""))
                        }
                    }
                    if let cov = owner.cpuCoverage, cov < 1, owner.cpu != nil {
                        DetailLine(left: "CPU coverage", right: String(format: "%.0f%% of processes measured", cov * 100))
                    }
                    if owner.growth == nil {
                        DetailLine(left: "Growth", right: owner.growthReason ?? "not enough history")
                    }
                    DetailLine(left: "Owner ID", right: owner.id)
                }
                .padding(.top, 4).padding(.bottom, 2)
            }
        }
        .padding(.top, 9)
    }

    private var confidenceNote: String {
        switch owner.confidence {
        case "exact": return "process tree under the owner's root"
        case "inferred": return "matched by working directory and start time"
        case "shared": return "one process serves several owners"
        default: return "no owning session or app found"
        }
    }
}

struct OutcomeBanner: View {
    var banner: Banner
    var onRefresh: () -> Void
    var onDismiss: () -> Void

    private var icon: (String, Color) {
        switch banner.tone {
        case .success: return ("checkmark.circle", P.green)
        case .warning: return ("exclamationmark.triangle", P.amber)
        case .error: return ("xmark.octagon", P.red)
        }
    }

    private var text: Text {
        let title = Text(banner.title).fontWeight(.medium).foregroundColor(P.text)
        let body = Text(banner.body).foregroundColor(P.text)
        guard let note = banner.note else { return Text("\(title) \(body)") }
        return Text("\(title) \(body) \(Text(note).foregroundColor(P.muted))")
    }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon.0).font(.system(size: 13, weight: .semibold))
                .foregroundColor(icon.1).frame(width: 16).padding(.top, 1)
                .accessibilityHidden(true)
            text.font(ft(12)).fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityLabel(banner.title + " " + banner.body + (banner.note.map { " " + $0 } ?? ""))
            HStack(spacing: 2) {
                if banner.offersRefresh {
                    ActionButton(title: "Refresh", icon: "arrow.clockwise", action: onRefresh)
                        .accessibilityLabel("Refresh the process list")
                }
                ActionButton(title: "Dismiss", icon: "xmark", variant: .icon, action: onDismiss)
            }
            .padding(.top, -3)
        }
        .padding(.leading, 12).padding(.trailing, 8).padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 10)
            .fill(banner.tone == .success ? P.selected : P.panel))
        .overlay(RoundedRectangle(cornerRadius: 10)
            .stroke(banner.tone == .success ? Color.clear : P.border, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Outcome")
    }
}

struct CopyCommandField: View {
    var command: String
    @State private var copied = false
    var body: some View {
        HStack(spacing: 8) {
            Text(command).font(.system(size: 11, design: .monospaced)).foregroundColor(P.text)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            ActionButton(title: copied ? "Copied" : "Copy", icon: "doc.on.doc") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(command, forType: .string)
                copied = true
            }
            .accessibilityLabel("Copy command \(command)")
        }
        .padding(.leading, 10).padding(.trailing, 6).padding(.vertical, 6)
        .panel(8)
    }
}

/// The in-view confirmation that replaces NSAlert: it keeps the target row
/// visible behind the scrim, renders offscreen, and starts on the safe button.
struct ConfirmOverlay: View {
    var request: ConfirmRequest
    var onCancel: () -> Void
    var onConfirm: () -> Void
    var onForce: () -> Void
    var onFocus: (String?) -> Void = { _ in }

    enum Field: Hashable { case safe, act }
    @FocusState private var focus: Field?

    private var owner: Owner { request.owner }
    private var working: Bool { if case .working = request.phase { return true }; return false }

    private var keepsConversation: Bool { owner.agent == "claude" || owner.agent == "codex" }

    private struct Content {
        var icon: String, tint: Color, title: String
        var target: String
        var sub: String? = nil
        var list: [String] = []
        var warn = false
        var message: String
        var safe: String?
        var command: String?
        var safeButton: String, safeSpoken: String
        var actButton: String?, actSpoken: String = "", actVariant: ActionButton.Variant = .danger
    }

    private var content: Content {
        switch request.phase {
        case .partial(let o):
            // Force acts only on the survivors its token names; anything only
            // observed (outside what was stopped) is counted but never signalled.
            let (n, k) = o.forceSplit
            let m = n + k
            let label: String
            if case .job(let j) = request.kind { label = "\(j.displayName.components(separatedBy: " · ")[0]) in \(owner.title)" } else { label = owner.title }
            let exited = o.exited ?? 0
            let scope = k > 0
                ? "Force stop \(n) of \(m) — \(k) \(k == 1 ? "is" : "are") outside what was stopped and won't be signalled. "
                : ""
            return Content(icon: "exclamationmark.triangle", tint: P.amber,
                           title: "\(plural(m, "process", "processes")) still running",
                           target: label,
                           sub: "\(exited) of \(o.captured ?? exited + n) exited · \(m) still running after 10 s",
                           message: scope + "They have not answered the polite stop signal. Force stop ends them immediately; any output they have not written is lost."
                               + (owner.agent == "codex" && isEndSession
                                  ? " A Codex terminal stopped this way may need `reset` afterwards." : ""),
                           safe: isEndSession ? nil : (keepsConversation ? "The conversation keeps running either way." : nil),
                           safeButton: "Leave running", safeSpoken: "Leave the \(plural(m, "remaining process", "remaining processes")) running",
                           actButton: k > 0 ? "Force stop \(n)" : "Force stop",
                           actSpoken: "Force stop \(n) of the \(plural(m, "remaining process", "remaining processes"))",
                           actVariant: .secondaryDanger)
        case .appPartial(_, let results, _):
            let running = results.filter { $0.state == .running }.count
            let list = results.enumerated().map { k, r in "Instance \(k + 1) · \(r.state.label)" }
            return Content(icon: "exclamationmark.triangle", tint: P.amber,
                           title: "\(plural(running, "instance")) still running",
                           target: owner.title,
                           sub: "\(results.filter { $0.state.done }.count) of \(results.count) instances quit after 10 s",
                           list: list,
                           message: "It has not answered the quit request. Force quit ends it immediately; unsaved work in it is lost."
                               + (owner.hostsShells ? " " + Owner.shellWarning : ""),
                           safeButton: "Leave running", safeSpoken: "Leave \(owner.title) running",
                           actButton: "Force quit", actSpoken: "Force quit the \(plural(running, "remaining instance"))",
                           actVariant: .secondaryDanger)
        case .ask, .working:
            return askContent
        }
    }

    private var isEndSession: Bool { if case .endSession = request.kind { return true }; return false }

    private var blastList: [String] {
        var list: [String] = []
        if let shared = owner.sharedWith, !shared.isEmpty {
            let head = shared.prefix(3).joined(separator: ", ")
            let more = shared.count > 3 ? " … (+\(shared.count - 3))" : ""
            list.append(owner.kind == "codex-app"
                        ? "\(plural(shared.count, "thread")) end: \(head)\(more)"
                        : "\(plural(shared.count, "container")) stop: \(head)\(more)")
        }
        if owner.kind == "service" {
            switch owner.usedBy {
            case let used? where !used.isEmpty:
                list.append("Used by sessions: \(used.joined(separator: ", ")) (inferred)")
            case _?:
                list.append("Used by sessions: none found (inferred)")
            case nil:
                list.append("Used by sessions: unknown")
            }
        }
        if owner.hostsShells { list.append(Owner.shellWarning) }
        if let inst = owner.instances, inst.count > 1 {
            list.append("\(inst.count) instances, each checked before it is asked to quit")
        }
        return list
    }

    private var askContent: Content {
        let size = owner.footprint.map { gb($0) + " now" }
        switch request.kind {
        case .job(let j):
            let name = j.displayName.components(separatedBy: " · ")[0]
            let sub = [j.memberCount.map { plural($0, "process", "processes") }, j.footprint.map { gb($0) + " now" }]
                .compactMap { $0 }.joined(separator: " · ")
            return Content(icon: "stop.circle", tint: P.red, title: "Stop \(name.lowercased())?",
                           target: "\(name) in \(owner.title)", sub: sub.isEmpty ? nil : sub,
                           message: "Processes get a polite stop signal first; nothing is force-killed unless you choose it.",
                           safe: keepsConversation ? "The conversation keeps running." : "\(owner.title) keeps running.",
                           safeButton: "Cancel", safeSpoken: "Cancel, keep the \(j.kind == "server" ? "server" : "job") running",
                           actButton: j.stopLabel,
                           actSpoken: "\(j.stopLabel): send stop signal to \(j.memberCount.map { plural($0, "process", "processes") } ?? "its processes")")
        case .endSession:
            let sub = [owner.memberCount.map { plural($0, "process", "processes") }, size]
                .compactMap { $0 }.joined(separator: " · ")
            let work = owner.jobs.filter { !$0.isConversation }.map { $0.label }
            return Content(icon: "xmark.octagon", tint: P.red, title: "End \(owner.title)?",
                           target: owner.title, sub: sub.isEmpty ? nil : sub,
                           list: work.isEmpty ? [] : ["Includes: " + work.joined(separator: ", ")],
                           message: "The conversation and every process it started get a polite stop signal. Anything not yet written to disk is lost.",
                           safe: "Other sessions, including any nested inside it, keep running.",
                           safeButton: "Cancel", safeSpoken: "Cancel, keep the session running",
                           actButton: "End session", actSpoken: "End session \(owner.title)")
        case .quitApp:
            return Content(icon: "exclamationmark.triangle", tint: P.amber, title: "Quit \(owner.title)?",
                           target: [owner.title, size].compactMap { $0 }.joined(separator: " · "),
                           list: blastList, warn: true,
                           message: owner.kind == "service"
                               ? "Sessions that call these services will get connection errors until the VM is started again."
                               : "The app is asked to quit normally so it can save first. Nothing is force-quit unless you choose it.",
                           safeButton: "Cancel", safeSpoken: "Cancel, keep \(owner.title) running",
                           actButton: "Quit app", actSpoken: "Quit \(owner.title)")
        case .stopCommand:
            return Content(icon: "exclamationmark.triangle", tint: P.amber, title: "Stop \(owner.title)?",
                           target: [owner.title, size].compactMap { $0 }.joined(separator: " · "),
                           list: blastList, warn: true,
                           message: "No app to quit — this VM runs without a window. Run this in a terminal:",
                           command: owner.stopCommand,
                           safeButton: "Close", safeSpoken: "Close")
        }
    }

    var body: some View {
        let c = content
        VStack(alignment: .leading, spacing: 0) {
            Image(systemName: c.icon).font(.system(size: 18, weight: .medium))
                .foregroundColor(c.tint).accessibilityHidden(true)
            Text(c.title).font(ft(18, .medium)).foregroundColor(P.text)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 10).padding(.bottom, 10)
                .accessibilityAddTraits(.isHeader)
            VStack(alignment: .leading, spacing: 2) {
                Text(c.target).font(ft(13, .medium)).foregroundColor(P.text)
                    .fixedSize(horizontal: false, vertical: true)
                if let sub = c.sub {
                    Text(sub).font(ft(12)).foregroundColor(P.muted).monospacedDigit()
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !c.list.isEmpty {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(c.list, id: \.self) { item in
                            HStack(alignment: .firstTextBaseline, spacing: 5) {
                                Text("•").accessibilityHidden(true)
                                Text(item).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    .font(ft(12)).foregroundColor(P.muted).padding(.top, 4)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 12).padding(.vertical, 11)
            .background(RoundedRectangle(cornerRadius: 9).fill(P.soft))
            .overlay(RoundedRectangle(cornerRadius: 9)
                .stroke(c.warn ? P.amber.opacity(0.62) : Color.clear, lineWidth: 1))
            .accessibilityElement(children: .combine)
            .padding(.bottom, 12)

            Text(c.message).font(ft(13)).foregroundColor(P.muted)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 12)
            if let cmd = c.command {
                CopyCommandField(command: cmd).padding(.bottom, 16)
            }
            if let safe = c.safe {
                HStack(spacing: 6) {
                    Image(systemName: "checkmark.shield").font(.system(size: 12, weight: .semibold))
                    Text(safe).font(ft(12)).fixedSize(horizontal: false, vertical: true)
                }
                .foregroundColor(P.green)
                .accessibilityElement(children: .combine)
                .padding(.bottom, 16)
            }
            if working {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Waiting up to 10 s for the processes to exit…")
                        .font(ft(12)).foregroundColor(P.muted)
                }
                .frame(maxWidth: .infinity, alignment: .trailing)
            } else {
                HStack(spacing: 8) {
                    Spacer(minLength: 0)
                    // Explicitly focusable: with Keyboard navigation off (the
                    // macOS default) a button never takes focus otherwise, so
                    // focus could not start on the safe choice.
                    ActionButton(title: c.safeButton, action: onCancel)
                        .accessibilityLabel(c.safeSpoken)
                        .keyboardShortcut(.cancelAction)
                        .focusable()
                        .focused($focus, equals: .safe)
                    if let act = c.actButton {
                        ActionButton(title: act, variant: c.actVariant) {
                            if case .ask = request.phase { onConfirm() } else { onForce() }
                        }
                        .accessibilityLabel(c.actSpoken)
                        .focusable()
                        .focused($focus, equals: .act)
                    }
                }
                .focusSection()
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 17).fill(P.panel))
        .overlay(RoundedRectangle(cornerRadius: 17).stroke(P.border, lineWidth: 1))
        .onAppear { focus = .safe }
        .task(id: focus) { onFocus(focus.map { $0 == .safe ? "safe" : "act" }) }
        .onExitCommand { if !working { onCancel() } }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(c.title)
        .accessibilityAddTraits(.isModal)
    }
}

struct DecisionRow: View {
    var level: String, result: String
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(level)
                .font(.system(size: 10, weight: .semibold))
                .foregroundColor(P.tint(level)).frame(width: 66, alignment: .leading)
            Text(result).font(ft(11)).foregroundColor(P.muted)
            Spacer(minLength: 0)
        }
    }
}

struct GateEventCard: View {
    var event: GateEvent
    var animation: Animation?
    @State private var expanded = false

    private var stopped: Bool { event.action == "block" }
    private var tint: Color { stopped ? P.red : P.amber }
    private var sessionLabel: String {
        event.sessionName ?? (event.sessionID.isEmpty ? "Unknown session" : event.sessionID)
    }
    private var matchLabel: String {
        guard let c = event.classification else { return "Rule match not recorded" }
        if c.source == "learned" {
            let observed = c.samples.map { " · \($0) observations" } ?? ""
            return "Learned rule: \(c.rule)\(observed) · warning only"
        }
        return "Built-in rule: \(c.rule)"
    }
    private var fullMatchLabel: String {
        guard event.classification != nil else {
            return "Not recorded — this event predates rule tracking"
        }
        return matchLabel
    }
    private var outcome: String {
        if stopped {
            return "Stopped before running · "
                + (event.retryStatus == "waiting" ? "waiting to retry" : "not waiting to retry")
        }
        return "Warning added to the session’s context; command ran"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: expanded ? 8 : 5) {
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                Text(stopped ? "Stopped · command did not run" : "Warned · command ran")
                    .font(ft(11, .medium)).foregroundColor(tint)
                Spacer(minLength: 4)
                Text(eventTime(event.ts)).font(ft(11)).foregroundColor(P.muted)
            }

            Text(event.commandDisplay)
                .font(.system(size: 11, design: .monospaced))
                .foregroundColor(P.text)
                .lineLimit(expanded ? nil : 2)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)

            if expanded {
                eventDetail("Session", sessionLabel)
                eventDetail("Command match", fullMatchLabel)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Memory at \(eventClock(event.ts))")
                        .font(ft(10, .medium)).foregroundColor(P.muted)
                    Text(event.level).font(ft(11, .semibold)).foregroundColor(P.tint(event.level))
                    ForEach(Array(event.reasons.enumerated()), id: \.offset) { _, reason in
                        Text(reason).font(ft(11)).foregroundColor(P.muted)
                    }
                }
                eventDetail("Outcome", outcome)
            } else {
                Text("Session \(sessionLabel) · \(relative(event.ts))")
                    .font(ft(11)).foregroundColor(P.muted).lineLimit(1)
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Text("\(matchLabel) + \(event.level) memory → \(stopped ? "stopped" : "warned")")
                        .font(ft(11)).foregroundColor(P.muted)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 2)
                    Chevron(open: false)
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(P.panel))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(tint.opacity(0.62), lineWidth: 1))
        .contentShape(Rectangle())
        .onTapGesture { withAnimation(animation) { expanded.toggle() } }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
    }

    private func eventDetail(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(ft(10, .medium)).foregroundColor(P.muted)
            Text(value).font(ft(11)).foregroundColor(P.text)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

struct MissingGateEventCard: View {
    var item: PendingRetry
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 5) {
                Text("Stopped · command did not run").font(ft(11, .medium)).foregroundColor(P.red)
                Spacer()
                Text(eventTime(item.ts)).font(ft(11)).foregroundColor(P.muted)
            }
            Text(item.commandDisplay)
                .font(.system(size: 11, design: .monospaced)).foregroundColor(P.text)
                .lineLimit(2).textSelection(.enabled)
            Text("Session \(item.sessionName ?? item.sessionID) · \(relative(item.ts))")
                .font(ft(11)).foregroundColor(P.muted)
            Text("Stopped earlier · event details are no longer retained")
                .font(ft(11)).foregroundColor(P.muted)
        }
        .padding(10).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(P.panel))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(P.red.opacity(0.62), lineWidth: 1))
        .accessibilityElement(children: .combine)
    }
}

struct FooterButton: View {
    var icon: String, label: String
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: icon).font(.system(size: 10, weight: .semibold))
                Text(label).font(ft(11, .medium))
            }
            .foregroundColor(hover ? P.text : P.muted)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(Capsule().fill(hover ? P.selected : P.soft))
        }
        .buttonStyle(.plain)
        .fixedSize()
        .onHover { hover = $0 }
        .accessibilityLabel(label)
    }
}

struct BodyHeightKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

struct ChromeHeightKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value += nextValue() }
}

// MARK: - main view

struct ContentView: View {
    @ObservedObject var model: Model
    var onQuit: () -> Void
    /// ImageRenderer cannot lay out a ScrollView offscreen — it renders empty.
    /// Renders drop the scroll container and take their natural height.
    var flattened = false
    var previewOpenGate = false

    static let width: CGFloat = 380
    static let maxHeight: CGFloat = 620

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var openGate = false
    @State private var openMatchRules = false
    @State private var showAllWarnings = false
    @State private var showAllStops = false
    @State private var bodyHeight: CGFloat = 0
    @State private var chromeHeight: CGFloat = 0

    private func motion(_ seconds: Double) -> Animation? {
        reduceMotion ? nil : .easeInOut(duration: seconds)
    }

    var body: some View {
        ZStack(alignment: .top) {
            P.bg
            column
                .blur(radius: model.confirm == nil ? 0 : 4)
                .allowsHitTesting(model.confirm == nil)
                .disabled(model.confirm != nil)
                .accessibilityHidden(model.confirm != nil)
            if let request = model.confirm {
                P.scrim.transition(.opacity)
                ConfirmOverlay(request: request,
                               onCancel: { withAnimation(motion(0.12)) { model.cancel() } },
                               onConfirm: { model.perform() },
                               onForce: { model.force() },
                               onFocus: { model.overlayFocus = $0 })
                    .padding(.horizontal, 14).padding(.top, 96)
                    .transition(.opacity)
            }
        }
        .animation(motion(0.12), value: model.confirm == nil)
        .frame(width: Self.width, height: flattened ? nil : min(bodyHeight + chromeHeight, Self.maxHeight),
               alignment: .top)
        .foregroundColor(P.text)
    }

    private var column: some View {
        VStack(spacing: 0) {
            header.background(GeometryReader { Color.clear.preference(key: ChromeHeightKey.self, value: $0.size.height) })
            Rectangle().fill(P.border).frame(height: 1)
            if let snap = model.snap {
                if flattened {
                    content(snap)
                } else {
                    ScrollView {
                        content(snap).background(GeometryReader {
                            Color.clear.preference(key: BodyHeightKey.self, value: $0.size.height)
                        })
                    }
                    .frame(height: min(bodyHeight, Self.maxHeight - chromeHeight))
                }
            } else {
                loading
                    .background(GeometryReader { Color.clear.preference(key: BodyHeightKey.self, value: $0.size.height) })
            }
            footer.background(GeometryReader { Color.clear.preference(key: ChromeHeightKey.self, value: $0.size.height + 1) })
        }
        .onPreferenceChange(BodyHeightKey.self) { bodyHeight = $0 }
        .onPreferenceChange(ChromeHeightKey.self) { chromeHeight = $0 }
    }

    private var loading: some View {
        VStack(spacing: 8) {
            if let e = model.loadError {
                Image(systemName: "exclamationmark.triangle").foregroundColor(P.amber)
                Text("Could not read memory: \(e).").font(ft(12)).foregroundColor(P.muted)
                    .multilineTextAlignment(.center)
                ActionButton(title: "Try again", icon: "arrow.clockwise") { model.refresh() }
            } else {
                ProgressView().controlSize(.small)
                Text("Reading memory…").font(ft(12)).foregroundColor(P.muted)
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, minHeight: 180)
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "memorychip")
                .font(.system(size: 15, weight: .medium)).foregroundColor(P.accent)
                .frame(width: 34, height: 34)
                .background(RoundedRectangle(cornerRadius: 11).fill(P.selected))
                .accessibilityHidden(true)
            Text("memmon").font(ft(17, .medium)).tracking(-0.3).foregroundColor(P.text)
            Spacer()
            if model.refreshing || model.stillSampling {
                ProgressView().controlSize(.small)
                Text(model.refreshing ? "Syncing…" : "Still sampling…").font(ft(11)).foregroundColor(P.muted)
            }
        }
        .padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 12)
    }

    private func content(_ s: OwnersSnap) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            healthCard(s).padding(.horizontal, 12).padding(.top, 12).padding(.bottom, 8)
            protectionLine(s).padding(.horizontal, 16).padding(.bottom, 12)
            if s.degraded {
                degradedBanner(s.inventoryReason).padding(.horizontal, 12).padding(.bottom, 10)
            }
            if let e = model.loadError {
                OutcomeBanner(banner: Banner(tone: .warning, title: "Could not refresh",
                                             body: "— \(e). Showing the previous sample."),
                              onRefresh: { model.refresh() }, onDismiss: { model.loadError = nil })
                    .padding(.horizontal, 12).padding(.bottom, 10)
            }
            if let b = model.banner {
                OutcomeBanner(banner: b, onRefresh: { model.banner = nil; model.refresh() },
                              onDismiss: { withAnimation(motion(0.12)) { model.banner = nil } })
                    .padding(.horizontal, 12).padding(.bottom, 10)
                    .transition(.opacity)
            }
            toolbar.padding(.horizontal, 16).padding(.bottom, 8)
            HStack {
                Text("Task / app")
                Spacer()
                Text(model.sort.columnHeader)
            }
            .font(ft(11)).foregroundColor(P.muted)
            .padding(.horizontal, 18).padding(.bottom, 3)
            .accessibilityHidden(true)
            ownerList(s).padding(.horizontal, 8)
            Text("System and other users: not itemised"
                 + (s.hiddenProcesses.flatMap { $0 > 0 ? " (\(plural($0, "process", "processes")))" : nil } ?? ""))
                .font(ft(11)).foregroundColor(P.muted)
                .padding(.horizontal, 18).padding(.top, 2).padding(.bottom, 8)
            if !s.runnerJobs.isEmpty {
                managedJobs(s.runnerJobs).padding(.horizontal, 12).padding(.top, 4).padding(.bottom, 6)
            }
            gateSection(s).padding(.horizontal, 12).padding(.top, 4).padding(.bottom, 10)
        }
        .animation(motion(0.12), value: model.banner == nil)
    }

    // MARK: health

    private func healthCard(_ s: OwnersSnap) -> some View {
        let sys = s.system
        let level = sys.scoreLevel
        let (headline, icon): (String, String) = {
            switch level {
            case "HEALTHY": return ("Memory pressure normal", "checkmark.circle")
            case "WATCH": return ("Memory pressure elevated", "exclamationmark.circle")
            case "DANGER": return ("Memory pressure high", "exclamationmark.triangle")
            case "CRITICAL": return ("Memory pressure critical", "exclamationmark.octagon")
            default: return ("Memory pressure unknown", "questionmark.circle")
            }
        }()
        let tint = P.tint(level)
        let ramGB = sys.ramBytes.map { $0 / GB }
        let usedGB = sys.usedBytes.map { $0 / GB }
        // A partial sum would understate the machine, so a CPU total is shown
        // only when every owner was measured.
        let measuredCPU = s.owners.compactMap { $0.cpu }
        let coverage = sys.cpuCoverage
            ?? (s.owners.isEmpty ? 0 : Double(measuredCPU.count) / Double(s.owners.count))
        let cpuNow = coverage >= 1 ? (sys.cpuCores ?? measuredCPU.reduce(0, +)) : nil
        let cpuMissing = s.degraded ? "CPU not measured" : (coverage > 0 ? "CPU partly measured" : "CPU warming up")
        let ncpu = sys.ncpu ?? Double(ProcessInfo.processInfo.activeProcessorCount)
        let over = (usedGB ?? 0) > (ramGB ?? .infinity)
        return VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 7) {
                Image(systemName: icon).font(.system(size: 14, weight: .medium)).foregroundColor(tint)
                    .accessibilityHidden(true)
                Text(headline).font(ft(14, .medium)).foregroundColor(tint)
                Spacer(minLength: 6)
                freshness(s)
            }
            HStack(alignment: .lastTextBaseline) {
                if let usedGB, let ramGB {
                    Text("\(Text(String(format: "%.1f", usedGB)).font(ft(23)).foregroundColor(P.text))\(Text(String(format: " / %.0f GB in use", ramGB)).font(ft(12)).foregroundColor(P.muted))")
                } else {
                    Text("\(Text("—").font(ft(23)).foregroundColor(P.text))\(Text(" memory in use not available").font(ft(12)).foregroundColor(P.muted))")
                }
                Spacer(minLength: 6)
                Text(cpuNow.map { String(format: "CPU %.1f / %.0f cores", $0, ncpu) } ?? cpuMissing)
                    .font(ft(12)).foregroundColor(P.muted)
            }
            .monospacedDigit()
            .padding(.top, 11).padding(.bottom, 8)
            Meter(value: usedGB.flatMap { u in ramGB.map { u / max($0, 0.001) } },
                  tint: level == "DANGER" || level == "CRITICAL" || over ? P.red : P.accent)
                .accessibilityRepresentation {
                    // A progress indicator is how VoiceOver reads a meter's value.
                    ProgressView(value: min(max((usedGB ?? 0) / max(ramGB ?? 1, 0.001), 0), 1))
                        .accessibilityLabel("Memory in use")
                        .accessibilityValue(usedGB.flatMap { u in ramGB.map { r in
                            String(format: "%.1f of %.0f GB", u, r) + (u > r ? ", over the limit" : "") } }
                            ?? "not available")
                }
            Text("Score \(level ?? "unavailable") · kernel pressure \(sys.pressureLevel ?? "unavailable")"
                 + (sys.reason.map { " · \($0)" } ?? ""))
                .font(ft(11)).foregroundColor(P.muted).padding(.top, 7)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .panel(13)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("System memory")
    }

    private func freshness(_ s: OwnersSnap) -> some View {
        let text: String
        let spoken: String
        if let age = s.age {
            text = "Sampled \(ageText(age)) ago" + (s.stale ? " · stale" : "")
            spoken = "Sampled \(ageText(age)) ago by the \(s.source == "sampler" ? "background sampler" : "live reader")"
                + (s.stale ? ", stale" : "")
        } else {
            text = "Sample time unknown"
            spoken = "Sample time unknown"
        }
        return HStack(spacing: 4) {
            if s.stale { Image(systemName: "clock").font(.system(size: 11)) }
            Text(text).font(ft(11)).lineLimit(1)
        }
        .foregroundColor(s.stale ? P.amber : P.muted)
        .fixedSize()
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
        .id(model.tick)
    }

    private func protectionLine(_ s: OwnersSnap) -> some View {
        let p = s.protection
        let (text, tint): (String, Color) = {
            switch p?.summary {
            case "on": return ("Protection on · no heavy processes outside memmon run", P.green)
            case "partial":
                let n = p?.unmanagedHeavy ?? 0
                return ("Protection partial · \(plural(n, "heavy process", "heavy processes")) not started through memmon run", P.amber)
            case "paused": return ("Protection paused · commands run without a memory check", P.amber)
            case "off": return ("Protection off · the command gate is not installed or is disabled", P.muted)
            default: return ("Protection status unknown", P.muted)
            }
        }()
        return HStack(alignment: .top, spacing: 7) {
            Image(systemName: "shield").font(.system(size: 13, weight: .medium)).padding(.top, 1)
                .accessibilityHidden(true)
            Text(text).font(ft(12)).fixedSize(horizontal: false, vertical: true)
        }
        .foregroundColor(tint)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(text)
    }

    private func degradedBanner(_ reason: String?) -> some View {
        let detail = "libproc is unavailable" + (reason.map { " (\($0))" } ?? "")
            + ", so memory comes from top and stop actions are off."
        return HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle").foregroundColor(P.amber).padding(.top, 1)
                .accessibilityHidden(true)
            Text("\(Text("Limited process details").fontWeight(.medium)) — \(detail)")
                .font(ft(12)).foregroundColor(P.text)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(P.amber.opacity(0.16)))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Limited process details: " + detail)
    }

    // MARK: owners

    private var toolbar: some View {
        HStack(spacing: 8) {
            Text("Sessions & apps").font(ft(14, .medium))
            Spacer()
            HStack(spacing: 2) {
                ForEach(SortKey.allCases, id: \.self) { key in
                    let on = model.sort == key
                    Button { model.sort = key } label: {
                        Text(key.label).font(ft(12))
                            .foregroundColor(on ? P.text : P.muted)
                            .padding(.horizontal, 9).padding(.vertical, 4)
                            .background(RoundedRectangle(cornerRadius: 6).fill(on ? P.panel : Color.clear))
                            .overlay(RoundedRectangle(cornerRadius: 6).stroke(on ? P.border : Color.clear, lineWidth: 1))
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Sort by \(key.label)")
                    .accessibilityAddTraits(on ? .isSelected : [])
                }
            }
            .padding(3)
            .background(RoundedRectangle(cornerRadius: 8).fill(P.soft))
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Sort owners")
        }
    }

    private func ownerList(_ s: OwnersSnap) -> some View {
        let rows = sortOwners(s.rows, by: model.sort)
        return VStack(alignment: .leading, spacing: 2) {
            if rows.isEmpty {
                Text("No owners found in this sample.").font(ft(12)).foregroundColor(P.muted)
                    .padding(10)
            }
            ForEach(rows) { o in
                let open = model.expanded == o.id
                OwnerRow(owner: o, sort: model.sort, expanded: open) {
                    withAnimation(motion(0.16)) { model.expanded = open ? nil : o.id }
                }
                if open {
                    OwnerDetailCard(
                        owner: o, degraded: s.degraded,
                        techOpen: Binding(get: { model.techOpen.contains(o.id) },
                                          set: { on in
                                              if on { model.techOpen.insert(o.id) } else { model.techOpen.remove(o.id) }
                                          }),
                        animation: motion(0.16),
                        onAsk: { kind in withAnimation(motion(0.12)) { model.ask(kind, o) } })
                        .padding(.bottom, 8)
                        .transition(.opacity)
                }
            }
        }
    }

    // MARK: managed jobs (memmon run)

    private func managedJobs(_ jobs: [ManagedJob]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "list.bullet.rectangle").font(.system(size: 13, weight: .medium))
                    .foregroundColor(P.muted).accessibilityHidden(true)
                Text("Managed jobs").font(ft(13, .medium))
                Spacer()
                Text("memmon run").font(.system(size: 11, design: .monospaced)).foregroundColor(P.muted)
            }
            .padding(.bottom, 4)
            ForEach(jobs) { job in
                let line = "\(job.label) · \(job.state) · \(ageText(Double(job.elapsed)))"
                VStack(alignment: .leading, spacing: 2) {
                    Text(line).font(ft(12, .medium)).foregroundColor(P.text)
                    Text(job.reason.isEmpty ? job.resource : "\(job.resource) — \(job.reason)")
                        .font(ft(11)).foregroundColor(P.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.vertical, 7)
                .frame(maxWidth: .infinity, alignment: .leading)
                .overlay(Rectangle().fill(P.border).frame(height: 1), alignment: .top)
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Managed job \(line), \(job.resource)" + (job.reason.isEmpty ? "" : ", \(job.reason)"))
            }
        }
        .padding(.horizontal, 12).padding(.top, 10).padding(.bottom, 4)
        .panel(12)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Managed jobs")
    }

    // MARK: gate (retained)

    private var recentWarnings: [GateEvent] {
        (model.snap?.gate.events ?? []).filter { $0.action == "warn" }.sorted { $0.ts > $1.ts }
    }
    // Stops split by whether they still represent unfinished work. A command
    // that never ran and is still waiting is the one thing here nobody should
    // have to expand a disclosure to find, so those are always listed in full.
    // Everything else is history and is capped like the warnings are — an
    // uncapped list grew with the log and pushed the rest of the popover down
    // for no benefit, since an already-retried stop is not actionable.
    private var pendingStops: [GateEvent] {
        (model.snap?.gate.events ?? []).filter { $0.action == "block" && $0.retryStatus == "waiting" }
            .sorted { $0.ts > $1.ts }
    }
    private var resolvedStops: [GateEvent] {
        (model.snap?.gate.events ?? []).filter { $0.action == "block" && $0.retryStatus != "waiting" }
            .sorted { $0.ts > $1.ts }
    }

    private func policyCopy(_ g: GateStats) -> String {
        if g.paused {
            return "Command protection is paused. Every command runs without a memory check. History below is unchanged."
        }
        switch g.mode {
        case "block":
            return "Current policy: WATCH warns; DANGER or CRITICAL stops before running."
        case "warn":
            return "Current policy: WATCH, DANGER, or CRITICAL warns; commands are never stopped."
        default:
            return "Current policy: WATCH or DANGER warns; CRITICAL stops before running."
        }
    }

    private func policyResult(_ g: GateStats, _ level: String) -> String {
        if g.paused { return "runs without a memory check" }
        if level == "HEALTHY" { return "runs silently" }
        if g.mode == "warn" { return "warned; command ran" }
        if g.mode == "block" && (level == "DANGER" || level == "CRITICAL") {
            return "stopped before running"
        }
        if g.mode == "block-critical" && level == "CRITICAL" {
            return "stopped before running"
        }
        return "warned; command ran"
    }

    private func retryCopy(_ s: OwnersSnap) -> (String, Color) {
        let g = s.gate
        if g.paused {
            return ("Protection is paused; retrying now will run without a memory check.", P.amber)
        }
        guard let level = s.system.scoreLevel else {
            return ("Memory level is unknown right now; a retry may be stopped again.", P.amber)
        }
        if level == "HEALTHY" {
            return ("Memory is HEALTHY now — waiting commands can be retried.", P.green)
        }
        let stops = (level == "CRITICAL" && g.mode != "warn")
            || (level == "DANGER" && g.mode == "block")
        return (stops
            ? "If memory stays \(level), a retry will be stopped again."
            : "If memory stays \(level), a retry will be warned and will run.", P.amber)
    }

    private func gateSection(_ s: OwnersSnap) -> some View {
        let g = s.gate
        let isOpen = previewOpenGate || openGate
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "shield").font(.system(size: 13, weight: .medium)).foregroundColor(P.muted)
                    .accessibilityHidden(true)
                Text("Command protection").font(ft(13, .medium))
                if s.gateMissing {
                    Chip(text: "Status unavailable")
                } else if !g.installed {
                    Chip(text: "Not installed")
                } else if g.paused {
                    Chip(text: "Paused", tint: P.amber)
                } else {
                    Chip(text: "Active", tint: P.green)
                }
                Spacer(minLength: 4)
                if g.installed {
                    ActionButton(title: g.paused ? "Resume" : "Pause",
                                 icon: g.paused ? "play.fill" : "pause.fill") {
                        model.toggleGate(!g.paused)
                    }
                    .accessibilityLabel(g.paused ? "Resume command protection" : "Pause command protection")
                }
            }
            if g.installed && g.paused {
                Text(g.pausedUntil.map { "Paused until \(eventTime($0)) · every command runs without a memory check" }
                     ?? "Paused · every command runs without a memory check")
                    .font(ft(12)).foregroundColor(P.amber).fixedSize(horizontal: false, vertical: true)
            }
            if !g.pending.isEmpty {
                let (copy, tint) = retryCopy(s)
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(plural(g.pending.count, "blocked command")) waiting to retry")
                        .font(ft(12, .medium))
                    ForEach(g.pending.sorted { $0.ts > $1.ts }) { p in
                        Text("\(p.commandDisplay) · \(p.sessionName ?? (p.sessionID.isEmpty ? "unknown session" : p.sessionID)) · blocked at \(p.pressureLevel) \(relative(p.ts))")
                            .font(.system(size: 11, design: .monospaced)).foregroundColor(P.muted)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                    Text(copy).font(ft(11)).foregroundColor(tint).fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, 10).padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(P.amber.opacity(0.62), lineWidth: 1))
                .accessibilityElement(children: .combine)
            }
            Button { withAnimation(motion(0.16)) { openGate.toggle() } } label: {
                HStack(spacing: 5) {
                    Chevron(open: isOpen)
                    Text(g.installed
                         ? "Policy and history · \(g.warned) warned · \(g.stopped) stopped"
                         : "What command protection does")
                        .font(ft(11)).foregroundColor(P.muted)
                    Spacer()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(isOpen ? "expanded" : "collapsed")
            if isOpen { gateDetail(s) }
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .panel(12)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Command protection")
    }

    @ViewBuilder private func gateDetail(_ s: OwnersSnap) -> some View {
        let g = s.gate
        if s.gateMissing {
            Text("memmon did not report the command gate's state this time.")
                .font(ft(11)).foregroundColor(P.muted)
                .fixedSize(horizontal: false, vertical: true)
        } else if !g.installed {
            Text("Command protection is not installed. Memory monitoring is active; commands are never warned or stopped.")
                .font(ft(11)).foregroundColor(P.muted)
                .fixedSize(horizontal: false, vertical: true)
        } else {
            VStack(alignment: .leading, spacing: 9) {
                if g.paused {
                    Text(policyCopy(g)).font(ft(11)).foregroundColor(P.amber)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("Only commands that match a memory-intensive rule are checked.")
                        .font(ft(11)).foregroundColor(P.muted)
                    Text(policyCopy(g)).font(ft(11, .medium))
                        .foregroundColor(P.text).fixedSize(horizontal: false, vertical: true)
                }
                Text("Matched command + memory then → result")
                    .font(ft(10, .medium)).foregroundColor(P.muted)
                VStack(spacing: 3) {
                    DecisionRow(level: "HEALTHY", result: policyResult(g, "HEALTHY"))
                    DecisionRow(level: "WATCH", result: policyResult(g, "WATCH"))
                    DecisionRow(level: "DANGER", result: policyResult(g, "DANGER"))
                    DecisionRow(level: "CRITICAL", result: policyResult(g, "CRITICAL"))
                }
                Button { withAnimation(motion(0.16)) { openMatchRules.toggle() } } label: {
                    HStack {
                        Text("What commands match?").font(ft(10, .medium)).foregroundColor(P.muted)
                        Spacer()
                        Chevron(open: openMatchRules)
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain)
                if openMatchRules {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Package tasks: typecheck, build, test, install, dev, lint")
                        Text("Tools: tsc, Vitest, Jest, Playwright, pytest, Cargo, Gradle, Bazel, Xcodebuild, webpack, make, Next, Expo, Docker, Colima")
                        Text("Verified commands learned from this Mac are labelled “Learned”.")
                        Text("Other commands run without a memory check.")
                    }
                    .font(ft(11)).foregroundColor(P.muted)
                    .fixedSize(horizontal: false, vertical: true)
                }
                if g.errors > 0 {
                    Text("Command protection failed open \(g.errors) times. Those commands ran.")
                        .font(ft(11)).foregroundColor(P.amber)
                }
                if g.warned == 0 && g.stopped == 0 && g.pending.isEmpty {
                    Text("No warnings or stops since \(retainedDate(g.since, includeTime: true)).")
                        .font(ft(11, .medium)).foregroundColor(P.green)
                } else {
                    stoppedHistory(g)
                    warningHistory(g)
                }
                if g.evaluated > 0 {
                    Text("Retained activity since \(retainedDate(g.since, includeTime: true)).")
                        .font(ft(10)).foregroundColor(P.muted)
                    if !g.complete {
                        Text("Older activity may be missing.").font(ft(10)).foregroundColor(P.muted)
                    }
                    if let to = g.historyTo {
                        Text("Event details retained from \(retainedDate(g.historyFrom, includeTime: true)) to \(retainedDate(to, includeTime: true)).")
                            .font(ft(10)).foregroundColor(P.muted)
                    }
                }
            }
        }
    }

    private func stoppedHistory(_ g: GateStats) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("Stopped before running").font(ft(11, .medium)).foregroundColor(P.red)
                Spacer()
                Text("\(g.stopped)").font(ft(11, .semibold)).foregroundColor(P.red)
            }
            // Unfinished work first, never truncated.
            ForEach(pendingStops) { GateEventCard(event: $0, animation: motion(0.16)) }
            ForEach(g.pending.filter { !$0.eventRetained }) {
                MissingGateEventCard(item: $0)
            }
            if showAllStops {
                LazyVStack(spacing: 7) {
                    ForEach(resolvedStops) { GateEventCard(event: $0, animation: motion(0.16)) }
                }
            } else {
                ForEach(Array(resolvedStops.prefix(3))) { GateEventCard(event: $0, animation: motion(0.16)) }
            }
            if resolvedStops.count > 3 {
                Button(showAllStops
                       ? "Show only 3 recent stops"
                       : "Show all \(resolvedStops.count) earlier stops") {
                    withAnimation(motion(0.16)) { showAllStops.toggle() }
                }
                .buttonStyle(.plain)
                .font(ft(11, .medium))
                .foregroundColor(P.red)
            }
            if pendingStops.isEmpty && resolvedStops.isEmpty && g.pending.isEmpty {
                Text("No commands have been stopped since \(retainedDate(g.since)).")
                    .font(ft(11)).foregroundColor(P.muted)
            }
        }
    }

    private func warningHistory(_ g: GateStats) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("Warned — command ran").font(ft(11, .medium)).foregroundColor(P.amber)
                Spacer()
                Text("\(g.warned)").font(ft(11, .semibold)).foregroundColor(P.amber)
            }
            if showAllWarnings {
                LazyVStack(spacing: 7) {
                    ForEach(recentWarnings) { GateEventCard(event: $0, animation: motion(0.16)) }
                }
            } else {
                ForEach(Array(recentWarnings.prefix(3))) { GateEventCard(event: $0, animation: motion(0.16)) }
            }
            if recentWarnings.count > 3 {
                Button(showAllWarnings
                       ? "Show only 3 recent warnings"
                       : "Show all \(g.warned) retained warnings") {
                    withAnimation(motion(0.16)) { showAllWarnings.toggle() }
                }
                .buttonStyle(.plain)
                .font(ft(11, .medium))
                .foregroundColor(P.amber)
            }
        }
    }

    private var footer: some View {
        let count = model.snap?.rows.count
        return HStack(spacing: 8) {
            FooterButton(icon: "arrow.clockwise", label: "Sync") { model.refresh() }
            Spacer(minLength: 4)
            VStack(spacing: 1) {
                if let count {
                    Text("\(plural(count, "owner")) · child processes included once")
                }
                Text("Memory figures are estimates")
            }
            .font(ft(11)).foregroundColor(P.muted)
            .multilineTextAlignment(.center)
            .lineLimit(1).minimumScaleFactor(0.85)
            Spacer(minLength: 4)
            FooterButton(icon: "power", label: "Quit", action: onQuit)
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .overlay(Rectangle().fill(P.border).frame(height: 1), alignment: .top)
    }
}

// MARK: - app

/// The status-item title from the sampler's latest.json: a level dot and swap
/// in use. An unknown level or a sample older than 180 s gets a neutral dot,
/// never green.
func statusTitle(_ j: [String: Any], now: Double) -> String? {
    guard let used = num(j["swap_used"]) else { return nil }
    let fresh = num(j["ts"]).map { now - $0 <= 180 } ?? false
    let dot: String
    switch fresh ? (j["pressure"] as? String) : nil {
    case "CRITICAL"?, "DANGER"?: dot = "🔴"
    case "WATCH"?: dot = "🟠"
    case "HEALTHY"?: dot = "🟢"
    default: dot = "⚪"
    }
    let text = used >= GB ? String(format: "%.1fG", used / GB) : String(format: "%.0fM", used / MB)
    return "\(dot) \(text)"
}

/// The popover's content, shared by the app and the hosted-view self-test.
/// The popover follows the view's own height, capped at 620 pt.
@discardableResult
func configurePopover(_ popover: NSPopover, model: Model, onQuit: @escaping () -> Void)
    -> NSHostingController<ContentView> {
    let host = NSHostingController(rootView: ContentView(model: model, onQuit: onQuit))
    host.sizingOptions = [.preferredContentSize]
    popover.contentViewController = host
    return host
}

final class Controller: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let popover = NSPopover()
    let model = Model()
    let cache = NSString(string: "~/.claude/memmon/latest.json").expandingTildeInPath

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)

        configurePopover(popover, model: model, onQuit: { NSApp.terminate(nil) })
        popover.behavior = .transient
        popover.animates = true
        popover.delegate = self

        statusItem.button?.action = #selector(toggle)
        statusItem.button?.target = self
        updateTitleFromCache()
        // Cheap: reads a ~1KB file the sampler already wrote. No process spawn.
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in
            self.updateTitleFromCache()
        }
    }

    @objc func toggle() {
        if popover.isShown {
            popover.performClose(nil)
        } else if let b = statusItem.button {
            NSApp.activate(ignoringOtherApps: true)
            popover.show(relativeTo: b.bounds, of: b, preferredEdge: .minY)
            model.refresh()          // sync on open — the only expensive work
            model.startTicking()
        }
    }

    func popoverDidClose(_ note: Notification) {
        model.stopTicking()
        model.popoverClosed()
        updateTitleFromCache()
    }

    /// Colour comes from the pressure model, not from swap usage: macOS grows
    /// swap on demand, so a large swapfile on its own means nothing.
    func updateTitleFromCache() {
        guard let d = FileManager.default.contents(atPath: cache),
              let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any],
              let title = statusTitle(j, now: Date().timeIntervalSince1970) else { return }
        statusItem.button?.attributedTitle = NSAttributedString(
            string: title,
            attributes: [.font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .regular)])
    }
}

// MARK: - offscreen render, accessibility audit and self-tests
//
// `MemmonBar --render out.png --fixture f.json` draws the popover from a
// synthetic owners payload and exits. The UI is otherwise unreviewable without
// screen-recording permission, and a layout that only breaks under a partial
// stop is exactly the state you cannot reproduce on demand.

let ARGS = CommandLine.arguments

func argValue(_ flag: String) -> String? {
    guard let i = ARGS.firstIndex(of: flag), i + 1 < ARGS.count else { return nil }
    return ARGS[i + 1]
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(2)
}

struct RenderOptions {
    var dark = false
    var select: String?
    var confirm: String?
    var outcome: String?
    var sort: SortKey = .memory
    var techOpen = false
    var openGate = false
}

/// A fixture may name a `_base` fixture whose keys it overrides, and carries
/// its own view state in `_view`; command-line flags win over both.
func loadFixture(_ path: String) -> ([String: Any], [String: Any]) {
    guard let d = FileManager.default.contents(atPath: path),
          var j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else {
        fail("cannot read fixture \(path)")
    }
    let view = j["_view"] as? [String: Any] ?? [:]
    if let base = j["_base"] as? String {
        let dir = (path as NSString).deletingLastPathComponent
        var (b, _) = loadFixture((dir as NSString).appendingPathComponent(base))
        for (k, v) in j where k != "_base" && k != "_view" { b[k] = v }
        j = b
    }
    return (j, view)
}

func renderOptions(_ view: [String: Any], fixtureDir: String) -> RenderOptions {
    var o = RenderOptions()
    o.select = argValue("--select") ?? str(view["select"])
    o.confirm = argValue("--confirm") ?? str(view["confirm"])
    if let out = argValue("--outcome") {
        o.outcome = out
    } else if let out = str(view["outcome"]) {
        o.outcome = (fixtureDir as NSString).appendingPathComponent(out)
    }
    o.sort = SortKey(rawValue: argValue("--sort") ?? str(view["sort"]) ?? "memory") ?? .memory
    o.techOpen = ARGS.contains("--tech-open") || (view["tech_open"] as? Bool ?? false)
    o.openGate = ARGS.contains("--open-gate") || (view["open_gate"] as? Bool ?? false)
    o.dark = ARGS.contains("--dark")
    return o
}

/// Builds the model a fixture describes, including any confirm or outcome
/// state, by driving the same decoders the live app uses.
func fixtureModel(_ json: [String: Any], _ o: RenderOptions) -> Model {
    if let now = num(json["_now"]) { clockOverride = now }
    guard let snap = OwnersSnap.decode(json) else { fail("fixture is not an owners payload (schema 2)") }
    let m = Model()
    m.live = false
    m.snap = snap
    m.sort = o.sort
    m.expanded = o.select
    if o.techOpen, let s = o.select { m.techOpen = [s] }
    guard let action = o.confirm else {
        if o.outcome != nil { fail("--outcome needs --confirm") }
        return m
    }
    guard let id = o.select, let owner = snap.rows.first(where: { $0.id == id }) else {
        fail("--confirm needs --select with an owner_id from the fixture")
    }
    let kind: ConfirmRequest.Kind
    switch action {
    case "end-session": kind = .endSession
    case "quit-app": kind = .quitApp
    case "stop-command": kind = .stopCommand
    default:
        guard let job = owner.jobs.first(where: { $0.stopAction == action && $0.token != nil }) else {
            fail("owner \(id) has no job with action \(action)")
        }
        kind = .job(job)
    }
    m.ask(kind, owner)
    guard let path = o.outcome else { return m }
    guard let d = FileManager.default.contents(atPath: path),
          let out = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else {
        fail("cannot read outcome \(path)")
    }
    if let app = out["app"] as? [String: Any] {
        guard let tok = owner.token, let t = AppToken.decode(tok) else { fail("owner has no app token") }
        let states = (app["results"] as? [[String: Any]] ?? []).map {
            InstanceOutcome(pid: Int32(int($0["pid"]) ?? 0),
                            state: InstanceState(rawValue: str($0["state"]) ?? "") ?? .running)
        }
        let forced = app["forced"] as? Bool ?? false
        m.applyApp(.done(states), m.confirm!, t, forced: forced)
    } else {
        var r = CLIResult(exit: int(out["exit"]).map { Int32($0) }, stdout: Data())
        r.timedOut = out["timed_out"] as? Bool ?? false
        if out["after_force"] as? Bool ?? false { m.confirm?.forcing = true }
        if let s = out["stdout"] as? String {
            r.stdout = s.data(using: .utf8) ?? Data()
        } else if let obj = out["stdout"], JSONSerialization.isValidJSONObject(obj) {
            r.stdout = (try? JSONSerialization.data(withJSONObject: obj)) ?? Data()
        }
        m.apply(ActView.classify(r, timeout: CLI.actTimeout), m.confirm!)
    }
    m.refreshing = false
    return m
}

@MainActor
func fixtureRoot(_ model: Model, _ o: RenderOptions) -> some View {
    ContentView(model: model, onQuit: {}, flattened: true, previewOpenGate: o.openGate)
        .environment(\.colorScheme, o.dark ? .dark : .light)
}

@MainActor
func prepareFixture() -> (Model, RenderOptions) {
    guard let path = argValue("--fixture") else { fail("--fixture <owners json> is required") }
    let (json, view) = loadFixture(path)
    let o = renderOptions(view, fixtureDir: (path as NSString).deletingLastPathComponent)
    // ImageRenderer ignores a forced NSApp.appearance when resolving dynamic
    // NSColors; the colorScheme environment is what selects the token set.
    NSApp.appearance = NSAppearance(named: o.dark ? .darkAqua : .aqua)
    return (fixtureModel(json, o), o)
}

@MainActor
func renderFixture(to path: String) {
    let (model, o) = prepareFixture()
    let renderer = ImageRenderer(content: fixtureRoot(model, o))
    renderer.scale = 2
    guard let img = renderer.nsImage,
          let tiff = img.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else {
        fail("render failed")
    }
    do { try png.write(to: URL(fileURLWithPath: path)) } catch { fail("cannot write \(path)") }
    let darkOnly = darkTokenPixels(in: rep)
    let corner = rep.colorAt(x: 4, y: 4)?.usingColorSpace(.sRGB)
    let hex = corner.map { String(format: "#%02x%02x%02x", Int(round($0.redComponent * 255)),
                                  Int(round($0.greenComponent * 255)), Int(round($0.blueComponent * 255))) } ?? "?"
    print("rendered \(path) \(rep.pixelsWide)x\(rep.pixelsHigh) bg=\(hex) dark_tokens=\(darkOnly)")
}

/// Counts pixels of `rep` that carry one of the dark-only surface tokens (bg,
/// panel, soft) exactly as the renderer draws them in dark mode. A light
/// render must have none.
@MainActor
func darkTokenPixels(in rep: NSBitmapImageRep) -> Int {
    let swatch = ImageRenderer(content: HStack(spacing: 0) { P.bg; P.panel; P.soft }
        .frame(width: 3, height: 1).environment(\.colorScheme, .dark))
    swatch.scale = 1
    guard let img = swatch.nsImage, let tiff = img.tiffRepresentation,
          let sw = NSBitmapImageRep(data: tiff), let swData = sw.bitmapData,
          let data = rep.bitmapData, sw.bitsPerPixel == rep.bitsPerPixel, rep.bitsPerPixel == 32 else {
        return -1
    }
    let tokens = (0..<3).map { k in (0..<3).map { Int(swData[k * 4 + $0]) } }
    var hits = 0
    for y in stride(from: 0, to: rep.pixelsHigh, by: 2) {
        let row = data + y * rep.bytesPerRow
        for x in stride(from: 0, to: rep.pixelsWide, by: 2) {
            let p = row + x * 4
            for t in tokens where abs(Int(p[0]) - t[0]) <= 1 && abs(Int(p[1]) - t[1]) <= 1
                && abs(Int(p[2]) - t[2]) <= 1 {
                hits += 1
                break
            }
        }
    }
    return hits
}

/// Prints the accessibility tree SwiftUI builds for a fixture, one element per
/// line: depth, role, label, value, value description and frame height,
/// tab-separated.
final class A11yDump: NSObject, NSApplicationDelegate {
    var window: NSWindow?
    func applicationDidFinishLaunching(_ note: Notification) {
        MainActor.assumeIsolated {
            let (model, o) = prepareFixture()
            let host = NSHostingView(rootView: fixtureRoot(model, o))
            let size = host.fittingSize
            host.frame = NSRect(origin: .zero, size: size)
            let w = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: size.width, height: size.height),
                             styleMask: [.borderless], backing: .buffered, defer: false)
            w.contentView = host
            window = w
            // SwiftUI builds its accessibility nodes only once an assistive
            // client is present; this is how one announces itself.
            NSApp.setValue(true, forKey: "accessibilityEnhancedUserInterface")
            _ = NSApp.accessibilityFocusedUIElement
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self.walk(host, 0)
                exit(0)
            }
        }
    }

    func walk(_ element: Any, _ depth: Int) {
        for row in axRows(element) {
            print(([String(row.depth), row.role, row.label, row.value, row.described,
                    String(format: "%.0f", row.height)]).joined(separator: "\t"))
        }
    }
}

struct AXRow {
    var depth: Int, role: String, label: String, value: String, described: String
    var height: Double = 0
}

/// Flattens the accessibility tree under `element`, depth first.
func axRows(_ element: Any, _ depth: Int = 0) -> [AXRow] {
    guard depth < 40, let e = element as? NSAccessibilityElementProtocol else { return [] }
    let o = e as AnyObject
    // KVC rather than the typed accessors: a progress element reports a
    // number where the protocol promises a string.
    func attribute(_ key: String) -> String {
        guard let n = o as? NSObject, n.responds(to: Selector(key)),
              let v = n.value(forKey: key) else { return "" }
        return "\(v)".replacingOccurrences(of: "\n", with: " ").replacingOccurrences(of: "\t", with: " ")
    }
    // SwiftUI files a textual accessibilityValue under ValueDescription,
    // which is what VoiceOver reads.
    let frame = ((o as? NSObject)?.value(forKey: "accessibilityFrame") as? NSValue)?.rectValue ?? .zero
    var rows = [AXRow(depth: depth, role: attribute("accessibilityRole"),
                      label: attribute("accessibilityLabel"), value: attribute("accessibilityValue"),
                      described: attribute("accessibilityValueDescription"),
                      height: Double(frame.height))]
    for c in (o.accessibilityChildren?() ?? nil) ?? [] { rows += axRows(c, depth + 1) }
    return rows
}

final class KeyableWindow: NSWindow {
    override var canBecomeKey: Bool { true }
}

/// Hosts the real popover root (scrolling, adaptive height, live overlay) in an
/// offscreen window and drives it: sizes, keyboard focus, Esc and Return, and
/// the freshness ticker. The window is never ordered on screen and the app
/// never activates, so nothing takes focus from the user.
final class HostSelftest: NSObject, NSApplicationDelegate {
    let check: String
    var window: NSWindow?
    var popover: NSPopover?
    init(check: String) { self.check = check }

    func spin(_ seconds: Double) { RunLoop.main.run(until: Date().addingTimeInterval(seconds)) }

    func key(_ chars: String, _ code: UInt16, in w: NSWindow) {
        guard let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                                       windowNumber: w.windowNumber, context: nil, characters: chars,
                                       charactersIgnoringModifiers: chars, isARepeat: false,
                                       keyCode: code) else { return }
        w.sendEvent(e)
    }

    /// A second offscreen window to anchor the popover to.
    func probeAnchor() -> NSView {
        let anchorWindow = KeyableWindow(contentRect: NSRect(x: -21000, y: -21000, width: 20, height: 20),
                                         styleMask: [.borderless], backing: .buffered, defer: false)
        anchors.append(anchorWindow)
        // NSPopover only opens from a view in a visible window. This one is
        // ordered in far off every display and the app never activates.
        anchorWindow.orderFrontRegardless()
        return anchorWindow.contentView!
    }
    var anchors: [NSWindow] = []

    func phase(_ m: Model) -> String {
        switch m.confirm?.phase {
        case nil: return "closed"
        case .ask?: return "ask"
        case .working?: return "working"
        case .partial?: return "partial"
        case .appPartial?: return "app_partial"
        }
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        MainActor.assumeIsolated {
            let (model, _) = prepareFixture()
            let pop = NSPopover()
            let host = configurePopover(pop, model: model, onQuit: {})
            popover = pop
            let w = KeyableWindow(contentRect: NSRect(x: -20000, y: -20000, width: 380, height: 900),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            w.contentView = host.view
            window = w
            NSApp.setValue(true, forKey: "accessibilityEnhancedUserInterface")
            spin(0.6)
            var report: [String: Any] = ["check": check]
            switch check {
            case "size":
                let fit = host.view.fittingSize
                report["fitting"] = [fit.width, fit.height]
                report["preferred"] = [host.preferredContentSize.width, host.preferredContentSize.height]
                report["popover_unshown"] = [pop.contentSize.width, pop.contentSize.height]
                pop.show(relativeTo: .zero, of: probeAnchor(), preferredEdge: .minY)
                spin(0.4)
                report["popover_shown"] = pop.isShown
                report["popover"] = [pop.contentSize.width, pop.contentSize.height]
                let frame = host.view.window?.frame ?? .zero
                report["popover_on_a_display"] = NSScreen.screens.contains { $0.frame.intersects(frame) }
                pop.close()
            case "keys":
                report["phase_before"] = phase(model)
                report["focus"] = model.overlayFocus ?? NSNull()
                // Tab and shift-Tab must cycle inside the overlay; a nil focus
                // would mean it went to the dimmed list behind it.
                var trail: [Any] = []
                for shift in [false, false, false, true, true] {
                    guard let e = NSEvent.keyEvent(with: .keyDown, location: .zero,
                                                   modifierFlags: shift ? [.shift] : [], timestamp: 0,
                                                   windowNumber: w.windowNumber, context: nil,
                                                   characters: "\t", charactersIgnoringModifiers: "\t",
                                                   isARepeat: false, keyCode: 48) else { continue }
                    w.sendEvent(e)
                    spin(0.15)
                    trail.append(model.overlayFocus ?? NSNull())
                }
                report["tab_trail"] = trail
                key("\r", 36, in: w)
                spin(0.3)
                report["after_return_phase"] = phase(model)
                report["after_return_actions"] = model.actionLog
                key("\u{1b}", 53, in: w)
                spin(0.4)
                report["after_esc_phase"] = phase(model)
                report["after_esc_banner"] = model.banner.map { $0.title + " " + $0.body } ?? NSNull()
                report["actions"] = model.actionLog
            case "force-ttl":
                // Age the app partial result, then press Force.
                let age = argValue("--age").flatMap(Double.init) ?? 0
                if case .appPartial(let t, let r, _)? = model.confirm?.phase {
                    model.confirm?.phase = .appPartial(t, r, watchEnded: model.uptime() - age)
                }
                model.force()
                spin(0.2)
                report["phase"] = phase(model)
                report["banner"] = model.banner.map { $0.title + " " + $0.body } ?? NSNull()
                report["actions"] = model.actionLog
            case "close":
                model.popoverClosed()
                spin(0.2)
                report["phase"] = phase(model)
                report["banner"] = model.banner.map { $0.title + " " + $0.body } ?? NSNull()
            case "popover-keys":
                // The same keys inside a shown, transient NSPopover: Esc must
                // close the overlay, not the popover.
                pop.behavior = .transient
                pop.show(relativeTo: .zero, of: probeAnchor(), preferredEdge: .minY)
                spin(0.5)
                guard let pw = host.view.window else { fail("popover has no window") }
                report["popover_on_a_display"] = NSScreen.screens.contains { $0.frame.intersects(pw.frame) }
                report["focus"] = model.overlayFocus ?? NSNull()
                key("\r", 36, in: pw)
                spin(0.3)
                report["after_return_phase"] = phase(model)
                key("\u{1b}", 53, in: pw)
                spin(0.4)
                report["after_esc_phase"] = phase(model)
                report["popover_open_after_esc"] = pop.isShown
                report["actions"] = model.actionLog
                pop.close()
            case "ticker":
                func freshness() -> String {
                    axRows(host.view).first { $0.label.hasPrefix("Sampled") || $0.label.hasPrefix("Sample time") }?.label ?? ""
                }
                report["before"] = freshness()
                model.startTicking(every: 0.1)
                clockOverride = (clockOverride ?? nowTs()) + 5
                spin(0.5)
                report["after_5s"] = freshness()
                clockOverride = (clockOverride ?? nowTs()) + 90
                spin(0.5)
                report["after"] = freshness()
                report["ticks"] = model.tick
                model.stopTicking()
            default:
                fail("unknown check \(check)")
            }
            let data = try! JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
            print(String(data: data, encoding: .utf8)!)
            exit(0)
        }
    }
}

// Test seams. None of these touch a real process: --act-probe runs a stub
// script, --selftest-quit-app drives a fake AppControl on a virtual clock.

final class FakeApp: RunningAppHandle {
    let bundleIdentifier: String?
    let launchDate: Date?
    var isTerminated = false
    /// The process itself, which memmon's identity check sees.
    var alive = true
    /// Whether AppKit knows it as an app (false for helpers and widgets).
    var listed = true
    let ignoresTerminate: Bool
    let pid: Int32
    let log: (String) -> Void
    init(pid: Int32, bundle: String, launched: Double, ignoresTerminate: Bool, log: @escaping (String) -> Void) {
        self.pid = pid; bundleIdentifier = bundle
        launchDate = Date(timeIntervalSince1970: launched)
        self.ignoresTerminate = ignoresTerminate; self.log = log
    }
    func terminate() -> Bool {
        log("terminate \(pid)")
        if !ignoresTerminate { isTerminated = true; alive = false }
        return true
    }
    func forceTerminate() -> Bool { log("force \(pid)"); isTerminated = true; alive = false; return true }
}

final class FakeApps: AppControl {
    var apps: [Int32: FakeApp] = [:]
    /// Models AppKit's lag: a lookup can still return a handle that has just
    /// reported termination.
    var lingering = false
    var t = 0.0
    func app(pid: Int32) -> RunningAppHandle? {
        guard let a = apps[pid], a.listed else { return nil }
        return a.isTerminated && !lingering ? nil : a
    }
    func now() -> Double { t }
    func sleep(_ seconds: Double) { t += seconds }
    /// What memmon's identity check would report.
    func liveness() -> InstanceLiveness {
        var out: InstanceLiveness = [:]
        for (pid, a) in apps { out[pid] = a.alive }
        return out
    }
}

func selftestQuitApp(_ scenario: String) {
    var calls: [String] = []
    let fake = FakeApps()
    let bundle = "com.example.containers"
    let log: (String) -> Void = { calls.append($0) }
    fake.apps[101] = FakeApp(pid: 101, bundle: bundle, launched: 1000, ignoresTerminate: false, log: log)
    fake.apps[102] = FakeApp(pid: 102, bundle: bundle, launched: 2000, ignoresTerminate: true, log: log)
    var token = AppToken(bundleId: bundle, instances: [AppTokenInstance(pid: 101, launchDate: 1000),
                                                       AppTokenInstance(pid: 102, launchDate: 2000)])
    var verifyFails = false
    switch scenario {
    case "two-one-stubborn": break
    case "pid-reused":
        fake.apps[102] = FakeApp(pid: 102, bundle: "com.example.other", launched: 2000,
                                 ignoresTerminate: true, log: log)
    case "relaunched":
        token.instances[1].launchDate = 1500
    case "lingering-handle":
        fake.lingering = true
    case "gone":
        fake.apps[101]?.alive = false
        fake.apps[101]?.listed = false
    case "not-an-app":
        // A helper or widget: alive, but AppKit has no app for it.
        fake.apps[102]?.listed = false
    case "appkit-says-gone":
        // AppKit drops the handle but the process keeps running.
        fake.apps[102] = FakeApp(pid: 102, bundle: bundle, launched: 2000, ignoresTerminate: false, log: log)
        fake.apps[102]?.listed = true
        fake.lingering = false
    case "verify-fails":
        verifyFails = true
    case "quit-before-force":
        break
    default:
        fail("unknown scenario \(scenario)")
    }
    let engine = QuitApp(control: fake, verify: {
        if scenario == "appkit-says-gone" { fake.apps[102]?.alive = true }
        return verifyFails ? nil : fake.liveness()
    })
    let first = engine.quit(token, alive: fake.liveness())
    let firstCalls = calls
    var report: [String: Any] = [
        "after_quit": first.map { ["pid": Int($0.pid), "state": $0.state.rawValue] },
        "complete_after_quit": first.allSatisfy { $0.state.done },
        "quit_calls": firstCalls,
        "virtual_seconds": fake.t,
    ]
    if first.contains(where: { $0.state == InstanceState.running }) {
        if scenario == "quit-before-force" {
            // The stubborn instance quits on its own before Force is pressed.
            fake.apps[102]?.alive = false
            fake.apps[102]?.listed = false
        }
        calls = []
        let second = engine.force(token, after: first)
        report["after_force"] = second.map { ["pid": Int($0.pid), "state": $0.state.rawValue] }
        report["complete_after_force"] = second.allSatisfy { $0.state.done }
        report["force_calls"] = calls
    }
    let data = try! JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
}

/// Drives the real refresh path against a stub memmon whose first scan
/// outlives the timeout: no second scanner may start while it runs, and the
/// request made meanwhile runs once it ends.
final class RefreshSelftest: NSObject, NSApplicationDelegate {
    func spin(_ seconds: Double) { RunLoop.main.run(until: Date().addingTimeInterval(seconds)) }

    func applicationDidFinishLaunching(_ note: Notification) {
        guard let script = argValue("--script") else { fail("usage: --selftest-refresh --script <stub>") }
        CLI.script = script
        CLI.ownersTimeout = argValue("--timeout").flatMap(Double.init) ?? 0.3
        let m = Model()
        var report: [String: Any] = [:]
        m.refresh()
        spin(0.8)
        report["after_timeout"] = ["error": m.loadError ?? NSNull(), "still_sampling": m.stillSampling,
                                   "scans": m.scansStarted] as [String: Any]
        m.refresh()
        spin(0.3)
        report["while_busy_scans"] = m.scansStarted
        let deadline = Date().addingTimeInterval(8)
        while Date() < deadline && !(m.loaded && !m.refreshing) { spin(0.1) }
        report["end"] = ["scans": m.scansStarted, "loaded": m.loaded,
                         "still_sampling": m.stillSampling] as [String: Any]
        let data = try! JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
        print(String(data: data, encoding: .utf8)!)
        exit(0)
    }
}

func actProbe() {
    guard let script = argValue("--script"), let sep = ARGS.firstIndex(of: "--") else {
        fail("usage: --act-probe --script <stub> [--timeout s] -- <memmon args>")
    }
    CLI.script = script
    let timeout = argValue("--timeout").flatMap(Double.init) ?? CLI.actTimeout
    let r = CLI.run(Array(ARGS[(sep + 1)...]), timeout: timeout)
    var report: [String: Any] = ["view": "", "exit": r.exit.map { Int($0) } ?? NSNull(), "timed_out": r.timedOut]
    let view = ActView.classify(r, timeout: timeout)
    report["view"] = view.name
    switch view {
    case .success(let o), .partial(let o), .refused(let o):
        report["result"] = o.result
        report["force_token"] = o.forceToken ?? NSNull()
        report["reason"] = o.reason ?? NSNull()
    case .error(let e):
        report["message"] = e
    }
    let data = try! JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
}

/// Runs the real quit-app path against a stub memmon and a fake AppControl:
/// lock, verify-app with the descriptor inherited, then quit and watch.
func quitProbe() {
    guard let script = argValue("--script"), let lock = argValue("--lock-path"),
          let token = argValue("--token") else {
        fail("usage: --quit-probe --script <stub> --lock-path <file> --token <tok> [--timeout s]")
    }
    CLI.script = script
    ActionsLock.path = lock
    if let t = argValue("--timeout").flatMap(Double.init) { CLI.actTimeout = t }
    guard let parsed = AppToken.decode(token) else { fail("token does not decode") }
    let fake = FakeApps()
    var calls: [String] = []
    // Each terminate() also reports whether actions.lock is still held, by
    // trying it from a second open file description.
    let lockHeld: () -> Bool = {
        let fd = open(lock, O_RDWR)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        if flock(fd, LOCK_EX | LOCK_NB) == 0 { flock(fd, LOCK_UN); return false }
        return true
    }
    for i in parsed.instances {
        fake.apps[i.pid] = FakeApp(pid: i.pid, bundle: parsed.bundleId, launched: i.launchDate ?? 0,
                                   ignoresTerminate: false,
                                   log: { calls.append($0 + (lockHeld() ? " locked" : " unlocked")) })
    }
    var report: [String: Any] = [:]
    let started = ProcessInfo.processInfo.systemUptime
    switch Model.quitApp(token: token, parsed: parsed, control: fake) {
    case .refused(let o): report["view"] = "refused"; report["reason"] = o.reason ?? NSNull()
    case .error(let e): report["view"] = "error"; report["message"] = e
    case .done(let out):
        report["view"] = "done"
        report["states"] = out.map { $0.state.rawValue }
    }
    report["seconds"] = ProcessInfo.processInfo.systemUptime - started
    report["lock_free_after"] = !lockHeld()
    report["calls"] = calls
    let data = try! JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
}

if ARGS.contains("--selftest-quit-app") {
    selftestQuitApp(argValue("--selftest-quit-app") ?? "two-one-stubborn")
    exit(0)
}
if ARGS.contains("--act-probe") { actProbe(); exit(0) }
if ARGS.contains("--title-probe") {
    guard let path = argValue("--latest"), let now = argValue("--now").flatMap(Double.init),
          let d = FileManager.default.contents(atPath: path),
          let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else {
        fail("usage: --title-probe --latest <latest.json> --now <epoch>")
    }
    print(statusTitle(j, now: now) ?? "")
    exit(0)
}
if ARGS.contains("--selftest-refresh") {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let selftest = RefreshSelftest()
    app.delegate = selftest
    app.run()
}
if ARGS.contains("--quit-probe") { quitProbe(); exit(0) }
if let out = argValue("--render") {
    _ = NSApplication.shared          // AppKit must exist for text rendering
    MainActor.assumeIsolated { renderFixture(to: out) }
    exit(0)
}
if let check = argValue("--selftest-host") {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let selftest = HostSelftest(check: check)
    app.delegate = selftest
    app.run()
}
if ARGS.contains("--a11y-dump") {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let dump = A11yDump()
    app.delegate = dump
    app.run()
}

let app = NSApplication.shared
let controller = Controller()
app.delegate = controller
app.run()
