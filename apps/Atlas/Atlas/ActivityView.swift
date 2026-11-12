// The agent's chain of thought: every turn (yours, or a check it ran on its own), its thinking, the tools it
// called with what came back, and what it decided. Read-only, live.
import SwiftUI

struct ActivityView: View {
    @Environment(ChatStore.self) private var store
    @State private var expanded: Set<String> = []

    @State private var atBottom = true          // sticky: follow new items only while the user is at the bottom
    @State private var visible: Set<String> = []  // rows the lazy stack currently has on screen (approximately)
    @State private var settlingUntil = Date.distantPast   // ignore position reports while we are scrolling to the end ourselves

    var body: some View {
        GeometryReader { outer in
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 6) {
                        if store.thoughts.isEmpty {
                            Text("Nothing yet. Each turn the agent takes shows up here as it happens.")
                                .foregroundStyle(.secondary).padding(.top, 40).frame(maxWidth: .infinity)
                        }
                        ForEach(store.thoughts) { item in
                            row(item)
                                .id(item.id)
                                .onAppear { visible.insert(item.id); rememberAnchor() }
                                .onDisappear { visible.remove(item.id); rememberAnchor() }
                        }
                        // The sentinel reports where the bottom of the content is relative to the viewport.
                        GeometryReader { g in
                            Color.clear.preference(key: BottomOffsetKey.self, value: g.frame(in: .named("activityScroll")).maxY)
                        }
                        .frame(height: 1)
                        .id("abottom")
                    }
                    .padding(12)
                }
                .coordinateSpace(name: "activityScroll")
                .onPreferenceChange(BottomOffsetKey.self) { maxY in
                    // within ~60 pt of the end counts as "at the bottom"; scrolling up switches auto-follow off.
                    // Reports that arrive while new rows are being appended and we are scrolling to them are ignored,
                    // otherwise the growth itself would read as "the user scrolled up".
                    guard Date() > settlingUntil else { return }
                    let nowAtBottom = maxY <= outer.size.height + 60
                    if nowAtBottom != atBottom { atBottom = nowAtBottom; rememberAnchor() }
                }
                .onChange(of: store.thoughts.count) { _, _ in
                    if store.pendingScrollRestore && !store.thoughts.isEmpty {
                        restore(proxy)
                    } else if atBottom {
                        follow(proxy)
                    }
                }
                .onChange(of: outer.size.height) { _, _ in if atBottom { follow(proxy) } }   // window resized: stay on the end
                .onAppear {
                    if store.pendingScrollRestore && !store.thoughts.isEmpty { restore(proxy) } else { proxy.scrollTo("abottom", anchor: .bottom) }
                }
                .overlay(alignment: .bottomTrailing) {
                    if !atBottom {
                        Button {
                            withAnimation { proxy.scrollTo("abottom", anchor: .bottom) }
                        } label: {
                            Label("Latest", systemImage: "arrow.down.to.line").font(.caption)
                        }
                        .buttonStyle(.borderedProminent).controlSize(.small).padding(10)
                    }
                }
            }
        }
        .font(.callout)
        .background(Color.secondary.opacity(0.04))
    }

    /// Scroll to the end now and again as the lazy rows get their real heights.
    private func follow(_ proxy: ScrollViewProxy) {
        settlingUntil = Date().addingTimeInterval(0.8)
        proxy.scrollTo("abottom", anchor: .bottom)
        for delay in [0.05, 0.25, 0.6] {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { proxy.scrollTo("abottom", anchor: .bottom) }
        }
    }

    /// After a launch: back to the row that was at the top of the viewport last time, or the bottom.
    private func restore(_ proxy: ScrollViewProxy) {
        store.pendingScrollRestore = false
        if let a = store.thoughtsAnchor, store.thoughts.contains(where: { $0.id == a }) {
            atBottom = false
            DispatchQueue.main.async { proxy.scrollTo(a, anchor: .top) }
        } else {
            atBottom = true
            DispatchQueue.main.async { follow(proxy) }
        }
    }

    private func rememberAnchor() {
        guard !store.pendingScrollRestore else { return }
        let anchor = atBottom ? nil : store.thoughts.first(where: { visible.contains($0.id) })?.id
        if anchor != store.thoughtsAnchor { store.thoughtsAnchor = anchor }
    }

    @ViewBuilder private func row(_ item: ActivityItem) -> some View {
        switch item.kind {
        case .turn:
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: item.isScheduled ? "clock" : "person").foregroundStyle(.secondary)
                Text(turnLabel(item)).fontWeight(.semibold).lineLimit(2)
                Spacer()
                Text(item.hhmm).font(.caption).foregroundStyle(.secondary)
            }
            .padding(.top, 14)
        case .thinking:
            collapsible(item, icon: "brain", tint: .secondary, lines: 6) {
                Text(item.text).italic().foregroundStyle(.secondary)
            }
        case .text:
            if item.final {
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: item.isScheduled ? "checkmark.circle" : "arrowshape.turn.up.left").foregroundStyle(.green)
                    Text(item.isScheduled ? "ended: " + item.text : "replied").lineLimit(3).fixedSize(horizontal: false, vertical: true).foregroundStyle(.secondary)
                }
            } else {
                collapsible(item, icon: "text.alignleft", tint: .secondary, lines: 4) { Text(item.text) }
            }
        case .tool_call:
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "wrench.and.screwdriver").foregroundStyle(.blue)
                Text(item.name).fontWeight(.medium)
                Text(item.input == "{}" ? "" : item.input).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(expanded.contains(item.id) ? nil : 1).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .contentShape(Rectangle())
            .onTapGesture { toggle(item.id) }
            .contextMenu { Button("Copy call") { Clipboard.set("\(item.name) \(item.input)") } }
        case .tool_result:
            collapsible(item, icon: item.error ? "exclamationmark.triangle" : "arrow.turn.down.right", tint: item.error ? .red : .secondary, lines: 2) {
                Text(item.output.isEmpty ? "(empty)" : item.output).font(.caption.monospaced()).foregroundStyle(item.error ? .red : .secondary)
            }
        }
    }

    private func collapsible<C: View>(_ item: ActivityItem, icon: String, tint: Color, lines: Int, @ViewBuilder content: () -> C) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: icon).foregroundStyle(tint).frame(width: 16)
            // fixedSize: selectable multi-line Text in a lazy stack otherwise under-reports its height and the next
            // row draws over it (seen 2026-11-07)
            content().lineLimit(expanded.contains(item.id) ? nil : lines).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            Spacer(minLength: 0)
            Image(systemName: expanded.contains(item.id) ? "chevron.up" : "chevron.down")
                .font(.caption2).foregroundStyle(.tertiary).padding(.top, 3)
                .help(expanded.contains(item.id) ? "Collapse" : "Show the whole text")
        }
        .padding(.leading, 2)
        .contentShape(Rectangle())
        .onTapGesture { toggle(item.id) }
        .contextMenu { Button("Copy") { Clipboard.set(item.kind == .tool_result ? item.output : item.text) } }
    }

    private func toggle(_ id: String) {
        if expanded.contains(id) { expanded.remove(id) } else { expanded.insert(id) }
    }

    private func turnLabel(_ item: ActivityItem) -> String {
        if item.isScheduled {
            // "[heartbeat: 12:03, next around 13:10]\nUnscheduled check-in..." -> "heartbeat: 12:03, next around 13:10"
            let first = item.label.split(separator: "\n", maxSplits: 1).first.map(String.init) ?? item.label
            return first.trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        }
        return "\(store.config.isConfigured ? "You" : "User") (\(item.channel)): \(item.label)"
    }
}


private struct BottomOffsetKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}
