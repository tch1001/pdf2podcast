import AVFoundation
import MediaPlayer
import OSLog
import SwiftUI
import UniformTypeIdentifiers
import UIKit

private enum PodcastPalette {
    static let background = Color(red: 0.032, green: 0.045, blue: 0.059)
    static let panel = Color(red: 0.063, green: 0.086, blue: 0.113)
    static let cyan = Color(red: 0.427, green: 0.827, blue: 1)
    static let muted = Color(red: 0.51, green: 0.57, blue: 0.62)
    static let line = Color.white.opacity(0.09)
    static let danger = Color(red: 1, green: 0.44, blue: 0.47)
}

private func audioTime(_ seconds: Double) -> String {
    guard seconds.isFinite else { return "0:00" }
    let total = max(0, Int(seconds))
    if total >= 3600 { return String(format: "%d:%02d:%02d", total / 3600, total % 3600 / 60, total % 60) }
    return String(format: "%d:%02d", total / 60, total % 60)
}

private struct SyntheticJob: Codable, Identifiable, Hashable, Sendable {
    let jobID: String
    let filename: String?
    let documentTitle: String?
    let status: String
    let stage: String?
    let progressPercent: Double?
    let estimatedSecondsRemaining: Double?
    let completedChunks: Int?
    let totalChunks: Int?
    let chapterCount: Int?
    let sectionCount: Int?
    let pageCount: Int?
    let audioBytes: Int?
    let durationSeconds: Double?
    let playbackURL: String?
    let readingMode: String?
    let paragraphCount: Int?
    let error: String?

    var id: String { jobID }
    var title: String {
        if let documentTitle, !documentTitle.isEmpty, documentTitle != "input" { return documentTitle }
        return filename.map { ($0 as NSString).deletingPathExtension } ?? jobID
    }
    var normalizedStatus: String { status.lowercased() }
    var isComplete: Bool { ["complete", "completed"].contains(normalizedStatus) }
    var isActive: Bool { ["queued", "processing", "running"].contains(normalizedStatus) }
    var isFailed: Bool { ["failed", "aborted"].contains(normalizedStatus) }
    var progress: Double { min(100, max(0, progressPercent ?? 0)) }
    var detail: String {
        var parts: [String] = []
        if let durationSeconds, durationSeconds > 0 { parts.append(audioTime(durationSeconds)) }
        if let chapterCount, chapterCount > 0 { parts.append("\(chapterCount) chapters") }
        if let count = paragraphCount ?? sectionCount, count > 0 {
            parts.append("\(count) \(readingMode == "paper_companion" ? "paragraphs" : "sections")")
        }
        return parts.isEmpty ? (stage ?? normalizedStatus.capitalized) : parts.joined(separator: " · ")
    }
    var eta: String {
        guard let seconds = estimatedSecondsRemaining, seconds > 0 else { return "Estimating..." }
        return seconds >= 3600 ? "~\(Int(ceil(seconds / 3600))) hr left" : "~\(Int(ceil(seconds / 60))) min left"
    }
    enum CodingKeys: String, CodingKey {
        case filename, status, stage, error
        case documentTitle = "title"
        case jobID = "job_id"
        case progressPercent = "progress_percent"
        case estimatedSecondsRemaining = "estimated_seconds_remaining"
        case completedChunks = "completed_chunks"
        case totalChunks = "total_chunks"
        case chapterCount = "chapter_count"
        case sectionCount = "section_count"
        case pageCount = "page_count"
        case audioBytes = "audio_bytes"
        case durationSeconds = "duration_seconds"
        case playbackURL = "playback_url"
        case readingMode = "reading_mode"
        case paragraphCount = "paragraph_count"
    }
}

private struct SyntheticJobsEnvelope: Codable, Sendable { let jobs: [SyntheticJob] }
private struct SyntheticManifest: Codable, Sendable {
    let version: Int
    let title: String
    let duration: Double
    let chapters: [SyntheticChapter]
    var sections: [SyntheticSection] { chapters.flatMap(\.sections) }
}
private struct SyntheticChapter: Codable, Identifiable, Sendable {
    let n: Int
    let title: String
    let sections: [SyntheticSection]
    let sourceHeading: String?
    let pageStart: Int?
    let pageEnd: Int?
    let level: Int?
    var id: Int { n }
    enum CodingKeys: String, CodingKey {
        case n, title, sections, level
        case sourceHeading = "source_heading"
        case pageStart = "page_start"
        case pageEnd = "page_end"
    }
}
private struct SyntheticAudioSegment: Codable, Sendable {
    let speaker: String
    let kind: String
    let start: Double
    let end: Double
}

private struct SyntheticSection: Codable, Identifiable, Sendable {
    let n: Int
    let title: String
    let slug: String
    let start: Double
    let duration: Double
    let paragraphID: String?
    let sourceText: String?
    let spokenText: String?
    let summary: String?
    let sourceStart: Double?
    let summaryStart: Double?
    let audioSegments: [SyntheticAudioSegment]?
    let contentKind: String?
    let end: Double?
    let pageStart: Int?
    let pageEnd: Int?
    var id: String { slug }
    var pageLabel: String? {
        guard let pageStart else { return nil }
        if let pageEnd, pageEnd != pageStart { return "PDF pages \(pageStart)-\(pageEnd)" }
        return "PDF page \(pageStart)"
    }
    enum CodingKeys: String, CodingKey {
        case n, title, slug, start, duration, summary, end
        case paragraphID = "paragraph_id"
        case sourceText = "source_text"
        case spokenText = "spoken_text"
        case sourceStart = "source_start"
        case summaryStart = "summary_start"
        case audioSegments = "audio_segments"
        case contentKind = "content_kind"
        case pageStart = "page_start"
        case pageEnd = "page_end"
    }
}
private struct SyntheticTranscript: Codable, Sendable {
    let version: Int
    let duration: Double
    let lines: [SyntheticTranscriptLine]
}
private struct SyntheticTranscriptLine: Codable, Sendable {
    let t: Double
    let speaker: String
    let kind: String
    let text: String
    let sourceText: String?
    let paragraphID: String?
    let chapterNumber: Int?
    let sectionNumber: Int?
    let pageStart: Int?
    let pageEnd: Int?
    let end: Double?
    var displayText: String { speaker == "source" ? (sourceText ?? text) : text }
    enum CodingKeys: String, CodingKey {
        case t, speaker, kind, text, end
        case sourceText = "source_text"
        case paragraphID = "paragraph_id"
        case chapterNumber = "chapter_n"
        case sectionNumber = "section_n"
        case pageStart = "page_start"
        case pageEnd = "page_end"
    }
}
private struct SummaryRange: Sendable {
    let start: Double
    let end: Double
}

private struct ListeningExperience: Sendable {
    let id = UUID()
    let manifest: SyntheticManifest
    let lines: [SyntheticTranscriptLine]
    let sections: [SyntheticSection]
    let summaryRanges: [SummaryRange]

    init(manifest: SyntheticManifest, lines: [SyntheticTranscriptLine]) {
        self.manifest = manifest
        self.lines = lines
        sections = manifest.chapters.flatMap(\.sections)
        var ranges: [SummaryRange] = []
        for section in sections {
            if let segments = section.audioSegments {
                ranges.append(contentsOf: segments.filter { $0.kind == "summary" }
                    .map { SummaryRange(start: $0.start, end: $0.end) })
            } else if let start = section.summaryStart {
                // Older episodes put the summary after the source reading.
                let end = (section.sourceStart ?? section.start) > start
                    ? (section.sourceStart ?? section.start) : (section.end ?? section.start + section.duration)
                ranges.append(SummaryRange(start: start, end: end))
            }
        }
        for (index, line) in lines.enumerated() where line.kind == "summary" {
            let end = line.end ?? (index + 1 < lines.count ? lines[index + 1].t : manifest.duration)
            ranges.append(SummaryRange(start: line.t, end: end))
        }
        let valid = ranges.filter { $0.start.isFinite && $0.end.isFinite && $0.end > $0.start && $0.start >= 0 }
            .sorted { $0.start < $1.start }
        var merged: [SummaryRange] = []
        for range in valid {
            let end = min(manifest.duration, range.end)
            guard end > range.start else { continue }
            if let last = merged.last, range.start <= last.end + 0.001 {
                merged[merged.count - 1] = SummaryRange(start: last.start, end: max(last.end, end))
            } else { merged.append(SummaryRange(start: range.start, end: end)) }
        }
        summaryRanges = merged
    }
}

// Only the small progress controls observe the high-frequency playback clock.
@MainActor
private final class PlaybackClock: ObservableObject {
    @Published var currentTime: Double = 0
    @Published var duration: Double = 0
    @Published var sleepRemaining = 0
}

private struct ReadingPosition: Equatable {
    var sectionIndex: Int?
    var lineIndex = 0
}

// AVPlayer requires removal on the exact player that created an observer token.
private final class PlayerTimeObservation {
    let player: AVPlayer
    var token: Any?

    init(player: AVPlayer) { self.player = player }
    func invalidate() {
        if let token { player.removeTimeObserver(token) }
        token = nil
    }
    deinit { invalidate() }
}

private enum RemotePlaybackAction: Sendable {
    case play, pause, toggle
    case skip(Double), seek(Double), rate(Float)
}

// Keep remote-command and audio-session registrations tied to the player's lifetime.
private final class RemotePlaybackControls {
    private var commands: [(MPRemoteCommand, Any)] = []
    private var notifications: [NSObjectProtocol] = []

    func add(_ command: MPRemoteCommand, handler: @escaping (MPRemoteCommandEvent) -> MPRemoteCommandHandlerStatus) {
        commands.append((command, command.addTarget(handler: handler)))
    }
    func observe(_ name: Notification.Name, handler: @escaping (Notification) -> Void) {
        notifications.append(NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main, using: handler))
    }
    deinit {
        for (command, token) in commands { command.removeTarget(token) }
        for token in notifications { NotificationCenter.default.removeObserver(token) }
    }
}

@MainActor
private final class SyntheticLibrary: ObservableObject {
    @Published var jobs: [SyntheticJob] = []
    // Only playback changes selectedJob. Browsing a detail page never changes audio.
    @Published var selectedJob: SyntheticJob?
    @Published var experiences: [String: ListeningExperience] = [:]
    @Published var experienceErrors: [String: String] = [:]
    @Published var isPlaying = false
    @Published var isLoadingAudio = false
    @Published var isUploading = false
    let clock = PlaybackClock()
    @Published private(set) var readingPosition = ReadingPosition()
    var currentTime: Double {
        get { clock.currentTime }
        set {
            guard newValue.isFinite else { return }
            if clock.currentTime != newValue { clock.currentTime = newValue }
            updateReadingPosition()
        }
    }
    var duration: Double {
        get { clock.duration }
        set {
            guard newValue.isFinite, newValue >= 0 else { return }
            if clock.duration != newValue { clock.duration = newValue }
        }
    }
    @Published var playbackRate: Float = 1
    @Published var skipSummaries = UserDefaults.standard.bool(forKey: "pdf2podcast.skipSummaries") {
        didSet {
            guard skipSummaries != oldValue else { return }
            UserDefaults.standard.set(skipSummaries, forKey: "pdf2podcast.skipSummaries")
            configureSummaryBoundary()
            _ = skipSummaryIfNeeded()
        }
    }
    @Published var seekRevision = 0
    @Published var errorMessage: String?
    @Published var serverURL: String
    @Published var mobileToken: String
    @Published var sleepDeadline: Date?
    @Published var sleepSectionEnd: Double?
    var sleepRemaining: Int {
        get { clock.sleepRemaining }
        set { if clock.sleepRemaining != newValue { clock.sleepRemaining = newValue } }
    }

    private var player: AVPlayer?
    private var timeObserver: PlayerTimeObservation?
    private var statusObservation: NSKeyValueObservation?
    private var itemObservation: NSKeyValueObservation?
    private var playerVolumeObservation: NSKeyValueObservation?
    private var outputVolumeObservation: NSKeyValueObservation?
    private var audioSessionActive = false
    private let audioDiagnostics = Logger(subsystem: "io.pdf2podcast.mobile", category: "AudioSession")
    private let uploadDiagnostics = Logger(subsystem: Bundle.main.bundleIdentifier ?? "io.pdf2podcast", category: "PDFUpload")
    private var playbackRequestID = UUID()
    private var experienceTasks: [String: Task<ListeningExperience, Error>] = [:]
    private var experienceRecency: [String] = []
    private var wantsPlayback = false
    private var isSeeking = false
    private var seekID = UUID()
    private var sleepTimer: Timer?
    private var sleepWork: DispatchWorkItem?
    private var sleepBoundary: PlayerTimeObservation?
    private var sleepBoundaryID = UUID()
    private var summaryBoundary: PlayerTimeObservation?
    private var summaryBoundaryID = UUID()
    private let remoteControls = RemotePlaybackControls()
    private var lastNowPlayingUpdate = Date.distantPast
    private var resumeAfterInterruption = false

    init() {
        let sharedConnection = try? ShareConnectionStore.load()
        let currentServerURL = "https://participant-barrel-ladder-marsh.trycloudflare.com"
        let savedServerURL = UserDefaults.standard.string(forKey: "pdf2podcast.serverURL")
            ?? sharedConnection?.serverURL
        let retiredHosts = [
            "interactions-expect-overnight-polyphonic.trycloudflare.com",
            "moved-ext-questions-chat.trycloudflare.com",
        ]
        let savedHost = savedServerURL.flatMap { URL(string: $0)?.host?.lowercased() }
        if savedServerURL == nil || savedHost == "127.0.0.1" || savedHost == "localhost"
            || savedHost.map(retiredHosts.contains) == true {
            serverURL = currentServerURL
            UserDefaults.standard.set(currentServerURL, forKey: "pdf2podcast.serverURL")
        } else {
            serverURL = savedServerURL ?? currentServerURL
        }
        mobileToken = UserDefaults.standard.string(forKey: "pdf2podcast.mobileToken")
            ?? sharedConnection?.token
            ?? ""
        configureRemoteControls()
        if !serverURL.isEmpty && !mobileToken.isEmpty {
            saveConnection()
        }
    }

    var completedJobs: [SyntheticJob] { jobs.filter(\.isComplete) }
    var activeJobs: [SyntheticJob] { jobs.filter(\.isActive) }
    var manifest: SyntheticManifest? { selectedJob.flatMap { experiences[$0.id]?.manifest } }
    var currentSection: SyntheticSection? {
        guard let id = selectedJob?.id, let sections = experiences[id]?.sections,
              let index = readingPosition.sectionIndex, sections.indices.contains(index) else { return nil }
        return sections[index]
    }
    var sleepIsActive: Bool { sleepDeadline != nil || sleepSectionEnd != nil }
    var sleepLabel: String {
        if sleepSectionEnd != nil { return "End of section" }
        return sleepDeadline == nil ? "Sleep timer" : audioTime(Double(sleepRemaining))
    }
    var rateLabel: String { String(format: "%gx", playbackRate) }

    func isCurrent(_ job: SyntheticJob) -> Bool { selectedJob?.id == job.id }
    func lineIndex(for job: SyntheticJob) -> Int {
        isCurrent(job) ? readingPosition.lineIndex : 0
    }

    private func updateReadingPosition() {
        guard let id = selectedJob?.id, let experience = experiences[id] else {
            if readingPosition != ReadingPosition() { readingPosition = ReadingPosition() }
            return
        }
        var lower = 0
        var upper = experience.lines.count
        while lower < upper {
            let middle = (lower + upper) / 2
            if experience.lines[middle].t <= currentTime { lower = middle + 1 } else { upper = middle }
        }
        let lineIndex = max(0, lower - 1)
        lower = 0
        upper = experience.sections.count
        while lower < upper {
            let middle = (lower + upper) / 2
            if experience.sections[middle].start <= currentTime + 0.01 { lower = middle + 1 } else { upper = middle }
        }
        let next = ReadingPosition(sectionIndex: lower > 0 ? lower - 1 : nil, lineIndex: lineIndex)
        if readingPosition != next {
            readingPosition = next
            publishNowPlaying(force: true)
        }
    }

    func releaseUnusedExperiences(keeping visible: Set<String> = [], limit: Int = 3) {
        var protected = visible
        if let id = selectedJob?.id { protected.insert(id) }
        var retained = Set(experiences.keys)
        for id in experienceRecency where retained.count > max(limit, protected.count) {
            if !protected.contains(id) { retained.remove(id) }
        }
        if retained.count != experiences.count {
            experiences = experiences.filter { retained.contains($0.key) }
            experienceRecency.removeAll { !retained.contains($0) }
        }
    }

    private func touchExperience(_ id: String) {
        experienceRecency.removeAll { $0 == id }
        experienceRecency.append(id)
    }

    @discardableResult
    func configureAudio() -> Bool {
        let session = AVAudioSession.sharedInstance()
        do {
            if session.category != .playback || session.mode != .spokenAudio || !session.categoryOptions.isEmpty {
                try session.setCategory(.playback, mode: .spokenAudio, options: [])
            }
            try session.setActive(true)
            audioSessionActive = true
            recordAudioDiagnostics("session-activated")
            return true
        } catch {
            let failure = error as NSError
            recordAudioDiagnostics("activation-failed:\(failure.domain):\(failure.code)")
            errorMessage = "Audio setup failed: \(error.localizedDescription)"
            return false
        }
    }

    private func releaseAudioSession() {
        guard audioSessionActive else { return }
        // The player must already be paused. Never reset an actively playing session on screen lock.
        do {
            try AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            audioSessionActive = false
            recordAudioDiagnostics("session-released")
        } catch {
            let failure = error as NSError
            // iOS reports isBusy after making the session inactive if audio was still winding down.
            if failure.domain == NSOSStatusErrorDomain, failure.code == AVAudioSession.ErrorCode.isBusy.rawValue {
                audioSessionActive = false
            }
            recordAudioDiagnostics("deactivation-failed:\(failure.domain):\(failure.code)")
        }
    }

    func recordAudioDiagnostics(_ event: String) {
        let session = AVAudioSession.sharedInstance()
        let routes = session.currentRoute.outputs.map { $0.portType.rawValue }.joined(separator: ",")
        // Log only audio state, never stream URLs, credentials, device names or document text.
        let details = "event=\(event) category=\(session.category.rawValue) mode=\(session.mode.rawValue)"
            + " options=\(session.categoryOptions.rawValue) systemVolume=\(session.outputVolume)"
            + " playerVolume=\(player?.volume ?? -1) muted=\(player?.isMuted ?? false) rate=\(player?.rate ?? 0)"
            + " position=\(currentTime) wantsPlayback=\(wantsPlayback) sessionActive=\(audioSessionActive)"
            + " appState=\(UIApplication.shared.applicationState.rawValue) routes=\(routes)"
        audioDiagnostics.notice("\(details, privacy: .public)")
    }

    private func configureRemoteControls() {
        let center = MPRemoteCommandCenter.shared()
        func bind(_ command: MPRemoteCommand, action: @escaping (MPRemoteCommandEvent) -> RemotePlaybackAction?) {
            remoteControls.add(command) { [weak self] event in
                guard let self, let action = action(event) else { return .commandFailed }
                Task { @MainActor in self.performRemoteAction(action) }
                return .success
            }
        }
        bind(center.playCommand) { _ in .play }
        bind(center.pauseCommand) { _ in .pause }
        bind(center.stopCommand) { _ in .pause }
        bind(center.togglePlayPauseCommand) { _ in .toggle }
        center.skipForwardCommand.preferredIntervals = [15]
        center.skipBackwardCommand.preferredIntervals = [15]
        bind(center.skipForwardCommand) { .skip(($0 as? MPSkipIntervalCommandEvent)?.interval ?? 15) }
        bind(center.skipBackwardCommand) { .skip(-(($0 as? MPSkipIntervalCommandEvent)?.interval ?? 15)) }
        bind(center.changePlaybackPositionCommand) {
            guard let event = $0 as? MPChangePlaybackPositionCommandEvent else { return nil }
            return .seek(event.positionTime)
        }
        center.changePlaybackRateCommand.supportedPlaybackRates = [0.85, 1, 1.25, 1.5, 1.75, 2]
        bind(center.changePlaybackRateCommand) {
            guard let event = $0 as? MPChangePlaybackRateCommandEvent else { return nil }
            return .rate(event.playbackRate)
        }
        center.nextTrackCommand.isEnabled = false
        center.previousTrackCommand.isEnabled = false
        outputVolumeObservation = AVAudioSession.sharedInstance().observe(\.outputVolume, options: [.initial, .new]) { [weak self] session, change in
            let volume = change.newValue ?? session.outputVolume
            Task { @MainActor in self?.recordAudioDiagnostics("system-volume:\(volume)") }
        }
        remoteControls.observe(AVAudioSession.interruptionNotification) { [weak self] notification in
            guard let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt else { return }
            let options = notification.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
            Task { @MainActor in
                guard let self else { return }
                self.recordAudioDiagnostics("interruption:\(raw):options=\(options)")
                if raw == AVAudioSession.InterruptionType.began.rawValue {
                    // An interruption has already deactivated our session; do not fight its owner.
                    self.audioSessionActive = false
                    self.resumeAfterInterruption = self.wantsPlayback
                    self.pause(keepInterruptionResume: true)
                } else if raw == AVAudioSession.InterruptionType.ended.rawValue {
                    let resume = self.resumeAfterInterruption && AVAudioSession.InterruptionOptions(rawValue: options).contains(.shouldResume)
                    self.resumeAfterInterruption = false
                    if resume { self.resumePlayback() }
                }
            }
        }
        remoteControls.observe(AVAudioSession.routeChangeNotification) { [weak self] notification in
            guard let reason = notification.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt else { return }
            Task { @MainActor in
                guard let self else { return }
                self.recordAudioDiagnostics("route-change:\(reason)")
                if reason == AVAudioSession.RouteChangeReason.oldDeviceUnavailable.rawValue { self.pause() }
            }
        }
        remoteControls.observe(AVAudioSession.mediaServicesWereResetNotification) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.audioSessionActive = false
                self.pause()
                self.disposePlayer()
                self.recordAudioDiagnostics("media-services-reset")
                // Recreate the player at the saved position only after the user taps Play.
            }
        }
        publishNowPlaying(force: true)
    }

    private func performRemoteAction(_ action: RemotePlaybackAction) {
        guard selectedJob != nil else { return }
        switch action {
        case .play: resumePlayback()
        case .pause: pause()
        case .toggle: togglePlayback()
        case .skip(let seconds): skip(seconds: seconds)
        case .seek(let seconds): seek(to: seconds, resume: wantsPlayback)
        case .rate(let rate): setPlaybackRate(rate)
        }
    }

    private func publishNowPlaying(force: Bool = false) {
        let now = Date()
        guard force || now.timeIntervalSince(lastNowPlayingUpdate) >= 5 else { return }
        lastNowPlayingUpdate = now
        let center = MPRemoteCommandCenter.shared()
        let available = selectedJob != nil
        center.playCommand.isEnabled = available
        center.pauseCommand.isEnabled = available
        center.stopCommand.isEnabled = available
        center.togglePlayPauseCommand.isEnabled = available
        center.skipForwardCommand.isEnabled = player != nil && duration > 0
        center.skipBackwardCommand.isEnabled = player != nil && duration > 0
        center.changePlaybackPositionCommand.isEnabled = player != nil && duration > 0
        center.changePlaybackRateCommand.isEnabled = available
        guard let job = selectedJob else {
            MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
            return
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = [
            MPMediaItemPropertyTitle: job.title,
            MPMediaItemPropertyArtist: currentSection?.title ?? "Pdf2Podcast",
            MPMediaItemPropertyAlbumTitle: "Pdf2Podcast",
            MPMediaItemPropertyPlaybackDuration: duration,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: currentTime,
            MPNowPlayingInfoPropertyPlaybackRate: isPlaying ? Double(playbackRate) : 0,
            MPNowPlayingInfoPropertyDefaultPlaybackRate: Double(playbackRate),
            MPNowPlayingInfoPropertyMediaType: MPNowPlayingInfoMediaType.audio.rawValue,
            MPNowPlayingInfoPropertyIsLiveStream: false,
            MPNowPlayingInfoPropertyExternalContentIdentifier: job.id,
        ]
    }

    private func timeSkippingSummaries(_ time: Double) -> Double {
        guard skipSummaries, let id = selectedJob?.id, let ranges = experiences[id]?.summaryRanges else { return time }
        var lower = 0
        var upper = ranges.count
        while lower < upper {
            let middle = (lower + upper) / 2
            if ranges[middle].start <= time + 0.01 { lower = middle + 1 } else { upper = middle }
        }
        guard lower > 0, time < ranges[lower - 1].end else { return time }
        return ranges[lower - 1].end
    }

    private func configureSummaryBoundary() {
        summaryBoundaryID = UUID()
        summaryBoundary?.invalidate()
        summaryBoundary = nil
        guard skipSummaries, let player, let id = selectedJob?.id,
              let ranges = experiences[id]?.summaryRanges, !ranges.isEmpty else { return }
        let boundaryID = summaryBoundaryID
        let observation = PlayerTimeObservation(player: player)
        summaryBoundary = observation
        observation.token = player.addBoundaryTimeObserver(
            forTimes: ranges.map { NSValue(time: CMTime(seconds: $0.start, preferredTimescale: 600)) }, queue: .main
        ) { [weak self, weak player] in
            Task { @MainActor in
                guard let self, let player, self.player === player, self.summaryBoundaryID == boundaryID else { return }
                _ = self.skipSummaryIfNeeded()
            }
        }
    }

    @discardableResult
    private func skipSummaryIfNeeded() -> Bool {
        guard skipSummaries, wantsPlayback, !isSeeking, let player else { return false }
        let time = player.currentTime().seconds
        guard time.isFinite else { return false }
        let target = timeSkippingSummaries(time)
        guard target > time + 0.005 else { return false }
        if let end = sleepSectionEnd, end <= target {
            finishSleepTimer()
        } else {
            seek(to: target, resume: true)
        }
        return true
    }

    func saveConnection() {
        serverURL = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        mobileToken = mobileToken.trimmingCharacters(in: .whitespacesAndNewlines)
        UserDefaults.standard.set(serverURL, forKey: "pdf2podcast.serverURL")
        do {
            try ShareConnectionStore.save(ShareConnection(serverURL: serverURL, token: mobileToken))
            UserDefaults.standard.removeObject(forKey: "pdf2podcast.mobileToken")
        } catch {
            errorMessage = "Could not share the connection with the PDF share extension: \(error.localizedDescription)"
        }
    }

    func loadJobs(showErrors: Bool = true) async {
        guard !mobileToken.isEmpty else {
            if showErrors { errorMessage = "Enter the mobile token to load your library." }
            return
        }
        do {
            let data = try await request(path: "/api/audio/jobs?limit=100")
            let fetched = try await Task.detached(priority: .utility) {
                if let envelope = try? JSONDecoder().decode(SyntheticJobsEnvelope.self, from: data) {
                    return envelope.jobs
                }
                return try JSONDecoder().decode([SyntheticJob].self, from: data)
            }.value
            if jobs != fetched { jobs = fetched }
            if let selectedJob, let fresh = jobs.first(where: { $0.id == selectedJob.id }), fresh != selectedJob {
                self.selectedJob = fresh
            }
        } catch {
            if showErrors { errorMessage = "Library failed to load: \(error.localizedDescription)" }
        }
    }

    func uploadPDF(_ sourceURL: URL) async {
        guard !isUploading else { return }
        isUploading = true
        errorMessage = nil
        let uploadID = UUID().uuidString
        var phase = "reading the selected PDF"
        uploadDiagnostics.notice("upload=\(uploadID, privacy: .public) preparing-file")
        let accessed = sourceURL.startAccessingSecurityScopedResource()
        defer {
            if accessed { sourceURL.stopAccessingSecurityScopedResource() }
            isUploading = false
        }
        do {
            let fileData = try await Task.detached(priority: .userInitiated) {
                var coordinationError: NSError?
                var result: Result<Data, Error>?
                NSFileCoordinator(filePresenter: nil).coordinate(
                    readingItemAt: sourceURL, options: .forUploading, error: &coordinationError
                ) { readableURL in
                    result = Result { try Data(contentsOf: readableURL) }
                }
                if let coordinationError { throw coordinationError }
                guard let result else { throw CocoaError(.fileReadUnknown) }
                return try result.get()
            }.value
            guard !fileData.isEmpty else { throw LibraryError.message("The selected PDF is empty.") }
            uploadDiagnostics.notice("upload=\(uploadID, privacy: .public) file-ready bytes=\(fileData.count)")
            let boundary = "Pdf2Podcast-\(UUID().uuidString)"
            let name = sourceURL.lastPathComponent.replacingOccurrences(of: "\"", with: "_")
                .replacingOccurrences(of: "\r", with: "").replacingOccurrences(of: "\n", with: "")
            var body = Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"\(name)\"\r\nContent-Type: application/pdf\r\n\r\n".utf8)
            body.append(fileData)
            body.append(Data("\r\n--\(boundary)--\r\n".utf8))
            phase = "sending the PDF to the server"
            _ = try await request(path: "/api/audio/upload", method: "POST",
                contentType: "multipart/form-data; boundary=\(boundary)", body: body)
            uploadDiagnostics.notice("upload=\(uploadID, privacy: .public) accepted-by-server")
            await loadJobs()
        } catch {
            let failure = error as NSError
            uploadDiagnostics.error("upload=\(uploadID, privacy: .public) phase=\(phase, privacy: .public) domain=\(failure.domain, privacy: .public) code=\(failure.code)")
            errorMessage = "Upload failed while \(phase): \(error.localizedDescription) [\(failure.domain) \(failure.code)]"
        }
    }

    func loadExperience(for job: SyntheticJob) async {
        if experiences[job.id] != nil { touchExperience(job.id); return }
        experienceErrors[job.id] = nil
        let task: Task<ListeningExperience, Error>
        if let existing = experienceTasks[job.id] {
            task = existing
        } else {
            task = Task {
                async let manifestData = request(path: "/api/audio/jobs/\(job.id)/manifest")
                async let syncData = request(path: "/api/audio/jobs/\(job.id)/sync")
                let (manifestBytes, syncBytes) = try await (manifestData, syncData)
                return try await Task.detached(priority: .userInitiated) {
                    let manifest = try JSONDecoder().decode(SyntheticManifest.self, from: manifestBytes)
                    let sync = try JSONDecoder().decode(SyntheticTranscript.self, from: syncBytes)
                    return ListeningExperience(manifest: manifest, lines: sync.lines)
                }.value
            }
            experienceTasks[job.id] = task
        }
        do {
            let experience = try await task.value
            if experiences[job.id] == nil { experiences[job.id] = experience }
            touchExperience(job.id)
            releaseUnusedExperiences(keeping: [job.id])
            updateReadingPosition()
            if isCurrent(job) {
                configureSummaryBoundary()
                _ = skipSummaryIfNeeded()
            }
        }
        catch { experienceErrors[job.id] = error.localizedDescription }
        experienceTasks[job.id] = nil
    }

    func play(_ job: SyntheticJob, at start: Double? = nil) async {
        guard job.isComplete else { return }
        if isCurrent(job), let player, player.currentItem?.status != .failed {
            if let start { seek(to: start, resume: true) } else { togglePlayback() }
            return
        }
        if isCurrent(job), player == nil, isLoadingAudio, start == nil { pause(); return }
        if sleepSectionEnd != nil { cancelSleepTimer() }
        let requestID = UUID()
        playbackRequestID = requestID
        disposePlayer()
        selectedJob = job
        currentTime = start ?? 0
        duration = experiences[job.id]?.manifest.duration ?? job.durationSeconds ?? 0
        wantsPlayback = true
        isLoadingAudio = true
        errorMessage = nil
        do {
            if skipSummaries {
                await loadExperience(for: job)
                guard playbackRequestID == requestID else { return }
                guard experiences[job.id] != nil else {
                    throw LibraryError.message("Summary timings could not load. Retry, or turn off Skip summaries to play without them.")
                }
            }
            if jobs.first(where: { $0.id == job.id })?.playbackURL == nil { await loadJobs() }
            guard playbackRequestID == requestID else { return }
            let fresh = jobs.first(where: { $0.id == job.id }) ?? job
            guard let path = fresh.playbackURL, let base = URL(string: serverURL),
                  let stream = URL(string: path, relativeTo: base)?.absoluteURL,
                  stream.scheme == base.scheme, stream.host == base.host, stream.port == base.port else {
                throw LibraryError.message("The stream is unavailable. Refresh the library and try again.")
            }
            guard configureAudio() else { pause(); return }
            let item = AVPlayerItem(url: stream)
            item.preferredForwardBufferDuration = 8
            let newPlayer = AVPlayer(playerItem: item)
            player = newPlayer
            playerVolumeObservation = newPlayer.observe(\.volume, options: [.initial, .new]) { [weak self] observed, _ in
                Task { @MainActor in
                    guard let self, self.player === observed else { return }
                    self.recordAudioDiagnostics("player-volume-changed")
                }
            }
            // Observe state, not just media time: media time stops while buffering.
            statusObservation = newPlayer.observe(\.timeControlStatus, options: [.initial, .new]) { [weak self] observed, _ in
                Task { @MainActor in
                    guard let self, self.player === observed else { return }
                    self.updatePlaybackState()
                }
            }
            itemObservation = item.observe(\.status, options: [.initial, .new]) { [weak self] item, _ in
                Task { @MainActor in
                    guard let self, self.player?.currentItem === item else { return }
                    if item.status == .failed {
                        self.pause()
                        self.errorMessage = "Audio streaming failed: \(item.error?.localizedDescription ?? "Please try again.")"
                    }
                }
            }
            let observation = PlayerTimeObservation(player: newPlayer)
            timeObserver = observation
            observation.token = newPlayer.addPeriodicTimeObserver(
                forInterval: CMTime(seconds: 0.25, preferredTimescale: 600), queue: .main
            ) { [weak self, weak newPlayer] time in
                Task { @MainActor in
                    guard let self, let newPlayer, self.player === newPlayer else { return }
                    if !self.isSeeking, time.seconds.isFinite { self.currentTime = max(0, time.seconds) }
                    let length = newPlayer.currentItem?.duration.seconds ?? 0
                    if length.isFinite, length > 0 { self.duration = length }
                    if let end = self.sleepSectionEnd, self.currentTime >= end { self.finishSleepTimer() }
                    self.enforceSleepDeadline()
                    if self.skipSummaryIfNeeded() { return }
                    if self.duration > 0, self.currentTime >= self.duration - 0.05 {
                        self.wantsPlayback = false
                    }
                    self.updatePlaybackState()
                }
            }
            configureSummaryBoundary()
            if let start { seek(to: start, resume: true) }
            else if timeSkippingSummaries(0) > 0 { seek(to: 0, resume: true) }
            else { newPlayer.playImmediately(atRate: playbackRate) }
            publishNowPlaying(force: true)
            Task { await loadExperience(for: job) }
        } catch {
            guard playbackRequestID == requestID else { return }
            pause()
            errorMessage = "Playback failed: \(error.localizedDescription)"
        }
    }

    private func updatePlaybackState() {
        guard let player else { return }
        let playing = player.timeControlStatus == .playing
        let buffering = wantsPlayback && (isSeeking || !playing)
        let changed = isPlaying != playing || isLoadingAudio != buffering
        if isPlaying != playing { isPlaying = playing }
        if isLoadingAudio != buffering { isLoadingAudio = buffering }
        if player.timeControlStatus == .paused, !wantsPlayback { releaseAudioSession() }
        if changed { recordAudioDiagnostics("playback-state:\(player.timeControlStatus.rawValue)") }
        publishNowPlaying(force: changed)
    }

    private func disposePlayer() {
        summaryBoundaryID = UUID()
        summaryBoundary?.invalidate()
        summaryBoundary = nil
        sleepBoundaryID = UUID()
        sleepBoundary?.invalidate()
        sleepBoundary = nil
        if sleepSectionEnd != nil { sleepSectionEnd = nil }
        statusObservation?.invalidate()
        itemObservation?.invalidate()
        playerVolumeObservation?.invalidate()
        playerVolumeObservation = nil
        player?.pause()
        releaseAudioSession()
        timeObserver?.invalidate()
        timeObserver = nil
        statusObservation = nil
        itemObservation = nil
        player = nil
        isPlaying = false
        isSeeking = false
        seekID = UUID()
    }

    func pause(keepInterruptionResume: Bool = false) {
        if !keepInterruptionResume { resumeAfterInterruption = false }
        playbackRequestID = UUID()
        wantsPlayback = false
        player?.pause()
        if !keepInterruptionResume { releaseAudioSession() }
        isPlaying = false
        isLoadingAudio = false
        recordAudioDiagnostics(keepInterruptionResume ? "paused-for-interruption" : "paused")
        publishNowPlaying(force: true)
    }

    func togglePlayback() {
        if wantsPlayback || isPlaying || isLoadingAudio { pause(); return }
        resumePlayback()
    }

    func resumePlayback() {
        guard let job = selectedJob else { return }
        guard let player, player.currentItem?.status != .failed else {
            let position = currentTime
            Task { await play(job, at: position) }
            return
        }
        enforceSleepDeadline()
        guard configureAudio() else { pause(); return }
        wantsPlayback = true
        if duration > 0, currentTime >= duration - 0.1 { seek(to: 0, resume: true) }
        else if timeSkippingSummaries(currentTime) > currentTime { seek(to: currentTime, resume: true) }
        else { player.playImmediately(atRate: playbackRate); updatePlaybackState() }
    }

    func seek(to seconds: Double, resume: Bool = true) {
        guard let player, seconds.isFinite else { return }
        if resume, !wantsPlayback {
            guard configureAudio() else { return }
        }
        let adjusted = timeSkippingSummaries(max(0, seconds))
        let target = max(0, duration > 0 ? min(duration, adjusted) : adjusted)
        let operation = UUID()
        seekID = operation
        isSeeking = true
        currentTime = target
        seekRevision += 1
        if resume { wantsPlayback = true }
        updatePlaybackState()
        publishNowPlaying(force: true)
        player.seek(to: CMTime(seconds: target, preferredTimescale: 600),
            toleranceBefore: .zero, toleranceAfter: .zero) { [weak self, weak player] finished in
            Task { @MainActor in
                guard let self, let player, self.player === player, self.seekID == operation else { return }
                self.isSeeking = false
                if finished, self.wantsPlayback { player.playImmediately(atRate: self.playbackRate) }
                self.updatePlaybackState()
            }
        }
    }

    func skip(seconds: Double) { seek(to: currentTime + seconds, resume: wantsPlayback) }
    func seek(fraction: Double) { seek(to: min(1, max(0, fraction)) * duration, resume: wantsPlayback) }
    func cyclePlaybackRate() {
        let rates: [Float] = [0.85, 1, 1.25, 1.5, 1.75, 2]
        setPlaybackRate(rates[((rates.firstIndex(of: playbackRate) ?? 1) + 1) % rates.count])
    }
    private func setPlaybackRate(_ rate: Float) {
        guard rate.isFinite, rate >= 0.5, rate <= 3 else { return }
        playbackRate = rate
        if wantsPlayback { player?.rate = playbackRate }
        publishNowPlaying(force: true)
    }

    func setSleepTimer(minutes: Int) {
        cancelSleepTimer()
        sleepDeadline = Date().addingTimeInterval(Double(minutes * 60))
        sleepRemaining = minutes * 60
        let work = DispatchWorkItem { [weak self] in
            Task { @MainActor in self?.enforceSleepDeadline() }
        }
        sleepWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Double(minutes * 60), execute: work)
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.enforceSleepDeadline() }
        }
        // Continue updating when a finger is tracking a scroll view.
        RunLoop.main.add(timer, forMode: .common)
        sleepTimer = timer
    }

    func sleepAtSectionEnd() {
        guard let player, let section = currentSection else { return }
        cancelSleepTimer()
        let end = section.start + section.duration
        sleepSectionEnd = end
        let boundaryID = UUID()
        sleepBoundaryID = boundaryID
        let observation = PlayerTimeObservation(player: player)
        sleepBoundary = observation
        observation.token = player.addBoundaryTimeObserver(
            forTimes: [NSValue(time: CMTime(seconds: end, preferredTimescale: 600))], queue: .main
        ) { [weak self, weak player] in
            Task { @MainActor in
                guard let self, let player, self.player === player, self.sleepBoundaryID == boundaryID else { return }
                self.finishSleepTimer()
            }
        }
    }

    func enforceSleepDeadline() {
        guard let deadline = sleepDeadline else { return }
        sleepRemaining = max(0, Int(ceil(deadline.timeIntervalSinceNow)))
        if deadline <= Date() { finishSleepTimer() }
    }

    private func finishSleepTimer() {
        pause()
        cancelSleepTimer()
    }

    func cancelSleepTimer() {
        sleepWork?.cancel()
        sleepWork = nil
        sleepTimer?.invalidate()
        sleepTimer = nil
        sleepBoundaryID = UUID()
        sleepBoundary?.invalidate()
        sleepBoundary = nil
        sleepDeadline = nil
        sleepSectionEnd = nil
        sleepRemaining = 0
    }

    private func request(path: String, method: String = "GET", contentType: String? = nil, body: Data? = nil) async throws -> Data {
        let base = serverURL.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: base + path) else { throw LibraryError.message("The server URL is invalid.") }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        request.timeoutInterval = 120
        request.setValue("Bearer \(mobileToken)", forHTTPHeaderField: "Authorization")
        if let contentType { request.setValue(contentType, forHTTPHeaderField: "Content-Type") }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw LibraryError.message("Invalid server response.") }
        guard (200..<300).contains(http.statusCode) else {
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let detail = object?["detail"] as? String ?? object?["error"] as? String ?? "HTTP \(http.statusCode)"
            throw LibraryError.message(detail)
        }
        return data
    }
    private enum LibraryError: LocalizedError {
        case message(String)
        var errorDescription: String? { if case .message(let text) = self { return text }; return nil }
    }
}

struct ContentView: View {
    let model: AppModel
    let settings: SettingsStore
    @StateObject private var library = SyntheticLibrary()
    @State private var navigation: [String] = []
    @State private var transcriptJob: SyntheticJob?
    @State private var showingImporter = false
    @State private var showingSettings = false
    @State private var pdfReaderVisible = false
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        NavigationStack(path: $navigation) {
            libraryPage
                .navigationTitle("Library")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        Button { showingSettings = true } label: { Image(systemName: "slider.horizontal.3") }
                            .accessibilityLabel("Connection settings")
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { showingImporter = true } label: { Label("Add PDF", systemImage: "plus") }
                    }
                }
                .navigationDestination(for: String.self) { id in
                    PodcastDetail(jobID: id, library: library, pdfReaderVisible: $pdfReaderVisible) {
                        transcriptJob = $0
                    }
                }
        }
        .tint(PodcastPalette.cyan)
        .preferredColorScheme(.dark)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if let job = library.selectedJob, !pdfReaderVisible {
                MiniPlayer(job: job, library: library) { transcriptJob = job }
            }
        }
        .overlay(alignment: .top) {
            if let error = library.errorMessage {
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "exclamationmark.circle")
                    Text(error).font(.caption).fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    Button { library.errorMessage = nil } label: { Image(systemName: "xmark") }
                }
                .padding(14).background(PodcastPalette.panel, in: RoundedRectangle(cornerRadius: 14))
                .overlay(RoundedRectangle(cornerRadius: 14).stroke(PodcastPalette.danger.opacity(0.6)))
                .padding(12)
            }
        }
        #if targetEnvironment(macCatalyst)
        .background {
            MacPDFImporter(isPresented: $showingImporter) { result in
                switch result {
                case .success(let imported):
                    Task {
                        defer { imported.removeTemporaryCopy() }
                        await library.uploadPDF(imported.url)
                    }
                case .failure(let error):
                    let failure = error as NSError
                    library.errorMessage = "Could not import the PDF: \(error.localizedDescription) [\(failure.domain) \(failure.code)]"
                }
            }
            .frame(width: 0, height: 0)
            .allowsHitTesting(false)
        }
        #else
        .fileImporter(isPresented: $showingImporter, allowedContentTypes: [.pdf], allowsMultipleSelection: false) { result in
            switch result {
            case .success(let urls):
                if let url = urls.first { Task { await library.uploadPDF(url) } }
            case .failure(let error):
                library.errorMessage = "Could not open the PDF: \(error.localizedDescription)"
            }
        }
        #endif
        .sheet(isPresented: $showingSettings) {
            ConnectionSheet(library: library).presentationDetents([.medium, .large])
        }
        .fullScreenCover(item: $transcriptJob) { job in
            TranscriptPlayer(job: job, library: library)
        }
        .onChange(of: scenePhase) { _, phase in
            library.recordAudioDiagnostics("scene:\(String(describing: phase))")
            if phase == .active { library.enforceSleepDeadline() }
        }
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.didReceiveMemoryWarningNotification)) { _ in
            library.releaseUnusedExperiences(keeping: Set([navigation.last, transcriptJob?.id].compactMap { $0 }), limit: 0)
        }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            library.recordAudioDiagnostics("foreground")
            await library.loadJobs()
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(library.activeJobs.isEmpty ? 30 : 5)) } catch { return }
                await library.loadJobs(showErrors: false)
            }
        }
    }

    private var libraryPage: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 7) {
                    Text("PDF2PODCAST").font(.caption2.weight(.black)).tracking(2).foregroundStyle(PodcastPalette.cyan)
                    Text("Your library").font(.system(size: 36, weight: .heavy, design: .rounded)).tracking(-1.4)
                    Text("Papers worth listening to.").foregroundStyle(PodcastPalette.muted).font(.subheadline)
                }
                if library.isUploading {
                    HStack { ProgressView(); Text("Uploading PDF...").font(.subheadline) }
                }
                if !library.activeJobs.isEmpty {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Being made").font(.title3.weight(.bold))
                        ForEach(library.activeJobs) { ProgressCard(job: $0) }
                    }
                }
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Text("All conversions").font(.title3.weight(.bold))
                        Spacer()
                        Text("\(library.jobs.count) shared").font(.caption).foregroundStyle(PodcastPalette.muted)
                    }
                    if library.jobs.isEmpty {
                        ContentUnavailableView("Add your first PDF", systemImage: "doc.badge.plus",
                            description: Text("Finished podcasts and their chapters will appear here."))
                    }
                    ForEach(library.jobs) { job in
                        ConversionRow(job: job, library: library) {
                            if job.isComplete { navigation.append(job.id) }
                            else if job.isFailed { library.errorMessage = job.error ?? "This conversion was \(job.normalizedStatus)." }
                        }
                    }
                }
            }
            .padding(20)
        }
        .refreshable { await library.loadJobs() }
        .background(PodcastBackground())
    }
}

private struct PodcastBackground: View {
    var body: some View {
        ZStack {
            PodcastPalette.background
            LinearGradient(colors: [PodcastPalette.cyan.opacity(0.055), .clear],
                startPoint: .topTrailing, endPoint: .center)
        }.ignoresSafeArea()
    }
}

private struct ProgressCard: View {
    let job: SyntheticJob
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(job.title).font(.subheadline.weight(.semibold)).lineLimit(2)
                Spacer()
                Text("\(Int(job.progress))%").foregroundStyle(PodcastPalette.cyan).monospacedDigit()
            }
            Text(job.stage ?? job.normalizedStatus).font(.caption).foregroundStyle(PodcastPalette.muted)
            ProgressView(value: job.progress, total: 100).tint(PodcastPalette.cyan)
            HStack {
                Text("\(job.completedChunks ?? 0) / \(job.totalChunks.map(String.init) ?? "?") \(job.sectionCount == nil ? "steps" : job.readingMode == "paper_companion" ? "paragraphs" : "sections")")
                Spacer()
                Text(job.eta)
            }.font(.caption2).foregroundStyle(PodcastPalette.muted)
        }
        .padding(16).background(PodcastPalette.panel, in: RoundedRectangle(cornerRadius: 16))
    }
}

private struct AudioBars: View {
    let animated: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        TimelineView(.animation(minimumInterval: 0.12, paused: !animated || reduceMotion)) { context in
            HStack(alignment: .center, spacing: 3) {
                ForEach(0..<4) { index in
                    Capsule().fill(PodcastPalette.cyan)
                        .frame(width: 3, height: barHeight(index, at: context.date))
                }
            }
        }
        .frame(width: 24, height: 24)
        .accessibilityHidden(true)
    }
    private func barHeight(_ index: Int, at date: Date) -> CGFloat {
        if animated && !reduceMotion {
            let phase = date.timeIntervalSinceReferenceDate * 4.0 + Double(index) * 1.3
            return CGFloat(6.0 + 15.0 * abs(sin(phase)))
        }
        return CGFloat([9.0, 18.0, 13.0, 21.0][index])
    }
}

private struct PlayControl: View {
    let job: SyntheticJob
    @ObservedObject var library: SyntheticLibrary
    var diameter: CGFloat = 44
    var prominent = false

    private var current: Bool { library.isCurrent(job) }
    private var busy: Bool { current && library.isLoadingAudio }
    private var playing: Bool { current && library.isPlaying }
    var body: some View {
        Button { Task { await library.play(job) } } label: {
            ZStack {
                Circle().fill(prominent ? PodcastPalette.cyan : Color.white.opacity(0.08))
                if busy {
                    ProgressView().progressViewStyle(.circular)
                        .tint(prominent ? PodcastPalette.background : PodcastPalette.cyan)
                } else {
                    Image(systemName: playing ? "pause.fill" : "play.fill")
                        .font(.system(size: diameter > 48 ? 22 : 16, weight: .bold))
                        .foregroundStyle(prominent ? PodcastPalette.background : Color.white)
                }
            }.frame(width: diameter, height: diameter).contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(busy ? "Buffering. Tap to pause" : playing ? "Pause \(job.title)" : "Play \(job.title)")
        .accessibilityIdentifier("play-\(job.id)")
    }
}

private struct ConversionRow: View {
    let job: SyntheticJob
    @ObservedObject var library: SyntheticLibrary
    let open: () -> Void
    private var current: Bool { library.isCurrent(job) }

    var body: some View {
        HStack(spacing: 0) {
            Button(action: open) {
                HStack(spacing: 13) {
                    if current { AudioBars(animated: library.isPlaying) }
                    else {
                        Image(systemName: job.isFailed ? "exclamationmark.circle" : "waveform")
                            .font(.title3).foregroundStyle(job.isFailed ? PodcastPalette.danger : PodcastPalette.muted)
                            .frame(width: 24)
                    }
                    VStack(alignment: .leading, spacing: 5) {
                        Text(job.title).font(.subheadline.weight(.semibold))
                            .foregroundStyle(current ? PodcastPalette.cyan : Color.white)
                            .lineLimit(2).multilineTextAlignment(.leading)
                        Text(current
                            ? (library.isLoadingAudio ? "Buffering..." : library.isPlaying ? "Now playing" : "Paused")
                            : job.isFailed ? (job.error ?? job.normalizedStatus) : job.detail)
                            .font(.caption).foregroundStyle(job.isFailed ? PodcastPalette.danger : PodcastPalette.muted)
                            .lineLimit(1)
                    }
                    Spacer(minLength: 4)
                }
                .padding(.vertical, 15).padding(.leading, 14).padding(.trailing, 10)
                .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }.buttonStyle(.plain)
            if job.isComplete {
                PlayControl(job: job, library: library).padding(.trailing, 12)
            } else if job.isActive {
                Text("\(Int(job.progress))%").font(.caption.weight(.bold))
                    .foregroundStyle(PodcastPalette.cyan).padding(.trailing, 14)
            }
        }
        .background(current ? PodcastPalette.cyan.opacity(0.06) : PodcastPalette.panel, in: RoundedRectangle(cornerRadius: 15))
        .overlay(RoundedRectangle(cornerRadius: 15).stroke(current ? PodcastPalette.cyan.opacity(0.25) : PodcastPalette.line))
    }
}

private struct PodcastDetail: View {
    let jobID: String
    @ObservedObject var library: SyntheticLibrary
    @Binding var pdfReaderVisible: Bool
    let openTranscript: (SyntheticJob) -> Void
    @State private var searchText = ""
    @State private var selectedParagraph: SyntheticSection?
    private var job: SyntheticJob? { library.jobs.first { $0.id == jobID } }
    var body: some View {
        ScrollView {
            if let job {
                LazyVStack(alignment: .leading, spacing: 24) {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(job.readingMode == "paper_companion" ? "PAPER COMPANION" : "PODCAST")
                            .font(.caption2.weight(.bold)).tracking(2).foregroundStyle(PodcastPalette.cyan)
                        Text(job.title).font(.system(size: 30, weight: .heavy, design: .rounded)).tracking(-0.7)
                        Text(job.detail).font(.subheadline).foregroundStyle(PodcastPalette.muted)
                        if let filename = job.filename {
                            Text(filename).font(.caption).foregroundStyle(PodcastPalette.muted.opacity(0.8))
                        }
                    }
                    HStack(spacing: 14) {
                        PlayControl(job: job, library: library, diameter: 56, prominent: true)
                        Button { openTranscript(job) } label: {
                            Label("Read along", systemImage: "text.alignleft")
                                .font(.subheadline.weight(.semibold)).padding(.horizontal, 18).frame(height: 46)
                                .background(Color.white.opacity(0.06), in: Capsule())
                        }
                        #if targetEnvironment(macCatalyst)
                        NavigationLink {
                            MacPDFReadAlongView(
                                jobID: job.id, title: job.title, serverURL: library.serverURL,
                                mobileToken: library.mobileToken,
                                activeParagraphID: library.isCurrent(job) ? library.currentSection?.paragraphID : nil,
                                isPlaying: library.isCurrent(job) && library.isPlaying,
                                isBuffering: library.isCurrent(job) && library.isLoadingAudio,
                                onPlay: { time in Task { await library.play(job, at: time) } },
                                onTogglePlayback: { Task { await library.play(job) } }
                            )
                        } label: {
                            Label("Open PDF", systemImage: "doc.richtext")
                                .font(.subheadline.weight(.semibold)).padding(.horizontal, 18).frame(height: 46)
                                .background(Color.white.opacity(0.06), in: Capsule())
                        }
                        #else
                        NavigationLink {
                            IOSPDFReadAlongView(
                                jobID: job.id, title: job.title, serverURL: library.serverURL,
                                mobileToken: library.mobileToken,
                                activeParagraphID: library.isCurrent(job) ? library.currentSection?.paragraphID : nil,
                                isPlaying: library.isCurrent(job) && library.isPlaying,
                                isBuffering: library.isCurrent(job) && library.isLoadingAudio,
                                onPlay: { time in Task { await library.play(job, at: time) } },
                                onTogglePlayback: { Task { await library.play(job) } }
                            )
                            .onAppear { pdfReaderVisible = true }
                            .onDisappear { pdfReaderVisible = false }
                        } label: {
                            Label("Open PDF", systemImage: "doc.richtext")
                                .font(.subheadline.weight(.semibold)).padding(.horizontal, 18).frame(height: 46)
                                .background(Color.white.opacity(0.06), in: Capsule())
                        }
                        #endif
                        Spacer(minLength: 0)
                        SleepTimerMenu(library: library, compact: true)
                    }
                    if library.isCurrent(job) { PlaybackTimeline(library: library) }
                    if let experience = library.experiences[job.id] {
                        chapterList(experience.manifest, job: job)
                    } else if let error = library.experienceErrors[job.id] {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Chapters could not load: \(error)").font(.subheadline)
                            Button("Try again") { Task { await library.loadExperience(for: job) } }
                        }
                    } else {
                        HStack { ProgressView(); Text("Loading chapters...").font(.subheadline) }
                    }
                }.padding(20)
            }
        }
        .background(PodcastBackground())
        .navigationTitle("Podcast")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(item: $selectedParagraph) { section in
            if let job { ReadAlongParagraphView(section: section, job: job, library: library) }
        }
        .task(id: jobID) {
            if let job { await library.loadExperience(for: job) }
        }
    }

    private func chapterList(_ manifest: SyntheticManifest, job: SyntheticJob) -> some View {
        LazyVStack(alignment: .leading, spacing: 22) {
            Text("Chapters").font(.title2.weight(.bold))
            Text(manifest.version >= 4
                ? "A short summary explains why each paragraph matters, then the original text is read. Chapter titles are announced."
                : manifest.version >= 3 ? "Original paper text with a separate summary for each paragraph."
                : "Earlier recording format. Re-upload the PDF for original headings and paragraph-by-paragraph reading.")
                .font(.caption).foregroundStyle(PodcastPalette.muted)
            HStack(spacing: 10) {
                Image(systemName: "magnifyingglass").foregroundStyle(PodcastPalette.muted)
                TextField("Find a phrase from the paper", text: $searchText)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                if !searchText.isEmpty {
                    Button { searchText = "" } label: { Image(systemName: "xmark.circle.fill") }
                        .accessibilityLabel("Clear phrase search")
                }
            }
            .padding(13).background(PodcastPalette.panel, in: RoundedRectangle(cornerRadius: 12))
            if !searchText.isEmpty && manifest.chapters.allSatisfy({ matchingSections(in: $0).isEmpty }) {
                Text("No matching paragraph. Try a shorter phrase.").font(.subheadline).foregroundStyle(PodcastPalette.muted)
            }
            ForEach(manifest.chapters.filter { !matchingSections(in: $0).isEmpty }) { chapter in
                let chapterActive = library.isCurrent(job) && chapter.sections.contains { $0.id == library.currentSection?.id }
                LazyVStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(String(format: "%02d", chapter.n)).font(.caption.monospacedDigit())
                        Text(chapter.sourceHeading ?? chapter.title).font(.subheadline.weight(.bold))
                    }
                    .foregroundStyle(chapterActive ? PodcastPalette.cyan : Color.white)
                    ForEach(matchingSections(in: chapter)) { section in
                        let active = library.isCurrent(job) && library.currentSection?.id == section.id
                        Button { Task { await library.play(job, at: section.start) } } label: {
                            HStack(spacing: 11) {
                                if active && library.isLoadingAudio {
                                    ProgressView().tint(PodcastPalette.cyan).frame(width: 24, height: 24)
                                } else if active {
                                    AudioBars(animated: library.isPlaying)
                                } else {
                                    Image(systemName: "play.fill").font(.caption).frame(width: 24)
                                }
                                Text(section.title).font(.subheadline)
                                    .frame(maxWidth: .infinity, alignment: .leading).multilineTextAlignment(.leading)
                                Text(audioTime(section.duration)).font(.caption.monospacedDigit())
                                    .foregroundStyle(PodcastPalette.muted)
                            }
                            .foregroundStyle(active ? PodcastPalette.cyan : Color.white.opacity(0.85))
                            .padding(13)
                            .background(active ? PodcastPalette.cyan.opacity(0.1) : Color.white.opacity(0.025),
                                in: RoundedRectangle(cornerRadius: 12))
                        }.buttonStyle(.plain)
                        .accessibilityAddTraits(active ? .isSelected : [])
                        if section.sourceText != nil {
                            HStack(spacing: 14) {
                                Button { selectedParagraph = section } label: {
                                    Label("Paper text", systemImage: "doc.text.magnifyingglass")
                                }
                                if let summaryStart = section.summaryStart {
                                    Button { Task { await library.play(job, at: summaryStart) } } label: {
                                        Label("Summary", systemImage: "waveform")
                                    }.disabled(library.skipSummaries)
                                }
                            }.font(.caption.weight(.semibold)).padding(.horizontal, 8)
                            if let pages = section.pageLabel {
                                Text(pages).font(.caption2).foregroundStyle(PodcastPalette.muted).padding(.horizontal, 8)
                            }
                        }
                    }
                }
            }
        }
    }

    private func matchingSections(in chapter: SyntheticChapter) -> [SyntheticSection] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return chapter.sections }
        return chapter.sections.filter { section in
            [chapter.title, section.title, section.sourceText ?? "", section.spokenText ?? "", section.summary ?? ""]
                .contains { $0.localizedCaseInsensitiveContains(query) }
        }
    }
}

private struct ReadAlongParagraphView: View {
    let section: SyntheticSection
    let job: SyntheticJob
    @ObservedObject var library: SyntheticLibrary
    @Environment(\.dismiss) private var dismiss
    @AppStorage("pdf2podcast.transcriptTextSize") private var textSize = 30.0

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text(section.title).font(.title2.weight(.bold))
                    if let pages = section.pageLabel {
                        Text(pages).font(.caption).foregroundStyle(PodcastPalette.muted)
                    }
                    HStack {
                        Button { Task { await library.play(job, at: section.sourceStart ?? section.start) } } label: {
                            Label("Read paragraph", systemImage: "play.fill")
                        }
                        Spacer()
                        Button {
                            UIPasteboard.general.string = section.sourceText ?? section.spokenText ?? ""
                        } label: { Label("Copy text", systemImage: "doc.on.doc") }
                    }.font(.subheadline.weight(.semibold))
                    if let summary = section.summary, !summary.isEmpty {
                        VStack(alignment: .leading, spacing: 12) {
                            Text("SUMMARY").font(.caption2.weight(.bold)).tracking(1)
                            Text(summary).font(.system(size: max(18, textSize * 0.8))).textSelection(.enabled)
                            if let start = section.summaryStart {
                                Button { Task { await library.play(job, at: start) } } label: {
                                    Label("Listen to summary", systemImage: "play.fill")
                                }.font(.subheadline.weight(.semibold)).disabled(library.skipSummaries)
                            }
                        }
                        .padding(18).foregroundStyle(PodcastPalette.cyan)
                        .background(PodcastPalette.cyan.opacity(0.06), in: RoundedRectangle(cornerRadius: 14))
                    }
                    Text(section.contentKind == "caption" ? "FIGURE CAPTION" : "PAPER TEXT")
                        .font(.caption2.weight(.bold)).tracking(1.5).foregroundStyle(PodcastPalette.muted)
                    Text(section.sourceText ?? section.spokenText ?? "")
                        .font(.system(size: textSize, design: .serif))
                        .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    Text("Paper wording is preserved here. Spoken text only normalizes reading artifacts, citations and simple numeric expressions.")
                        .font(.caption).foregroundStyle(PodcastPalette.muted)
                }.padding(22)
            }
            .background(PodcastBackground())
            .navigationTitle("Read paragraph").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .safeAreaInset(edge: .bottom) {
                HStack {
                    Image(systemName: "textformat.size.smaller")
                    Slider(value: $textSize, in: 20...46, step: 2).accessibilityLabel("Paper text size")
                    Image(systemName: "textformat.size.larger")
                    if library.isCurrent(job) { PlayControl(job: job, library: library) }
                }.padding().background(.ultraThinMaterial)
            }
        }.tint(PodcastPalette.cyan).preferredColorScheme(.dark)
    }
}

private struct MiniPlayer: View {
    let job: SyntheticJob
    @ObservedObject var library: SyntheticLibrary
    let openPlayer: () -> Void
    var body: some View {
        ZStack(alignment: .trailing) {
            // A real, full-width button behind the controls covers every bit of padding.
            Button(action: openPlayer) {
                HStack(spacing: 12) {
                    AudioBars(animated: library.isPlaying)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(job.title).font(.caption.weight(.bold)).lineLimit(1)
                        Text(library.isLoadingAudio ? "Buffering audio..." : "Tap for transcript")
                            .font(.caption2).foregroundStyle(PodcastPalette.muted)
                    }
                    Spacer(minLength: 0)
                }
                .padding(.leading, 16).padding(.trailing, 118)
                .frame(maxWidth: .infinity, minHeight: 72).contentShape(Rectangle())
            }
            .buttonStyle(.plain).accessibilityLabel("Open transcript for \(job.title)")
            HStack(spacing: 4) {
                Button { library.skip(seconds: -15) } label: {
                    Image(systemName: "gobackward.15").font(.title3).frame(width: 44, height: 48)
                }.buttonStyle(.plain).accessibilityLabel("Back 15 seconds")
                PlayControl(job: job, library: library)
            }.padding(.trailing, 14)
        }
        .background(.ultraThinMaterial)
        .overlay(alignment: .top) {
            MiniPlaybackProgress(clock: library.clock)
        }
    }
}

private struct MiniPlaybackProgress: View {
    @ObservedObject var clock: PlaybackClock
    var body: some View {
        GeometryReader { geometry in
            Rectangle().fill(PodcastPalette.cyan)
                .frame(width: geometry.size.width * min(1, max(0, clock.duration > 0 ? clock.currentTime / clock.duration : 0)), height: 2)
        }.frame(height: 2)
    }
}

@MainActor
private struct PlaybackTimeline: View {
    let library: SyntheticLibrary
    @ObservedObject private var clock: PlaybackClock
    @State private var dragging = false
    @State private var fraction = 0.0

    init(library: SyntheticLibrary) {
        self.library = library
        _clock = ObservedObject(wrappedValue: library.clock)
    }
    var body: some View {
        VStack(spacing: 0) {
            Slider(value: Binding(
                get: { dragging ? fraction : (library.duration > 0 ? min(1, max(0, library.currentTime / library.duration)) : 0) },
                set: { fraction = $0 }
            ), in: 0...1) { editing in
                if editing {
                    fraction = library.duration > 0 ? min(1, library.currentTime / library.duration) : 0
                    dragging = true
                } else {
                    dragging = false
                    library.seek(fraction: fraction)
                }
            }
            .tint(PodcastPalette.cyan).disabled(library.duration <= 0)
            .accessibilityLabel("Playback position")
            HStack {
                Text(audioTime(dragging ? fraction * library.duration : library.currentTime))
                Spacer()
                Text(audioTime(library.duration))
            }.font(.caption2.monospacedDigit()).foregroundStyle(PodcastPalette.muted)
        }
    }
}

@MainActor
private struct SleepTimerMenu: View {
    @ObservedObject var library: SyntheticLibrary
    @ObservedObject private var clock: PlaybackClock
    var compact = false

    init(library: SyntheticLibrary, compact: Bool = false) {
        self.library = library
        self.compact = compact
        _clock = ObservedObject(wrappedValue: library.clock)
    }
    var body: some View {
        Menu {
            ForEach([5, 10, 15, 30, 45, 60], id: \.self) { minutes in
                Button("\(minutes) minutes") { library.setSleepTimer(minutes: minutes) }
            }
            Button("At end of current section") { library.sleepAtSectionEnd() }
                .disabled(library.currentSection == nil)
            if library.sleepIsActive {
                Divider()
                Button("Turn off timer", role: .destructive) { library.cancelSleepTimer() }
            }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: library.sleepIsActive ? "moon.zzz.fill" : "moon.zzz")
                if !compact || library.sleepIsActive { Text(library.sleepLabel).font(.caption.monospacedDigit()) }
            }
            .foregroundStyle(library.sleepIsActive ? PodcastPalette.cyan : PodcastPalette.muted)
            .frame(minWidth: 44, minHeight: 44)
        }.accessibilityLabel(library.sleepLabel)
    }
}

private struct PlaybackOptionsMenu: View {
    @ObservedObject var library: SyntheticLibrary
    var body: some View {
        Menu {
            Toggle("Skip summaries", isOn: $library.skipSummaries)
            Text("Chapter titles still play.")
        } label: {
            Image(systemName: library.skipSummaries ? "text.badge.minus" : "slider.horizontal.3")
                .frame(width: 44, height: 44)
        }
        .accessibilityLabel("Playback options. Skip summaries \(library.skipSummaries ? "on" : "off")")
    }
}

private struct TranscriptPlayer: View {
    let job: SyntheticJob
    @ObservedObject var library: SyntheticLibrary
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @AppStorage("pdf2podcast.transcriptTextSize") private var textSize = 30.0
    @State private var followsPlayback = true
    @State private var showingTextSize = false
    @State private var centerRequest = 0
    private var experience: ListeningExperience? { library.experiences[job.id] }
    private var activeLine: Int { library.lineIndex(for: job) }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Button { dismiss() } label: {
                    Image(systemName: "chevron.down").font(.headline).frame(width: 44, height: 44)
                }.accessibilityLabel("Back to podcast")
                VStack(alignment: .leading, spacing: 3) {
                    Text("READ ALONG").font(.caption2.weight(.bold)).tracking(1.5).foregroundStyle(PodcastPalette.cyan)
                    Text(job.title).font(.caption.weight(.semibold)).lineLimit(1)
                }
                Spacer(minLength: 0)
                Button { showingTextSize.toggle() } label: {
                    Image(systemName: "textformat.size").frame(width: 44, height: 44)
                }.accessibilityLabel("Transcript text size")
                PlaybackOptionsMenu(library: library)
            }.padding(.horizontal, 10).padding(.top, 4)

            if showingTextSize {
                HStack(spacing: 14) {
                    Image(systemName: "textformat.size.smaller")
                    Slider(value: $textSize, in: 20...46, step: 2).accessibilityLabel("Transcript font size")
                    Image(systemName: "textformat.size.larger")
                    Text("\(Int(textSize))").font(.caption.monospacedDigit()).frame(width: 26)
                }
                .padding(.horizontal, 24).padding(.bottom, 10)
                .foregroundStyle(PodcastPalette.muted)
            }
            transcriptBody
            playerControls
        }
        .background(PodcastBackground()).tint(PodcastPalette.cyan).preferredColorScheme(.dark)
        .task { await library.loadExperience(for: job) }
    }

    @ViewBuilder
    private var transcriptBody: some View {
        if let experience {
            RecycledTranscript(
                experience: experience, activeLine: activeLine, textSize: textSize,
                followsPlayback: followsPlayback, centerRequest: centerRequest,
                seekRevision: library.isCurrent(job) ? library.seekRevision : 0,
                reduceMotion: reduceMotion,
                onScrollAway: { followsPlayback = false },
                onSelect: { line in
                    followsPlayback = true
                    Task { await library.play(job, at: line.t) }
                    centerRequest += 1
                }
            )
            .overlay(alignment: .bottom) {
                if !followsPlayback {
                    Button {
                        followsPlayback = true
                        centerRequest += 1
                    } label: {
                        Label("Recenter", systemImage: "scope")
                            .font(.subheadline.weight(.bold))
                            .padding(.horizontal, 18).padding(.vertical, 12)
                            .foregroundStyle(PodcastPalette.background)
                            .background(PodcastPalette.cyan, in: Capsule())
                            .shadow(color: .black.opacity(0.4), radius: 12, y: 4)
                    }
                    .padding(.bottom, 14).accessibilityIdentifier("recenter-transcript")
                }
            }
        } else if let error = library.experienceErrors[job.id] {
            VStack(spacing: 16) {
                Text("Transcript couldn't load").font(.headline)
                Text(error).font(.subheadline).foregroundStyle(PodcastPalette.muted)
                Button("Try again") { Task { await library.loadExperience(for: job) } }
            }.padding(24).frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ProgressView("Loading transcript...").frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private var playerControls: some View {
        VStack(spacing: 8) {
            if library.skipSummaries {
                Text("Summaries skipped").font(.caption2).foregroundStyle(PodcastPalette.muted)
            }
            if library.isCurrent(job) {
                if let section = library.currentSection {
                    Text(section.title).font(.caption).foregroundStyle(PodcastPalette.cyan).lineLimit(1)
                }
                PlaybackTimeline(library: library)
            }
            HStack(spacing: 0) {
                Button { library.cyclePlaybackRate() } label: {
                    Text(library.rateLabel).font(.caption.weight(.bold)).frame(width: 48, height: 44)
                }.accessibilityLabel("Playback speed \(library.rateLabel)")
                Spacer(minLength: 2)
                Button { library.skip(seconds: -15) } label: {
                    Image(systemName: "gobackward.15").font(.title2).frame(width: 44, height: 44)
                }.disabled(!library.isCurrent(job)).accessibilityLabel("Back 15 seconds")
                Spacer(minLength: 2)
                PlayControl(job: job, library: library, diameter: 56, prominent: true)
                Spacer(minLength: 2)
                Button { library.skip(seconds: 15) } label: {
                    Image(systemName: "goforward.15").font(.title2).frame(width: 44, height: 44)
                }.disabled(!library.isCurrent(job)).accessibilityLabel("Forward 15 seconds")
                Spacer(minLength: 2)
                SleepTimerMenu(library: library, compact: true)
            }
        }
        .padding(.horizontal, 18).padding(.vertical, 14)
        .background(.ultraThinMaterial)
    }
}

// Reuse a handful of native cells instead of retaining a book of laid-out text.
@MainActor
private struct RecycledTranscript: UIViewRepresentable {
    let experience: ListeningExperience
    let activeLine: Int
    let textSize: Double
    let followsPlayback: Bool
    let centerRequest: Int
    let seekRevision: Int
    let reduceMotion: Bool
    let onScrollAway: () -> Void
    let onSelect: (SyntheticTranscriptLine) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    func makeUIView(context: Context) -> UITableView {
        let table = UITableView(frame: .zero, style: .plain)
        table.backgroundColor = .clear
        table.separatorStyle = .none
        table.showsVerticalScrollIndicator = false
        table.rowHeight = UITableView.automaticDimension
        table.estimatedRowHeight = 300
        table.contentInset = UIEdgeInsets(top: 96, left: 0, bottom: 180, right: 0)
        table.register(UITableViewCell.self, forCellReuseIdentifier: "paragraph")
        table.dataSource = context.coordinator
        table.delegate = context.coordinator
        return table
    }

    func updateUIView(_ table: UITableView, context: Context) {
        let coordinator = context.coordinator
        let previous = coordinator.parent
        let reload = !coordinator.hasLoaded || previous.experience.id != experience.id || previous.textSize != textSize
        let activeChanged = previous.activeLine != activeLine
        let seekChanged = previous.seekRevision != seekRevision
        let centerChanged = previous.centerRequest != centerRequest
        coordinator.parent = self
        coordinator.hasLoaded = true
        coordinator.scrollGeneration += 1
        if reload {
            table.reloadData()
        } else if activeChanged {
            for index in table.indexPathsForVisibleRows ?? [] where index.row == previous.activeLine || index.row == activeLine {
                if let cell = table.cellForRow(at: index) { coordinator.configure(cell, at: index) }
            }
        }
        guard followsPlayback, reload || activeChanged || seekChanged || centerChanged,
              experience.lines.indices.contains(activeLine) else { return }
        let generation = coordinator.scrollGeneration
        // Wait for the updated row heights; discard any scroll superseded by a drag/seek.
        DispatchQueue.main.async { [weak table, weak coordinator] in
            guard let table, let coordinator, coordinator.scrollGeneration == generation,
                  coordinator.parent.followsPlayback else { return }
            table.layoutIfNeeded()
            table.scrollToRow(at: IndexPath(row: activeLine, section: 0), at: .middle,
                animated: !reduceMotion && !reload && !seekChanged)
        }
    }

    static func dismantleUIView(_ table: UITableView, coordinator: Coordinator) {
        coordinator.scrollGeneration += 1
        table.delegate = nil
        table.dataSource = nil
    }

    final class Coordinator: NSObject, UITableViewDataSource, UITableViewDelegate {
        var parent: RecycledTranscript
        var hasLoaded = false
        var scrollGeneration = 0
        init(parent: RecycledTranscript) { self.parent = parent }

        func tableView(_ tableView: UITableView, numberOfRowsInSection section: Int) -> Int {
            parent.experience.lines.count
        }

        func tableView(_ tableView: UITableView, cellForRowAt indexPath: IndexPath) -> UITableViewCell {
            let cell = tableView.dequeueReusableCell(withIdentifier: "paragraph", for: indexPath)
            configure(cell, at: indexPath)
            return cell
        }

        func configure(_ cell: UITableViewCell, at index: IndexPath) {
            guard parent.experience.lines.indices.contains(index.row) else { return }
            let line = parent.experience.lines[index.row]
            let active = index.row == parent.activeLine
            let size = parent.textSize
            cell.backgroundColor = .clear
            cell.selectionStyle = .none
            cell.contentConfiguration = UIHostingConfiguration {
                TranscriptParagraph(line: line, active: active, textSize: size)
                    .padding(.horizontal, 22).padding(.vertical, 11)
            }.margins(.all, 0)
        }

        func tableView(_ tableView: UITableView, didSelectRowAt indexPath: IndexPath) {
            guard parent.experience.lines.indices.contains(indexPath.row) else { return }
            tableView.deselectRow(at: indexPath, animated: false)
            parent.onSelect(parent.experience.lines[indexPath.row])
        }

        func scrollViewWillBeginDragging(_ scrollView: UIScrollView) {
            scrollGeneration += 1
            parent.onScrollAway()
        }
    }
}

private struct TranscriptParagraph: View {
    let line: SyntheticTranscriptLine
    let active: Bool
    let textSize: Double
    private var guide: Bool { line.speaker == "guide" }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            if guide || line.paragraphID != nil {
                HStack {
                    Text(line.kind == "chapter_title" ? "CHAPTER TITLE" : guide ? "SUMMARY" : line.kind == "caption" ? "FIGURE CAPTION" : "PAPER TEXT")
                    if let page = line.pageStart { Text("P. \(page)") }
                }
                .font(.caption2.weight(.bold)).tracking(1.2).foregroundStyle(PodcastPalette.cyan)
            }
            Text(line.displayText)
                .font(.system(size: guide ? textSize * 0.88 : textSize, weight: .bold, design: .rounded))
                .tracking(-0.5).fixedSize(horizontal: false, vertical: true)
                .foregroundStyle(active
                    ? (guide ? PodcastPalette.cyan : Color.white)
                    : (guide ? PodcastPalette.cyan.opacity(0.45) : Color.white.opacity(0.34)))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(guide ? 16 : 0)
        .background(guide ? PodcastPalette.cyan.opacity(0.06) : Color.clear,
            in: RoundedRectangle(cornerRadius: 14))
        .accessibilityAddTraits(active ? .isSelected : [])
        .contextMenu {
            Button { UIPasteboard.general.string = line.displayText } label: {
                Label(line.kind == "chapter_title" ? "Copy chapter title" : guide ? "Copy summary" : "Copy paper text", systemImage: "doc.on.doc")
            }
        }
    }
}

private struct ConnectionSheet: View {
    @ObservedObject var library: SyntheticLibrary
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Toggle("Skip summaries", isOn: $library.skipSummaries)
                } header: {
                    Text("Listening")
                } footer: {
                    Text("Off by default. Skips paragraph summaries, but keeps chapter titles and original paper text.")
                }
                Section("Pdf2Podcast server") {
                    TextField("Server URL", text: $library.serverURL)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    SecureField("Mobile token", text: $library.mobileToken)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                }
            }
            .navigationTitle("Connection")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Connect") {
                        library.saveConnection()
                        Task { await library.loadJobs() }
                        dismiss()
                    }.fontWeight(.bold)
                }
            }
        }.preferredColorScheme(.dark)
    }
}

#Preview {
    ContentView(model: AppModel(settings: SettingsStore()), settings: SettingsStore())
}
