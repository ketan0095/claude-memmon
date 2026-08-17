// MemmonBar — menu-bar front end for memmon.
//
// Deliberately does no background work. `memmon --json` (which spawns `top`,
// ~1.4s) runs ONLY when the popover opens. The title is refreshed from the tiny
// cached sample the launchd sampler already writes — a file read, never a spawn.
//
// Build:  swiftc -O -o MemmonBar MemmonBar.swift -framework Cocoa

import Cocoa
import SwiftUI

// MARK: - units

let GB = 1024.0 * 1024.0 * 1024.0
let MB = 1024.0 * 1024.0

func human(_ b: Double) -> String {
    if b >= GB { return String(format: "%.1fG", b / GB) }
    if b >= MB { return String(format: "%.0fM", b / MB) }
    return String(format: "%.0fB", b)
}

func durS(_ sec: Int) -> String {
    if sec >= 86400 { return "\(sec / 86400)d\((sec % 86400) / 3600)h" }
    if sec >= 3600 { return "\(sec / 3600)h\(String(format: "%02d", (sec % 3600) / 60))m" }
    return "\(sec / 60)m"
}

func clockOf(_ ts: Double) -> String {
    let f = DateFormatter(); f.dateFormat = "HH:mm"
    return f.string(from: Date(timeIntervalSince1970: ts))
}

func relative(_ ts: Double) -> String {
    let s = Int(Date().timeIntervalSince1970 - ts)
    if s < 60 { return "\(s)s ago" }
    if s < 3600 { return "\(s / 60)m ago" }
    if s < 86400 { return "\(s / 3600)h ago" }
    return "\(s / 86400)d ago"
}

func eventTime(_ ts: Double) -> String {
    let f = DateFormatter(); f.dateFormat = "d MMM, HH:mm"
    return f.string(from: Date(timeIntervalSince1970: ts))
}

func retainedDate(_ ts: Double, includeTime: Bool = false) -> String {
    let f = DateFormatter(); f.dateFormat = includeTime ? "d MMM, HH:mm" : "d MMM"
    return f.string(from: Date(timeIntervalSince1970: ts))
}

func eventClock(_ ts: Double) -> String {
    let f = DateFormatter(); f.dateFormat = "HH:mm"
    return f.string(from: Date(timeIntervalSince1970: ts))
}

// MARK: - palette

enum P {
    // Deep indigo base with a violet lift, so coloured accents read as light
    // rather than as stains on flat grey.
    static let bgTop = Color(red: 0.055, green: 0.043, blue: 0.086)
    static let bgMid = Color(red: 0.094, green: 0.071, blue: 0.161)
    static let bgBot = Color(red: 0.129, green: 0.086, blue: 0.220)

    static let card = Color(red: 1, green: 1, blue: 1).opacity(0.045)
    static let cardHi = Color(red: 1, green: 1, blue: 1).opacity(0.085)
    static let stroke = Color.white.opacity(0.085)
    static let strokeHi = Color.white.opacity(0.16)

    static let text = Color(red: 0.96, green: 0.95, blue: 1.0)
    static let dim = Color(red: 0.72, green: 0.70, blue: 0.82)
    static let faint = Color(red: 0.52, green: 0.50, blue: 0.63)

    static let green = Color(red: 0.204, green: 0.867, blue: 0.596)   // #34DD98
    static let amber = Color(red: 0.984, green: 0.749, blue: 0.235)   // #FBBF3C
    static let red = Color(red: 0.984, green: 0.443, blue: 0.518)     // #FB7184
    static let violet = Color(red: 0.694, green: 0.529, blue: 0.988)  // #B187FC
    static let blue = Color(red: 0.298, green: 0.749, blue: 0.973)    // #4CBFF8
    static let fuchsia = Color(red: 0.910, green: 0.475, blue: 0.976) // #E879F9

    /// RAM is sky, swap is fuchsia — two hues that never read as the same thing.
    static let ram = blue
    static let swap = fuchsia

    static func tint(_ level: String) -> Color {
        switch level {
        case "CRITICAL", "DANGER": return red
        case "WATCH": return amber
        default: return green
        }
    }

    /// Matches Claude Code's own session convention rather than traffic-light
    /// intuition: green means finished, grey means still going. Consistency with
    /// the tool these sessions belong to beats a prettier mapping.
    static func stateColor(_ s: String) -> Color {
        switch s {
        case "done", "stopped": return green      // completed
        case "working": return dim                // working
        case "blocked": return amber              // idle, waiting on you
        case "terminal": return blue              // interactive terminal session
        default: return faint
        }
    }

    static func stateLabel(_ s: String) -> String {
        switch s {
        case "done", "stopped": return "completed"
        case "working": return "working"
        case "blocked": return "idle"             // Claude's 'blocked' = awaiting input
        case "terminal": return "terminal"
        default: return s
        }
    }
}

// MARK: - model

struct Child: Identifiable {
    let id = UUID()
    var tag: String, worktree: String
    var mem: Double, pid: Int, age: Int
}

struct Agent: Identifiable {
    let id = UUID()
    var kind: String, goal: String, active: Bool
}

struct Sess: Identifiable {
    let id = UUID()
    var name: String, state: String, doing: String
    var total: Double, ram: Double, swap: Double
    var procs: Int, subActive: Int
    var root: Int
    var children: [Child] = []
    var agents: [Agent] = []       // active only — finished ones are noise
    var subFinished: Int = 0
    var started: [String] = []
}

struct WT: Identifiable {
    let id = UUID()
    var name: String, tag: String
    var mem: Double, ram: Double, swap: Double
    var procs: Int, orphans: Int
}

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

struct App: Identifiable {
    let id = UUID()
    var name: String
    var mem: Double
    var procs: Int
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
}

struct Snap {
    var ramUsed = 0.0, ramTotal = 1.0, swapUsed = 0.0, swapTotal = 1.0
    var free = 0.0, load = 0.0, compressed = 0.0
    var level = "HEALTHY", reasons: [String] = [], headroom: Double? = nil
    var score = 0
    var advice = "", nextLevel: String? = nil, toNext: Int? = nil
    var sessions: [Sess] = [], worktrees: [WT] = []
    var orphanTotal = 0.0, orphanCount = 0
    var idleSpares = 0, idleSpareMem = 0.0
    var apps: [App] = []
    var gate = GateStats()
}

final class Model: ObservableObject {
    @Published var snap = Snap()
    @Published var loaded = false
    @Published var refreshing = false
    @Published var lastSync: Date?

    let python = "/usr/bin/python3"
    let script = NSString(string: "~/.claude/memmon/memmon.py").expandingTildeInPath

    @discardableResult
    func run(_ args: [String]) -> Data? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [script] + args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return nil }
        let out = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return out
    }

    /// Full sync. Kicked off when the popover opens; the previous snapshot stays
    /// on screen meanwhile so the UI never blanks or blocks.
    func refresh() {
        guard !refreshing else { return }
        refreshing = true
        DispatchQueue.global(qos: .userInitiated).async {
            var parsed: Snap?
            if let d = self.run(["--json"]),
               let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] {
                parsed = Model.decode(j)
            }
            DispatchQueue.main.async {
                if let parsed { self.snap = parsed; self.loaded = true; self.lastSync = Date() }
                self.refreshing = false
            }
        }
    }

    static func decode(_ j: [String: Any]) -> Snap {
        func number(_ value: Any?) -> Double {
            (value as? NSNumber)?.doubleValue ?? 0
        }
        func integer(_ value: Any?) -> Int {
            (value as? NSNumber)?.intValue ?? 0
        }
        var s = Snap()
        if let vm = j["vm"] as? [String: Any] {
            s.ramUsed = vm["ram_used"] as? Double ?? 0
            s.ramTotal = max(vm["ram_total"] as? Double ?? 1, 1)
            s.swapUsed = vm["swap_used"] as? Double ?? 0
            s.swapTotal = max(vm["swap_total"] as? Double ?? 1, 1)
            s.free = vm["free_pct"] as? Double ?? 0
            s.load = vm["load"] as? Double ?? 0
            s.compressed = vm["compressor"] as? Double ?? 0
        }
        if let p = j["pressure"] as? [String: Any] {
            s.level = p["level"] as? String ?? "HEALTHY"
            s.reasons = p["reasons"] as? [String] ?? []
            s.headroom = p["headroom_min"] as? Double
            s.score = p["score"] as? Int ?? 0
            s.advice = p["advice"] as? String ?? ""
            s.nextLevel = p["next_level"] as? String
            s.toNext = p["to_next"] as? Int
        }
        s.sessions = (j["sessions"] as? [[String: Any]] ?? []).map { d in
            let subs = d["subagents_active"] as? [[String: Any]] ?? []
            let all = d["subagents"] as? [[String: Any]] ?? []
            let kids = d["top_children"] as? [[String: Any]] ?? []
            // started is {service: [iso_ts, command]}. "Docker" and "Docker VM"
            // are one action to the user, so collapse them.
            var services = Set((d["started"] as? [String: Any] ?? [:]).keys
                .map { $0 == "Docker VM" ? "Docker" : $0 })
            services.remove("")
            // Distinct agent types only; an omega wave spawns the same auditor
            // several times and listing each is pure repetition.
            var seenKinds = Set<String>()
            let activeAgents = subs.compactMap { a -> Agent? in
                let k = a["kind"] as? String ?? "agent"
                guard !seenKinds.contains(k) else { return nil }
                seenKinds.insert(k)
                return Agent(kind: k, goal: a["goal"] as? String ?? "", active: true)
            }
            return Sess(
                name: d["name"] as? String ?? "?",
                state: d["state"] as? String ?? "",
                doing: d["doing"] as? String ?? "",
                total: d["mem"] as? Double ?? 0,
                ram: d["ram"] as? Double ?? 0,
                swap: d["swap"] as? Double ?? 0,
                procs: d["nproc"] as? Int ?? 0,
                subActive: subs.count,
                root: d["root"] as? Int ?? 0,
                children: kids.map {
                    Child(tag: $0["tag"] as? String ?? "?",
                          worktree: $0["worktree"] as? String ?? "",
                          mem: $0["mem"] as? Double ?? 0,
                          pid: $0["pid"] as? Int ?? 0,
                          age: $0["age"] as? Int ?? 0)
                },
                agents: Array(activeAgents.prefix(5)),
                subFinished: max(0, all.count - subs.count),
                started: services.sorted())
        }
        s.worktrees = (j["worktrees"] as? [[String: Any]] ?? []).map { d in
            WT(name: d["name"] as? String ?? "?",
               tag: d["tag"] as? String ?? "",
               mem: d["mem"] as? Double ?? 0,
               ram: d["ram"] as? Double ?? 0,
               swap: d["swap"] as? Double ?? 0,
               procs: d["n"] as? Int ?? 0,
               orphans: d["orphans"] as? Int ?? 0)
        }
        s.orphanTotal = j["orphan_total"] as? Double ?? 0
        s.orphanCount = (j["orphans"] as? [[String: Any]])?.count ?? 0
        // Non-Claude memory feeds the verdict, so it has to be visible. A
        // browser routinely outweighs every session combined, and no amount of
        // scoping a build addresses that.
        s.apps = ((j["apps"] as? [String: Any]) ?? [:]).compactMap { k, v in
            guard let d = v as? [String: Any] else { return nil }
            return App(name: k, mem: d["mem"] as? Double ?? 0,
                       procs: d["n"] as? Int ?? 0)
        }.sorted { $0.mem > $1.mem }
        if let g = j["gate"] as? [String: Any] {
            s.gate.installed = g["installed"] as? Bool ?? false
            s.gate.paused = g["paused"] as? Bool ?? false
            if let until = g["paused_until"], !(until is NSNull) {
                s.gate.pausedUntil = number(until)
            }
            if let policy = g["policy"] as? [String: Any] {
                s.gate.mode = policy["mode"] as? String ?? "block-critical"
            }
            if let counts = g["counts"] as? [String: Any] {
                s.gate.since = number(counts["since"])
                s.gate.complete = counts["complete"] as? Bool ?? false
                s.gate.evaluated = integer(counts["evaluated"])
                s.gate.warned = integer(counts["warned"])
                s.gate.stopped = integer(counts["stopped"])
                s.gate.errors = integer(counts["errors"])
            }
            if let history = g["history"] as? [String: Any] {
                s.gate.historyFrom = number(history["from"])
                if let to = history["to"], !(to is NSNull) {
                    s.gate.historyTo = number(to)
                }
                s.gate.truncated = history["truncated"] as? Bool ?? false
                s.gate.events = (history["events"] as? [[String: Any]] ?? []).map { d in
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
            s.gate.pending = (g["pending_retry"] as? [[String: Any]] ?? []).map { d in
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
        }
        if let ov = j["overhead"] as? [String: Any] {
            s.idleSpares = ov["spares"] as? Int ?? 0
            s.idleSpareMem = ov["spare_mem"] as? Double ?? 0
        }
        return s
    }
}

// MARK: - building blocks

struct Card<Content: View>: View {
    var tint: Color = P.stroke
    @ViewBuilder var content: Content
    var body: some View {
        content
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 14).fill(P.card))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(tint, lineWidth: 1))
    }
}

struct Badge: View {
    var text: String
    var color: Color
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 9, weight: .heavy, design: .rounded))
            .tracking(0.4)
            .foregroundColor(color)
            .lineLimit(1).minimumScaleFactor(0.72)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .background(Capsule().fill(color.opacity(0.16)))
    }
}

struct Meter: View {
    var value: Double            // 0…1
    var tint: Color
    var height: CGFloat = 5
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.09))
                Capsule()
                    .fill(LinearGradient(colors: [tint.opacity(0.75), tint],
                                         startPoint: .leading, endPoint: .trailing))
                    .frame(width: max(2, g.size.width * min(max(value, 0), 1)))
            }
        }
        .frame(height: height)
    }
}

/// The big RAM / SWAP tiles.
struct StatTile: View {
    var icon: String, label: String
    var value: String, unit: String, caption: String
    var progress: Double, tint: Color
    var badge: String?
    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 5) {
                    Image(systemName: icon).font(.system(size: 10, weight: .bold))
                        .foregroundColor(tint)
                    Text(label.uppercased())
                        .font(.system(size: 9, weight: .heavy, design: .rounded))
                        .tracking(0.5).foregroundColor(P.dim)
                    Spacer(minLength: 4)
                    if let badge { Badge(text: badge, color: tint) }
                }
                HStack(alignment: .firstTextBaseline, spacing: 3) {
                    Text(value)
                        .font(.system(size: 27, weight: .bold, design: .rounded))
                        .foregroundColor(P.text)
                    Text(unit).font(.system(size: 11, weight: .semibold))
                        .foregroundColor(P.dim)
                }
                Meter(value: progress, tint: tint)
                Text(caption).font(.system(size: 9.5)).foregroundColor(P.faint)
                    .lineLimit(1)
            }
        }
    }
}

struct Chevron: View {
    var open: Bool
    var body: some View {
        Image(systemName: "chevron.right")
            .font(.system(size: 8, weight: .black))
            .foregroundColor(P.faint)
            .rotationEffect(.degrees(open ? 90 : 0))
    }
}

/// Collapsible section. Everything below the top two tiles lives in one of
/// these so a machine with 11 sessions is still readable in a 620pt popover.
struct Section<Content: View>: View {
    var title: String
    var count: Int
    var tint: Color = P.dim
    @Binding var open: Bool
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Button { withAnimation(.easeInOut(duration: 0.16)) { open.toggle() } } label: {
                HStack(spacing: 6) {
                    Chevron(open: open)
                    Text(title.uppercased())
                        .font(.system(size: 9, weight: .heavy, design: .rounded))
                        .tracking(0.7).foregroundColor(tint)
                    Spacer()
                    Text("\(count)")
                        .font(.system(size: 9, weight: .bold, design: .rounded))
                        .foregroundColor(tint.opacity(0.9))
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Capsule().fill(tint.opacity(0.15)))
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if open { content }
        }
    }
}

/// Without this nothing in the card explains itself — the dot colours and the
/// pink outline both had to be asked about.
struct Legend: View {
    private let items: [(Color, String)] = [
        (P.green, "completed"), (P.dim, "working"),
        (P.amber, "idle"), (P.blue, "terminal"),
    ]
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 9) {
                ForEach(items, id: \.1) { c, label in
                    HStack(spacing: 3) {
                        Circle().fill(c).frame(width: 5, height: 5)
                        Text(label).font(.system(size: 8.5)).foregroundColor(P.faint)
                    }
                }
            }
            HStack(spacing: 4) {
                RoundedRectangle(cornerRadius: 2)
                    .stroke(P.red.opacity(0.6), lineWidth: 1)
                    .frame(width: 12, height: 7)
                Text("outlined = over half its memory is swapped to disk")
                    .font(.system(size: 8.5)).foregroundColor(P.faint)
            }
        }
        .padding(.bottom, 1)
    }
}

struct DetailLine: View {
    var left: String, right: String
    var tint: Color = P.faint
    var body: some View {
        HStack(spacing: 6) {
            Text(left).font(.system(size: 9)).foregroundColor(tint).lineLimit(1)
            Spacer(minLength: 4)
            Text(right).font(.system(size: 9, design: .rounded))
                .foregroundColor(P.faint)
        }
    }
}

struct SessionCard: View {
    var s: Sess
    @Binding var expanded: Bool
    var onEnd: () -> Void
    @State private var hoverClose = false

    private var swapShare: Double { s.total > 0 ? s.swap / s.total : 0 }
    private var hot: Bool { swapShare > 0.5 }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 7) {
                Chevron(open: expanded)
                Circle().fill(P.stateColor(s.state)).frame(width: 6, height: 6)
                Text(s.name).font(.system(size: 11.5, weight: .semibold))
                    .foregroundColor(P.text).lineLimit(1)
                Spacer(minLength: 6)
                Text(human(s.total))
                    .font(.system(size: 11.5, weight: .bold, design: .rounded))
                    .foregroundColor(P.text)
                Button(action: onEnd) {
                    Image(systemName: "xmark")
                        .font(.system(size: 8, weight: .black))
                        .foregroundColor(hoverClose ? .white : P.faint)
                        .frame(width: 15, height: 15)
                        .background(Circle().fill(hoverClose ? P.red : P.cardHi))
                }
                .buttonStyle(.plain)
                .onHover { hoverClose = $0 }
                .help("End this session")
            }
            // RAM vs swap in one bar — the swapped share is what hurts.
            GeometryReader { g in
                HStack(spacing: 2) {
                    Capsule().fill(P.ram.opacity(0.9))
                        .frame(width: max(2, g.size.width * (1 - swapShare)))
                    Capsule().fill(hot ? P.red : P.swap.opacity(0.85))
                }
            }
            .frame(height: 4)
            HStack(spacing: 6) {
                Text("RAM \(human(s.ram))").font(.system(size: 9, weight: .medium))
                    .foregroundColor(P.ram)
                Text("SWAP \(human(s.swap))").font(.system(size: 9, weight: .medium))
                    .foregroundColor(hot ? P.red : P.swap)
                Text("· \(s.procs)p").font(.system(size: 9)).foregroundColor(P.faint)
                Spacer(minLength: 2)
                if s.subActive > 0 {
                    Badge(text: s.subActive == 1 ? "1 agent" : "\(s.subActive) agents", color: P.violet)
                }
            }
            if !s.doing.isEmpty {
                Text(s.doing).font(.system(size: 9.5)).foregroundColor(P.faint)
                    .lineLimit(expanded ? 2 : 1).truncationMode(.tail)
            }

            if expanded {
                // Only what a reader cannot infer from the collapsed row: the
                // work this session spawned, and who is running right now.
                let hasDetail = !s.children.isEmpty || !s.agents.isEmpty
                    || !s.started.isEmpty
                if hasDetail { Divider().overlay(P.stroke).padding(.vertical, 1) }

                if !s.children.isEmpty {
                    Text("SPAWNED").font(.system(size: 8, weight: .heavy))
                        .tracking(0.5).foregroundColor(P.faint)
                    ForEach(s.children) { c in
                        DetailLine(
                            left: c.worktree.isEmpty ? c.tag : "\(c.tag) · \(c.worktree)",
                            right: human(c.mem),
                            tint: P.dim)
                    }
                }
                if !s.agents.isEmpty {
                    Text("SUBAGENTS RUNNING").font(.system(size: 8, weight: .heavy))
                        .tracking(0.5).foregroundColor(P.faint).padding(.top, 2)
                    ForEach(s.agents) { a in
                        HStack(spacing: 5) {
                            Circle().fill(P.violet).frame(width: 4, height: 4)
                            Text(a.kind).font(.system(size: 9, weight: .medium))
                                .foregroundColor(P.violet).lineLimit(1)
                            Spacer(minLength: 2)
                        }
                    }
                }
                if !s.started.isEmpty {
                    HStack(spacing: 5) {
                        Text("STARTED").font(.system(size: 8, weight: .heavy))
                            .tracking(0.5).foregroundColor(P.faint)
                        Text(s.started.joined(separator: " · "))
                            .font(.system(size: 9)).foregroundColor(P.blue.opacity(0.85))
                    }
                    .padding(.top, 2)
                }
                if !hasDetail {
                    Text("no spawned work — the session process is all of it")
                        .font(.system(size: 9)).foregroundColor(P.faint)
                }
            }
        }
        .padding(.vertical, 8).padding(.horizontal, 11)
        .background(RoundedRectangle(cornerRadius: 12).fill(
            expanded ? P.cardHi : P.card))
        .overlay(RoundedRectangle(cornerRadius: 12)
            .stroke(hot ? P.red.opacity(0.40)
                    : (expanded ? P.strokeHi : P.stroke), lineWidth: 1))
        .contentShape(Rectangle())
        .onTapGesture { withAnimation(.easeInOut(duration: 0.16)) { expanded.toggle() } }
    }
}

/// Where the score sits between named tiers. An unlabelled progress bar told the
/// reader nothing — the zones have to carry their own names and boundaries.
struct LevelTrack: View {
    var score: Int
    var level: String

    // (name, first point in zone, width in points)
    private let zones: [(String, Int, Int)] = [
        ("HEALTHY", 0, 2), ("WATCH", 2, 2), ("DANGER", 4, 3), ("CRITICAL", 7, 2),
    ]
    private func color(_ name: String) -> Color { P.tint(name) }

    var body: some View {
        VStack(spacing: 3) {
            GeometryReader { g in
                let total = CGFloat(zones.reduce(0) { $0 + $1.2 })
                HStack(spacing: 2) {
                    ForEach(zones, id: \.0) { name, start, width in
                        let w = g.size.width * CGFloat(width) / total - 2
                        let filled = min(max(score - start, 0), width)
                        ZStack(alignment: .leading) {
                            Capsule().fill(color(name).opacity(0.16))
                            Capsule().fill(color(name))
                                .frame(width: w * CGFloat(filled) / CGFloat(width))
                        }
                        .frame(width: max(w, 2))
                    }
                }
            }
            .frame(height: 5)
            HStack(spacing: 2) {
                ForEach(zones, id: \.0) { name, _, width in
                    Text(name)
                        .font(.system(size: 7, weight: name == level ? .black : .medium,
                                      design: .rounded))
                        .foregroundColor(name == level ? color(name) : P.faint)
                        .frame(maxWidth: .infinity)
                }
            }
        }
    }
}

struct FooterButton: View {
    var icon: String, label: String
    var tint: Color = P.dim
    var action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: icon).font(.system(size: 10, weight: .bold))
                Text(label).font(.system(size: 10.5, weight: .medium))
            }
            .foregroundColor(hover ? P.text : tint)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(Capsule().fill(hover ? P.cardHi : P.card))
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
    }
}

struct DecisionRow: View {
    var level: String, result: String
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(level)
                .font(.system(size: 8.5, weight: .heavy, design: .rounded))
                .foregroundColor(P.tint(level)).frame(width: 66, alignment: .leading)
            Text(result).font(.system(size: 9.5)).foregroundColor(P.dim)
            Spacer(minLength: 0)
        }
    }
}

struct GateEventCard: View {
    var event: GateEvent
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
                Text(stopped ? "■" : "▲")
                    .font(.system(size: 8, weight: .black)).foregroundColor(tint)
                Text(stopped ? "STOPPED · COMMAND DID NOT RUN" : "WARNED · COMMAND RAN")
                    .font(.system(size: 8.5, weight: .heavy, design: .rounded))
                    .tracking(0.25).foregroundColor(tint)
                Spacer(minLength: 4)
                Text(eventTime(event.ts)).font(.system(size: 8.5)).foregroundColor(P.faint)
            }

            Text(event.commandDisplay)
                .font(.system(size: 9.5, design: .monospaced))
                .foregroundColor(P.text)
                .lineLimit(expanded ? nil : 2)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)

            if expanded {
                eventDetail("Session", sessionLabel)
                eventDetail("Command match", fullMatchLabel)

                VStack(alignment: .leading, spacing: 2) {
                    Text("Memory at \(eventClock(event.ts))")
                        .font(.system(size: 8, weight: .heavy)).foregroundColor(P.faint)
                    Text(event.level)
                        .font(.system(size: 9.5, weight: .heavy, design: .rounded))
                        .foregroundColor(P.tint(event.level))
                    ForEach(Array(event.reasons.enumerated()), id: \.offset) { _, reason in
                        Text(reason).font(.system(size: 9.5)).foregroundColor(P.dim)
                    }
                }
                eventDetail("Outcome", outcome)
            } else {
                Text("Session \(sessionLabel) · \(relative(event.ts))")
                    .font(.system(size: 8.5)).foregroundColor(P.faint).lineLimit(1)
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Text("\(matchLabel) + \(event.level) memory → \(stopped ? "stopped" : "warned")")
                        .font(.system(size: 8.5)).foregroundColor(P.dim)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 2)
                    Chevron(open: false)
                }
                if stopped && event.retryStatus == "waiting" {
                    Text("WAITING TO RETRY")
                        .font(.system(size: 8, weight: .heavy, design: .rounded))
                        .foregroundColor(P.red)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Capsule().fill(P.red.opacity(0.14)))
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(P.card))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(tint.opacity(0.62), lineWidth: 1))
        .contentShape(Rectangle())
        .onTapGesture { withAnimation(.easeInOut(duration: 0.16)) { expanded.toggle() } }
    }

    private func eventDetail(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.system(size: 8, weight: .heavy)).foregroundColor(P.faint)
            Text(value).font(.system(size: 9.5)).foregroundColor(P.dim)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

struct MissingGateEventCard: View {
    var item: PendingRetry
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 5) {
                Text("■").font(.system(size: 8, weight: .black)).foregroundColor(P.red)
                Text("STOPPED · COMMAND DID NOT RUN")
                    .font(.system(size: 8.5, weight: .heavy, design: .rounded))
                    .foregroundColor(P.red)
                Spacer()
                Text(eventTime(item.ts)).font(.system(size: 8.5)).foregroundColor(P.faint)
            }
            Text(item.commandDisplay)
                .font(.system(size: 9.5, design: .monospaced)).foregroundColor(P.text)
                .lineLimit(2).textSelection(.enabled)
            Text("Session \(item.sessionName ?? item.sessionID) · \(relative(item.ts))")
                .font(.system(size: 8.5)).foregroundColor(P.faint)
            Text("Stopped earlier · event details are no longer retained")
                .font(.system(size: 8.5)).foregroundColor(P.dim)
            Text("WAITING TO RETRY")
                .font(.system(size: 8, weight: .heavy, design: .rounded))
                .foregroundColor(P.red)
        }
        .padding(10).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(P.card))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(P.red.opacity(0.62), lineWidth: 1))
    }
}

// MARK: - main view

struct ContentView: View {
    @ObservedObject var model: Model
    var onQuit: () -> Void
    var onReap: () -> Void
    var onEndSession: (Sess) -> Void = { _ in }
    var onToggleGate: (Bool) -> Void = { _ in }
    /// ImageRenderer cannot lay out a ScrollView offscreen — it renders empty.
    /// The preview drops the scroll container so the real content is exercised.
    var flattened = false
    /// Preview only: force every session open so the drill-down layout is
    /// exercised without needing to click.
    var previewExpandAll = false
    var previewOpenGate = false
    var frameHeight: CGFloat = 620

    @State var openSessions = true
    @State var openWorktrees = false
    @State var openPool = false
    @State var openApps = false
    @State var openGate = true
    // Reference material, not status: you read the rule list once and then know
    // it. Left open it re-imposed ~150pt above the RAM/Swap tiles on every
    // popover open, which is the opposite of what those tiles are for.
    @State var openMatchRules = false
    @State var showAllWarnings = false
    @State var showAllStops = false
    @State var expandedSessions: Set<String> = []

    private var s: Snap { model.snap }
    private var tint: Color { P.tint(s.level) }
    private var finished: [Sess] {
        s.sessions.filter { $0.state == "done" || $0.state == "stopped" }
    }
    private var finishedCount: Int { finished.count }
    private var finishedMem: Double { finished.reduce(0) { $0 + $1.total } }
    private var sessionTotal: Double { s.sessions.reduce(0) { $0 + $1.total } }

    var body: some View {
        ZStack {
            LinearGradient(colors: [P.bgTop, P.bgBot],
                           startPoint: .top, endPoint: .bottom)
            VStack(spacing: 0) {
                header
                Divider().overlay(P.stroke)
                if model.loaded {
                    if flattened { body_; Spacer(minLength: 0) }
                    else { ScrollView { body_ } }
                } else {
                    VStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("reading memory…").font(.system(size: 11))
                            .foregroundColor(P.dim)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
                Divider().overlay(P.stroke)
                footer
            }
        }
        .frame(width: 380, height: frameHeight, alignment: .top)
    }

    private var header: some View {
        HStack(spacing: 10) {
            ZStack {
                Circle().fill(LinearGradient(colors: [P.violet, P.blue],
                                             startPoint: .topLeading,
                                             endPoint: .bottomTrailing))
                Image(systemName: "memorychip.fill")
                    .font(.system(size: 14, weight: .bold)).foregroundColor(.white)
            }
            .frame(width: 30, height: 30)
            VStack(alignment: .leading, spacing: 1) {
                Text("memmon").font(.system(size: 14, weight: .bold))
                    .foregroundColor(P.text)
                Text("Memory & command protection").font(.system(size: 9.5))
                    .foregroundColor(P.faint)
            }
            Spacer()
            HStack(spacing: 5) {
                Circle().fill(tint).frame(width: 7, height: 7)
                Text(model.refreshing ? "Syncing…" : s.level)
                    .font(.system(size: 10, weight: .bold, design: .rounded))
                    .foregroundColor(tint)
            }
            .padding(.horizontal, 9).padding(.vertical, 5)
            .background(Capsule().fill(tint.opacity(0.14)))
            .overlay(Capsule().stroke(tint.opacity(0.35), lineWidth: 1))
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
    }

    private var body_: some View {
        VStack(alignment: .leading, spacing: 11) {
            verdict
            gateSection
            HStack(spacing: 10) {
                StatTile(icon: "cpu.fill", label: "RAM",
                         value: String(format: "%.0f", s.ramUsed / s.ramTotal * 100),
                         unit: "% used",
                         caption: "\(human(s.ramUsed)) of \(human(s.ramTotal)) · \(human(s.compressed)) compressed",
                         progress: s.ramUsed / s.ramTotal, tint: P.ram)
                StatTile(icon: "internaldrive.fill", label: "Swap",
                         value: human(s.swapUsed), unit: "on disk",
                         caption: String(format: "%.2fx RAM size · load %.1f",
                                         s.swapUsed / s.ramTotal, s.load),
                         progress: min(s.swapUsed / s.ramTotal, 1),
                         tint: s.swapUsed > s.ramTotal ? P.red : P.swap,
                         badge: s.swapUsed > s.ramTotal ? "over" : nil)
            }
            if s.orphanTotal > 0 { orphanCard }

            if !s.sessions.isEmpty {
                Section(title: "Claude sessions", count: s.sessions.count,
                        open: $openSessions) {
                    VStack(spacing: 7) {
                        Legend()
                        if finishedMem > 0 {
                            // Finished sessions are free memory: the work is done,
                            // closing one costs nothing.
                            Text("\(finishedCount) completed session"
                                 + (finishedCount == 1 ? "" : "s")
                                 + " still holding \(human(finishedMem)) — safe to close")
                                .font(.system(size: 9)).foregroundColor(P.green)
                        }
                        ForEach(s.sessions) { sess in
                            SessionCard(
                                s: sess,
                                expanded: Binding(
                                    get: { previewExpandAll
                                           || expandedSessions.contains(sess.name) },
                                    set: { on in
                                        if on { expandedSessions.insert(sess.name) }
                                        else { expandedSessions.remove(sess.name) }
                                    }),
                                onEnd: { onEndSession(sess) })
                        }
                    }
                }
            }
            if !s.worktrees.isEmpty {
                Section(title: "Work by worktree", count: s.worktrees.count,
                        open: $openWorktrees) {
                    VStack(spacing: 6) { ForEach(s.worktrees) { worktreeRow($0) } }
                }
            }
            if !s.apps.isEmpty {
                Section(title: "Other apps", count: s.apps.count,
                        open: $openApps) {
                    VStack(spacing: 6) {
                        ForEach(s.apps.prefix(6)) { a in
                            HStack(spacing: 8) {
                                Circle().fill(P.faint).frame(width: 5, height: 5)
                                Text(a.name).font(.system(size: 10.5, weight: .medium))
                                    .foregroundColor(P.text).lineLimit(1)
                                Text("\(a.procs)p").font(.system(size: 8.5))
                                    .foregroundColor(P.faint)
                                Spacer(minLength: 4)
                                Text(human(a.mem))
                                    .font(.system(size: 11, weight: .bold,
                                                  design: .rounded))
                                    .foregroundColor(a.mem > sessionTotal
                                                     ? P.amber : P.text)
                            }
                            .padding(.vertical, 5).padding(.horizontal, 11)
                            .background(RoundedRectangle(cornerRadius: 10).fill(P.card))
                        }
                        if let biggest = s.apps.first, biggest.mem > sessionTotal {
                            Text("\(biggest.name) alone outweighs every Claude "
                                 + "session combined (\(human(sessionTotal)))")
                                .font(.system(size: 9)).foregroundColor(P.amber)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            if s.idleSpares > 0 {
                Section(title: "Claude runtime pool", count: s.idleSpares,
                        open: $openPool) {
                    Text("\(s.idleSpares) idle prewarm process(es) holding "
                         + "\(human(s.idleSpareMem)). Claimed sessions are counted "
                         + "above and are never reclaimable.")
                        .font(.system(size: 9.5)).foregroundColor(P.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
    }

    private var verdict: some View {
        Card(tint: tint.opacity(0.40)) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Image(systemName: s.level == "HEALTHY"
                          ? "checkmark.shield.fill" : "exclamationmark.triangle.fill")
                        .font(.system(size: 11, weight: .bold)).foregroundColor(tint)
                    Text("MEMORY NOW · \(s.level)")
                        .font(.system(size: 12, weight: .heavy, design: .rounded))
                        .foregroundColor(tint)
                        .lineLimit(1).minimumScaleFactor(0.75)
                    Spacer()
                    // Only once something is actually wrong. A red "~19 min left"
                    // sitting on a green HEALTHY card contradicts itself, and the
                    // projection is too noisy to be worth alarming about while
                    // the machine is fine.
                    if let h = s.headroom, h < 120, s.level != "HEALTHY" {
                        Badge(text: String(format: "~%.0f min to low headroom", h),
                              color: P.red)
                    }
                }
                LevelTrack(score: s.score, level: s.level)
                // Names the actual offender rather than repeating canned advice.
                Text(s.advice).font(.system(size: 10)).foregroundColor(P.dim)
                    .fixedSize(horizontal: false, vertical: true)
                if !s.reasons.isEmpty {
                    Text("Memory signals now: " + s.reasons.joined(separator: " · "))
                        .font(.system(size: 9)).foregroundColor(P.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }


    private var recentWarnings: [GateEvent] {
        s.gate.events.filter { $0.action == "warn" }.sorted { $0.ts > $1.ts }
    }
    // Stops split by whether they still represent unfinished work. A command
    // that never ran and is still waiting is the one thing here nobody should
    // have to expand a disclosure to find, so those are always listed in full.
    // Everything else is history and is capped like the warnings are — an
    // uncapped list grew with the log and pushed the rest of the popover down
    // for no benefit, since an already-retried stop is not actionable.
    private var pendingStops: [GateEvent] {
        s.gate.events.filter { $0.action == "block" && $0.retryStatus == "waiting" }
            .sorted { $0.ts > $1.ts }
    }
    private var resolvedStops: [GateEvent] {
        s.gate.events.filter { $0.action == "block" && $0.retryStatus != "waiting" }
            .sorted { $0.ts > $1.ts }
    }

    private var policyCopy: String {
        if s.gate.paused {
            return "Command protection is paused. Every command runs without a memory check. History below is unchanged."
        }
        switch s.gate.mode {
        case "block":
            return "Current policy: WATCH warns; DANGER or CRITICAL stops before running."
        case "warn":
            return "Current policy: WATCH, DANGER, or CRITICAL warns; commands are never stopped."
        default:
            return "Current policy: WATCH or DANGER warns; CRITICAL stops before running."
        }
    }

    private func policyResult(_ level: String) -> String {
        if s.gate.paused { return "runs without a memory check" }
        if level == "HEALTHY" { return "runs silently" }
        if s.gate.mode == "warn" { return "warned; command ran" }
        if s.gate.mode == "block" && (level == "DANGER" || level == "CRITICAL") {
            return "stopped before running"
        }
        if s.gate.mode == "block-critical" && level == "CRITICAL" {
            return "stopped before running"
        }
        return "warned; command ran"
    }

    private var retryCopy: String {
        if s.gate.paused {
            return "Protection is paused; retrying now will run without a memory check."
        }
        if s.level == "HEALTHY" {
            return "Memory is HEALTHY now — waiting commands can be retried."
        }
        let stops = (s.level == "CRITICAL" && s.gate.mode != "warn")
            || (s.level == "DANGER" && s.gate.mode == "block")
        return stops
            ? "If memory stays \(s.level), a retry will be stopped again."
            : "If memory stays \(s.level), a retry will be warned and will run."
    }

    private var gateSection: some View {
        let g = s.gate
        let isOpen = previewOpenGate || openGate
        return VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 5) {
                Button { withAnimation(.easeInOut(duration: 0.16)) { openGate.toggle() } } label: {
                    HStack(spacing: 5) {
                        Chevron(open: isOpen)
                        Text("COMMAND WARNINGS & STOPS")
                            .font(.system(size: 8.2, weight: .heavy, design: .rounded))
                            .tracking(0.45).foregroundColor(P.dim)
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain)
                Spacer(minLength: 3)
                if g.installed {
                    Circle().fill(g.paused ? P.amber : P.green).frame(width: 6, height: 6)
                    Text(g.paused ? "PAUSED" : "ACTIVE")
                        .font(.system(size: 8.5, weight: .heavy, design: .rounded))
                        .foregroundColor(g.paused ? P.amber : P.green)
                    FooterButton(icon: g.paused ? "play.fill" : "pause.fill",
                                 label: g.paused ? "Resume" : "Pause",
                                 tint: g.paused ? P.green : P.dim) {
                        onToggleGate(!g.paused)
                    }
                    .help(g.paused ? "Resume command warnings and stops"
                                   : "Pause command warnings and stops")
                }
            }

            if !isOpen {
                if !g.installed {
                    Text("Command protection is not installed")
                        .font(.system(size: 9)).foregroundColor(P.faint)
                } else if g.paused {
                    Text("Every command currently runs without a memory check")
                        .font(.system(size: 9)).foregroundColor(P.faint)
                } else {
                    Text("Retained since \(retainedDate(g.since)) · \(g.warned) warned and ran · \(g.stopped) stopped")
                        .font(.system(size: 9)).foregroundColor(P.faint)
                    if !g.complete {
                        Text("Older activity may be missing")
                            .font(.system(size: 8.5)).foregroundColor(P.faint)
                    }
                }
            } else if !g.installed {
                Text("Command protection is not installed. Memory monitoring is active; commands are never warned or stopped.")
                    .font(.system(size: 9.5)).foregroundColor(P.dim)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(alignment: .leading, spacing: 9) {
                    if g.paused {
                        Text(policyCopy).font(.system(size: 9.5)).foregroundColor(P.amber)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        Text("Only commands that match a memory-intensive rule are checked.")
                            .font(.system(size: 9.5)).foregroundColor(P.dim)
                        Text(policyCopy).font(.system(size: 9.5, weight: .medium))
                            .foregroundColor(P.text).fixedSize(horizontal: false, vertical: true)
                    }

                    Text("MATCHED COMMAND + MEMORY THEN → RESULT")
                        .font(.system(size: 8, weight: .heavy, design: .rounded))
                        .tracking(0.45).foregroundColor(P.faint)
                    VStack(spacing: 3) {
                        DecisionRow(level: "HEALTHY", result: policyResult("HEALTHY"))
                        DecisionRow(level: "WATCH", result: policyResult("WATCH"))
                        DecisionRow(level: "DANGER", result: policyResult("DANGER"))
                        DecisionRow(level: "CRITICAL", result: policyResult("CRITICAL"))
                    }

                    Button { withAnimation(.easeInOut(duration: 0.16)) {
                        openMatchRules.toggle()
                    }} label: {
                        HStack {
                            Text("WHAT COMMANDS MATCH?")
                                .font(.system(size: 8, weight: .heavy, design: .rounded))
                                .tracking(0.45).foregroundColor(P.faint)
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
                        .font(.system(size: 9)).foregroundColor(P.dim)
                        .fixedSize(horizontal: false, vertical: true)
                    }

                    if g.errors > 0 {
                        Text("Command protection failed open \(g.errors) times. Those commands ran.")
                            .font(.system(size: 9.5)).foregroundColor(P.amber)
                    }

                    if g.warned == 0 && g.stopped == 0 && g.pending.isEmpty {
                        Text("No warnings or stops since \(retainedDate(g.since, includeTime: true)).")
                            .font(.system(size: 9.5, weight: .medium)).foregroundColor(P.green)
                        Text("Matched commands run silently while memory is HEALTHY.\nAt WATCH or DANGER they run with a warning.\nAt CRITICAL they are stopped before running.")
                            .font(.system(size: 9)).foregroundColor(P.dim)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        stoppedHistory
                        warningHistory
                    }

                    if g.evaluated > 0 {
                        Text("Retained activity since \(retainedDate(g.since, includeTime: true)).")
                            .font(.system(size: 8.5)).foregroundColor(P.faint)
                        if !g.complete {
                            Text("Older activity may be missing.")
                                .font(.system(size: 8.5)).foregroundColor(P.faint)
                        }
                        if let to = g.historyTo {
                            Text("Event details retained from \(retainedDate(g.historyFrom, includeTime: true)) to \(retainedDate(to, includeTime: true)).")
                                .font(.system(size: 8.5)).foregroundColor(P.faint)
                        }
                    }
                }
            }
        }
    }

    private var stoppedHistory: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("STOPPED BEFORE RUNNING")
                    .font(.system(size: 8, weight: .heavy, design: .rounded))
                    .tracking(0.45).foregroundColor(P.red)
                Spacer()
                Text("\(s.gate.stopped)").font(.system(size: 9, weight: .bold))
                    .foregroundColor(P.red)
            }
            if !s.gate.pending.isEmpty {
                Text("\(s.gate.pending.count) waiting to retry")
                    .font(.system(size: 9.5, weight: .medium)).foregroundColor(P.red)
                Text(retryCopy).font(.system(size: 9)).foregroundColor(
                    s.level == "HEALTHY" || s.gate.paused ? P.green : P.amber)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // Unfinished work first, never truncated.
            ForEach(pendingStops) { GateEventCard(event: $0) }
            ForEach(s.gate.pending.filter { !$0.eventRetained }) {
                MissingGateEventCard(item: $0)
            }
            if showAllStops {
                LazyVStack(spacing: 7) {
                    ForEach(resolvedStops) { GateEventCard(event: $0) }
                }
            } else {
                ForEach(Array(resolvedStops.prefix(3))) { GateEventCard(event: $0) }
            }
            if resolvedStops.count > 3 {
                Button(showAllStops
                       ? "Show only 3 recent stops"
                       : "Show all \(resolvedStops.count) earlier stops") {
                    withAnimation(.easeInOut(duration: 0.16)) { showAllStops.toggle() }
                }
                .buttonStyle(.plain)
                .font(.system(size: 9.5, weight: .semibold))
                .foregroundColor(P.red)
            }
            if pendingStops.isEmpty && resolvedStops.isEmpty && s.gate.pending.isEmpty {
                Text("No commands have been stopped since \(retainedDate(s.gate.since)).")
                    .font(.system(size: 9)).foregroundColor(P.faint)
            }
        }
    }

    private var warningHistory: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("WARNED — COMMAND RAN")
                    .font(.system(size: 8, weight: .heavy, design: .rounded))
                    .tracking(0.45).foregroundColor(P.amber)
                Spacer()
                Text("\(s.gate.warned)").font(.system(size: 9, weight: .bold))
                    .foregroundColor(P.amber)
            }
            if showAllWarnings {
                LazyVStack(spacing: 7) {
                    ForEach(recentWarnings) { GateEventCard(event: $0) }
                }
            } else {
                ForEach(Array(recentWarnings.prefix(3))) { GateEventCard(event: $0) }
            }
            if recentWarnings.count > 3 {
                Button(showAllWarnings
                       ? "Show only 3 recent warnings"
                       : "Show all \(s.gate.warned) retained warnings") {
                    withAnimation(.easeInOut(duration: 0.16)) { showAllWarnings.toggle() }
                }
                .buttonStyle(.plain)
                .font(.system(size: 9.5, weight: .semibold))
                .foregroundColor(P.amber)
            }
        }
    }

    private var orphanCard: some View {
        Card(tint: P.red.opacity(0.45)) {
            HStack(spacing: 8) {
                Image(systemName: "trash.fill").font(.system(size: 11, weight: .bold))
                    .foregroundColor(P.red)
                VStack(alignment: .leading, spacing: 1) {
                    Text("\(human(s.orphanTotal)) reclaimable")
                        .font(.system(size: 11.5, weight: .bold)).foregroundColor(P.text)
                    Text("\(s.orphanCount) orphaned build process\(s.orphanCount == 1 ? "" : "es")")
                        .font(.system(size: 9)).foregroundColor(P.faint)
                }
                Spacer()
                FooterButton(icon: "bolt.fill", label: "Reap", tint: P.red,
                             action: onReap)
            }
        }
    }

    private func worktreeRow(_ wt: WT) -> some View {
        let hot = wt.mem > 6 * GB
        return HStack(spacing: 8) {
            Circle().fill(hot ? P.red : P.violet).frame(width: 5, height: 5)
            VStack(alignment: .leading, spacing: 2) {
                Text(wt.name).font(.system(size: 10.5, weight: .medium))
                    .foregroundColor(P.text).lineLimit(1)
                Text("\(wt.tag) · \(wt.procs)p"
                     + (wt.orphans > 0 ? " · \(wt.orphans) orphaned" : ""))
                    .font(.system(size: 8.5)).foregroundColor(P.faint)
            }
            Spacer(minLength: 4)
            Text(human(wt.mem))
                .font(.system(size: 11, weight: .bold, design: .rounded))
                .foregroundColor(hot ? P.red : P.text)
        }
        .padding(.vertical, 6).padding(.horizontal, 11)
        .background(RoundedRectangle(cornerRadius: 10).fill(P.card))
    }

    private var footer: some View {
        HStack(spacing: 7) {
            FooterButton(icon: "arrow.clockwise", label: "Sync") { model.refresh() }
            Spacer()
            if let t = model.lastSync {
                Text(relative(t.timeIntervalSince1970))
                    .font(.system(size: 9)).foregroundColor(P.faint)
            }
            FooterButton(icon: "power", label: "Quit", action: onQuit)
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
    }
}

// MARK: - app

final class Controller: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let popover = NSPopover()
    let model = Model()
    let cache = NSString(string: "~/.claude/memmon/latest.json").expandingTildeInPath

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)

        popover.contentSize = NSSize(width: 380, height: 620)
        popover.behavior = .transient
        popover.animates = true
        popover.delegate = self
        popover.contentViewController = NSHostingController(
            rootView: ContentView(model: model,
                                  onQuit: { NSApp.terminate(nil) },
                                  onReap: { [weak self] in self?.reap() },
                                  onEndSession: { [weak self] s in
                                      self?.endSession(s) },
                                  onToggleGate: { [weak self] pause in
                                      self?.toggleGate(pause) }))

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
        }
    }

    func popoverDidClose(_ note: Notification) { updateTitleFromCache() }

    /// Colour comes from the pressure model, not from swap usage: macOS grows
    /// swap on demand, so a large swapfile on its own means nothing.
    func updateTitleFromCache() {
        guard let d = FileManager.default.contents(atPath: cache),
              let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any],
              let used = j["swap_used"] as? Double else { return }
        let level = j["pressure"] as? String ?? "HEALTHY"
        let dot: String
        switch level {
        case "CRITICAL", "DANGER": dot = "🔴"
        case "WATCH": dot = "🟠"
        default: dot = "🟢"
        }
        statusItem.button?.attributedTitle = NSAttributedString(
            string: "\(dot) \(human(used))",
            attributes: [.font: NSFont.monospacedDigitSystemFont(ofSize: 12,
                                                                 weight: .regular)])
    }

    /// Ending a session destroys unsaved work in it, so the confirmation states
    /// exactly what is being killed and is not the default button.
    func endSession(_ s: Sess) {
        let a = NSAlert()
        a.alertStyle = .warning
        a.messageText = "End “\(s.name)”?"
        a.informativeText = """
            This terminates the session and all \(s.procs) of its processes \
            (\(human(s.total))), including any build it started.

            Anything it has not written to disk is lost. It is sent SIGTERM, so \
            it will try to flush its transcript first.
            """
        a.addButton(withTitle: "Cancel")
        a.addButton(withTitle: "End session")
        NSApp.activate(ignoringOtherApps: true)
        guard a.runModal() == .alertSecondButtonReturn else { return }
        model.run(["--end-session", "\(s.root)", "--apply"])
        model.refresh()
    }

    /// No confirmation: pausing destroys nothing and is trivially reversible,
    /// unlike reap and endSession.
    func toggleGate(_ pause: Bool) {
        model.run([pause ? "--off" : "--on"])
        model.refresh()
    }

    func reap() {
        let a = NSAlert()
        a.messageText = "Reap orphaned build processes?"
        a.informativeText = "Kills orphaned or stale build processes only. "
            + "Claimed Claude sessions are never touched."
        a.addButton(withTitle: "Reap")
        a.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard a.runModal() == .alertFirstButtonReturn else { return }
        model.run(["--reap", "--apply"])
        model.refresh()
    }

}

// MARK: - offscreen render
//
// `MemmonBar --render out.png` draws the popover with representative data and
// exits. The UI is otherwise unreviewable without screen-recording permission,
// and a layout that only breaks under DANGER with blocked commands is exactly
// the state you cannot reproduce on demand.

let PREVIEW_PAUSED = CommandLine.arguments.contains("--paused")
let PREVIEW_EMPTY = CommandLine.arguments.contains("--empty")
let PREVIEW_NOT_INSTALLED = CommandLine.arguments.contains("--not-installed")

@MainActor
func renderPreview(to path: String) {
    let m = Model()
    var s = Snap()
    s.ramUsed = 15.1 * GB; s.ramTotal = 16 * GB
    s.swapUsed = 19.4 * GB; s.swapTotal = 21 * GB
    s.compressed = 6.6 * GB; s.free = 18; s.load = 24.1
    s.level = "DANGER"
    s.reasons = ["swap 1.2x RAM size", "heavy thrashing 180 MB/s", "load 24 on 8 cores"]
    s.headroom = 6
    s.score = 6
    s.nextLevel = "CRITICAL"; s.toNext = 1
    s.advice = "web-checkout is running tsc typecheck (21.7G across 12 "
             + "processes). Let it finish before starting another build."
    s.sessions = [
        Sess(name: "api error handling", state: "working",
             doing: "[agent] infra + flake audit",
             total: 6.8 * GB, ram: 2.9 * GB, swap: 3.9 * GB, procs: 20,
             subActive: 9, root: 52963,
             children: [
                Child(tag: "tsc typecheck", worktree: "web-checkout",
                      mem: 3.5 * GB, pid: 99036, age: 1800),
                Child(tag: "vitest", worktree: "", mem: 331 * MB, pid: 77908, age: 540),
             ],
             agents: [
                Agent(kind: "infra-auditor", goal: "infra audit",
                      active: true),
                Agent(kind: "schema-auditor", goal: "schema audit",
                      active: true),
                Agent(kind: "Explore", goal: "adjacent callers", active: false),
             ],
             started: ["typecheck", "tests"]),
        Sess(name: "docs sweep", state: "blocked",
             doing: "waiting on the codegen step",
             total: 1.4 * GB, ram: 1.0 * GB, swap: 0.4 * GB, procs: 7,
             subActive: 0, root: 57020),
        Sess(name: "checkout redesign", state: "done",
             doing: "audit complete; 2 checks running",
             total: 653 * MB, ram: 352 * MB, swap: 301 * MB, procs: 4,
             subActive: 2, root: 17525),
        Sess(name: "pr #412 review", state: "terminal",
             doing: "reviewing the checkout diff",
             total: 505 * MB, ram: 240 * MB, swap: 265 * MB, procs: 4,
             subActive: 0, root: 57588),
    ]
    s.worktrees = [
        WT(name: "web-checkout", tag: "tsc typecheck",
           mem: 21.7 * GB, ram: 9.1 * GB, swap: 12.6 * GB, procs: 12, orphans: 2),
        WT(name: "web-search", tag: "tsc typecheck",
           mem: 12.0 * GB, ram: 5.2 * GB, swap: 6.8 * GB, procs: 7, orphans: 0),
    ]
    s.orphanTotal = 3.6 * GB; s.orphanCount = 2
    s.idleSpares = 2; s.idleSpareMem = 186 * MB
    let now = Date().timeIntervalSince1970
    let builtin = GateClassification(source: "builtin", rule: "pnpm … typecheck",
                                     shape: "pnpm typecheck", samples: nil,
                                     observedPeak: nil, blockEligible: true)
    let learned = GateClassification(source: "learned", rule: "codex.sh run",
                                     shape: "codex.sh run", samples: 13,
                                     observedPeak: 3.4 * GB, blockEligible: false)
    let events = [
        GateEvent(ts: now - 3800, action: "block", mode: "block-critical",
                  sessionID: "829922b2", sessionName: nil,
                  commandRaw: "/usr/bin/python3 ~/.claude/skills/linear/scripts/linear.py issue-get ABC-4573 --json",
                  commandDisplay: "python3 …/linear.py issue-get ABC-4573 --json",
                  classification: nil, legacy: true, level: "CRITICAL", score: 7,
                  reasons: ["swap 31% of RAM size", "heavy thrashing 289 MB/s",
                            "swap growing 5094 MB/min"],
                  retryStatus: "waiting", ms: 74),
        GateEvent(ts: now - 7200, action: "block", mode: "block-critical",
                  sessionID: "4dcb56b8", sessionName: "docs sweep",
                  commandRaw: "pnpm --filter dashboard typecheck",
                  commandDisplay: "pnpm --filter dashboard typecheck",
                  classification: builtin, legacy: false, level: "CRITICAL", score: 8,
                  reasons: ["heavy thrashing 210 MB/s", "load 29 on 8 cores"],
                  retryStatus: "waiting", ms: 78),
        GateEvent(ts: now - 11000, action: "block", mode: "block-critical",
                  sessionID: "73082786", sessionName: "checkout redesign",
                  commandRaw: "docker compose build", commandDisplay: "docker compose build",
                  classification: GateClassification(source: "builtin", rule: "docker compose build",
                      shape: "docker compose build", samples: nil, observedPeak: nil,
                      blockEligible: true), legacy: false, level: "CRITICAL", score: 7,
                  reasons: ["swap growing 1330 MB/min"], retryStatus: "not_waiting", ms: 71),
        // Four resolved stops so the preview exercises the overflow control;
        // with three or fewer it never renders and the path goes unreviewed.
        GateEvent(ts: now - 96000, action: "block", mode: "block-critical",
                  sessionID: "8ed32708", sessionName: "search indexing",
                  commandRaw: "npx vitest run --coverage",
                  commandDisplay: "npx vitest run --coverage",
                  classification: GateClassification(source: "builtin", rule: "vitest",
                      shape: "vitest run", samples: nil, observedPeak: nil,
                      blockEligible: true), legacy: false, level: "CRITICAL", score: 7,
                  reasons: ["swap 39% of RAM size", "paging 138 MB/s"],
                  retryStatus: "not_waiting", ms: 69),
        GateEvent(ts: now - 180000, action: "block", mode: "block-critical",
                  sessionID: "dd187cea", sessionName: "checkout redesign",
                  commandRaw: "pnpm install --frozen-lockfile",
                  commandDisplay: "pnpm install --frozen-lockfile",
                  classification: GateClassification(source: "builtin", rule: "pnpm … install",
                      shape: "pnpm install", samples: nil, observedPeak: nil,
                      blockEligible: true), legacy: false, level: "CRITICAL", score: 9,
                  reasons: ["heavy thrashing 301 MB/s", "load 44 on 8 cores"],
                  retryStatus: "not_waiting", ms: 81),
        GateEvent(ts: now - 250000, action: "block", mode: "block-critical",
                  sessionID: "70c9a8bb", sessionName: nil,
                  commandRaw: "turbo run build", commandDisplay: "turbo run build",
                  classification: GateClassification(source: "builtin", rule: "turbo … build",
                      shape: "turbo build", samples: nil, observedPeak: nil,
                      blockEligible: true), legacy: false, level: "CRITICAL", score: 7,
                  reasons: ["swap 44% of RAM size", "paging 85 MB/s"],
                  retryStatus: "not_waiting", ms: 66),
        GateEvent(ts: now - 900, action: "warn", mode: "block-critical",
                  sessionID: "8d63269a", sessionName: "api error handling",
                  commandRaw: "~/.claude/skills/codex/scripts/codex.sh run ABC-4573",
                  commandDisplay: "codex.sh run ABC-4573", classification: learned,
                  legacy: false, level: "DANGER", score: 5,
                  reasons: ["paging 73 MB/s", "swap growing 1947 MB/min"],
                  retryStatus: "not_waiting", ms: 76),
        GateEvent(ts: now - 3 * 86400, action: "warn", mode: "block-critical",
                  sessionID: "73082786", sessionName: nil,
                  commandRaw: "cat vitest.config.ts", commandDisplay: "cat vitest.config.ts",
                  classification: nil, legacy: true, level: "WATCH", score: 3,
                  reasons: ["swap growing 382 MB/min", "load 23"],
                  retryStatus: "not_waiting", ms: 72),
        GateEvent(ts: now - 2 * 86400, action: "warn", mode: "block-critical",
                  sessionID: "4dcb56b8", sessionName: "docs sweep",
                  commandRaw: "pnpm --filter dashboard typecheck",
                  commandDisplay: "pnpm --filter dashboard typecheck",
                  classification: builtin, legacy: false, level: "WATCH", score: 2,
                  reasons: ["load 24 on 8 cores"], retryStatus: "not_waiting", ms: 73),
        GateEvent(ts: now - 3600, action: "warn", mode: "block-critical",
                  sessionID: "a12e419f", sessionName: "search indexing",
                  commandRaw: "npx vitest run", commandDisplay: "npx vitest run",
                  classification: GateClassification(source: "builtin", rule: "vitest",
                      shape: "npx vitest", samples: nil, observedPeak: nil,
                      blockEligible: true), legacy: false, level: "DANGER", score: 4,
                  reasons: ["paging 56 MB/s"], retryStatus: "not_waiting", ms: 70),
    ]
    s.gate = GateStats(installed: true, paused: PREVIEW_PAUSED,
                       pausedUntil: PREVIEW_PAUSED ? now + 7200 : nil,
                       mode: "block-critical", since: now - 3 * 86400,
                       historyFrom: now - 3 * 86400, historyTo: now - 900,
                       complete: false, truncated: true, evaluated: 525,
                       // Must equal the number of block events below, or the
                       // preview shows a header count contradicting its own list.
                       warned: 54, stopped: 6, errors: 0, events: events,
                       pending: [
                        PendingRetry(ts: now - 3800, sessionID: "829922b2",
                                     sessionName: nil,
                                     commandRaw: events[0].commandRaw,
                                     commandDisplay: events[0].commandDisplay,
                                     pressureLevel: "CRITICAL", eventRetained: true),
                        PendingRetry(ts: now - 7200, sessionID: "4dcb56b8",
                                     sessionName: "docs sweep",
                                     commandRaw: events[1].commandRaw,
                                     commandDisplay: events[1].commandDisplay,
                                     pressureLevel: "CRITICAL", eventRetained: true),
                       ])
    if PREVIEW_EMPTY {
        s.gate.since = now - 600
        s.gate.historyFrom = s.gate.since
        s.gate.historyTo = nil
        s.gate.evaluated = 0; s.gate.warned = 0; s.gate.stopped = 0
        s.gate.events = []; s.gate.pending = []
    }
    if PREVIEW_NOT_INSTALLED {
        s.gate.installed = false; s.gate.paused = false
    }
    m.snap = s; m.loaded = true; m.lastSync = Date()

    let view = ContentView(model: m, onQuit: {}, onReap: {},
                           flattened: true, previewExpandAll: false, previewOpenGate: true,
                           frameHeight: 1000)
    let renderer = ImageRenderer(content: view)
    renderer.scale = 2
    guard let img = renderer.nsImage,
          let tiff = img.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else {
        FileHandle.standardError.write("render failed\n".data(using: .utf8)!)
        exit(1)
    }
    try? png.write(to: URL(fileURLWithPath: path))
    print("rendered \(path)")
}

if let i = CommandLine.arguments.firstIndex(of: "--render"),
   i + 1 < CommandLine.arguments.count {
    let out = CommandLine.arguments[i + 1]
    _ = NSApplication.shared          // AppKit must exist for text rendering
    MainActor.assumeIsolated { renderPreview(to: out) }
    exit(0)
}

let app = NSApplication.shared
let controller = Controller()
app.delegate = controller
app.run()
