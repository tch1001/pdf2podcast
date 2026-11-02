// Presents the original paper as a touch-driven companion to the shared audio player.
#if os(iOS) && !targetEnvironment(macCatalyst)
import CryptoKit
import PDFKit
import SwiftUI
import UIKit

private struct IOSPDFReadAlongMap: Decodable, Sendable {
    let version: Int
    let revision: String
    let coordinateSpace: String
    let pageCount: Int
    let unmappedParagraphs: Int
    let paragraphs: [IOSPDFReadAlongParagraph]
    let timeline: PDFReadAlongIntervalIndex

    enum CodingKeys: String, CodingKey {
        case version, revision, paragraphs
        case coordinateSpace = "coordinate_space"
        case pageCount = "page_count"
        case unmappedParagraphs = "unmapped_paragraphs"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        version = try values.decode(Int.self, forKey: .version)
        revision = try values.decode(String.self, forKey: .revision)
        coordinateSpace = try values.decode(String.self, forKey: .coordinateSpace)
        pageCount = try values.decode(Int.self, forKey: .pageCount)
        unmappedParagraphs = try values.decode(Int.self, forKey: .unmappedParagraphs)
        let decodedParagraphs = try values.decode([IOSPDFReadAlongParagraph].self, forKey: .paragraphs)
        paragraphs = decodedParagraphs
        // Older maps may omit end. Only their paragraph highlight uses the next start.
        let ordered = decodedParagraphs.indices.sorted { decodedParagraphs[$0].start < decodedParagraphs[$1].start }
        timeline = PDFReadAlongIntervalIndex(ordered.enumerated().map { position, index in
            let nextStart = position + 1 < ordered.count
                ? decodedParagraphs[ordered[position + 1]].start : Double.greatestFiniteMagnitude
            return .init(start: decodedParagraphs[index].start,
                         end: decodedParagraphs[index].end ?? nextStart, index: index)
        })
    }

    func paragraph(at time: Double) -> IOSPDFReadAlongParagraph? {
        timeline.index(at: time).map { paragraphs[$0] }
    }

}

private struct IOSPDFReadAlongParagraph: Decodable, Identifiable, Sendable {
    let id: String
    let title: String
    let chapter: String
    let start: Double
    let summaryStart: Double?
    let end: Double?
    let kind: String?
    let anchors: [IOSPDFReadAlongAnchor]
    let words: [IOSPDFReadAlongWord]
    private let wordTimeline: PDFReadAlongIntervalIndex

    enum CodingKeys: String, CodingKey {
        case id, title, chapter, start, end, kind, anchors, words
        case summaryStart = "summary_start"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        title = try values.decode(String.self, forKey: .title)
        chapter = try values.decode(String.self, forKey: .chapter)
        start = try values.decode(Double.self, forKey: .start)
        summaryStart = try values.decodeIfPresent(Double.self, forKey: .summaryStart)
        end = try values.decodeIfPresent(Double.self, forKey: .end)
        kind = try values.decodeIfPresent(String.self, forKey: .kind)
        anchors = try values.decode([IOSPDFReadAlongAnchor].self, forKey: .anchors)
        words = try values.decodeIfPresent([IOSPDFReadAlongWord].self, forKey: .words) ?? []
        wordTimeline = PDFReadAlongIntervalIndex(words.enumerated().map { index, word in
            .init(start: word.start, end: word.end, index: index)
        })
    }

    func wordIndex(at time: Double) -> Int? {
        guard start.isFinite, time >= start,
              end.map({ $0.isFinite && time < $0 }) ?? true,
              summaryStart.map({ $0.isFinite && time < $0 }) ?? true else { return nil }
        // Never extend a word through silence or a summary, or synthesize missing timing.
        return wordTimeline.index(at: time)
    }
}

private struct IOSPDFReadAlongWord: Decodable, Sendable {
    let text: String
    let start: Double
    let end: Double
    let anchors: [IOSPDFReadAlongAnchor]
}

private struct IOSPDFReadAlongAnchor: Decodable, Sendable {
    let page: Int
    let rects: [[Double]]

    var rectangles: [CGRect] {
        rects.compactMap {
            guard $0.count == 4, $0.allSatisfy(\.isFinite), $0[2] > 0, $0[3] > 0 else { return nil }
            return CGRect(x: $0[0], y: $0[1], width: $0[2], height: $0[3])
        }
    }
}

private enum IOSPDFReadAlongError: LocalizedError {
    case message(String)

    var errorDescription: String? {
        switch self { case .message(let message): return message }
    }
}

private final class IOSPDFRedirectGuard: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        guard let original = task.originalRequest?.url, let next = request.url,
              original.scheme == next.scheme, original.host == next.host, original.port == next.port else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }
}

@MainActor
private final class IOSPDFReadAlongLoader: ObservableObject {
    @Published var map: IOSPDFReadAlongMap?
    @Published var fileURL: URL?
    @Published var error: String?
    @Published var loading = false

    func load(server: String, token: String, jobID: String) async {
        loading = true
        error = nil
        map = nil
        fileURL = nil
        defer { loading = false }

        let session = URLSession(
            configuration: .ephemeral,
            delegate: IOSPDFRedirectGuard(),
            delegateQueue: nil
        )
        defer { session.invalidateAndCancel() }

        do {
            let base = server.trimmingCharacters(in: .whitespacesAndNewlines)
                .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            func request(_ artifact: String) throws -> URLRequest {
                guard let url = URL(string: base + "/api/audio/jobs/" + jobID + "/" + artifact),
                      ["http", "https"].contains(url.scheme?.lowercased() ?? ""),
                      url.host != nil else {
                    throw IOSPDFReadAlongError.message("The server URL is invalid.")
                }
                var request = URLRequest(url: url)
                request.timeoutInterval = 120
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                return request
            }

            let (mapData, mapResponse) = try await session.data(for: request("read-along"))
            guard let mapHTTP = mapResponse as? HTTPURLResponse else {
                throw IOSPDFReadAlongError.message("The paragraph map returned an invalid response.")
            }
            guard mapHTTP.statusCode == 200 else {
                let details = (try? JSONSerialization.jsonObject(with: mapData)) as? [String: Any]
                let reason = details?["detail"] as? String
                    ?? details?["error"] as? String
                    ?? "Paragraph locations are not available for this conversion."
                throw IOSPDFReadAlongError.message("\(reason) (HTTP \(mapHTTP.statusCode))")
            }
            let mapping = try JSONDecoder().decode(IOSPDFReadAlongMap.self, from: mapData)
            guard [1, 2].contains(mapping.version), mapping.coordinateSpace == "pdf_user_space" else {
                throw IOSPDFReadAlongError.message("This paragraph map uses an unsupported coordinate format.")
            }
            try Task.checkCancellation()

            let cacheKey = SHA256.hash(data: Data((base + "|" + jobID + "|" + mapping.revision).utf8))
                .map { String(format: "%02x", $0) }.joined()
            let directory = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("Pdf2PodcastReadAlong", isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let destination = directory.appendingPathComponent(cacheKey + ".pdf")

            if !FileManager.default.fileExists(atPath: destination.path) {
                let (temporary, pdfResponse) = try await session.download(for: request("pdf"))
                defer { try? FileManager.default.removeItem(at: temporary) }
                guard let pdfHTTP = pdfResponse as? HTTPURLResponse, pdfHTTP.statusCode == 200,
                      pdfHTTP.mimeType?.lowercased() == "application/pdf" else {
                    let status = (pdfResponse as? HTTPURLResponse)?.statusCode ?? 0
                    throw IOSPDFReadAlongError.message("The original PDF could not download (HTTP \(status)).")
                }
                try Task.checkCancellation()
                if !FileManager.default.fileExists(atPath: destination.path) {
                    try FileManager.default.moveItem(at: temporary, to: destination)
                }
            }

            guard let document = PDFDocument(url: destination), !document.isLocked, document.pageCount > 0 else {
                try? FileManager.default.removeItem(at: destination)
                throw IOSPDFReadAlongError.message("The downloaded PDF could not be opened, or requires a password.")
            }
            try Task.checkCancellation()
            map = mapping
            fileURL = destination
        } catch is CancellationError {
            return
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct IOSPDFReadAlongView: View {
    let jobID: String
    let title: String
    let serverURL: String
    let mobileToken: String
    let activeParagraphID: String?
    let isPlaying: Bool
    let isBuffering: Bool
    let onPlay: (Double) -> Void
    let onTogglePlayback: () -> Void

    // The player supplies absolute seconds; nil preserves legacy paragraph-only callers.
    var currentTime: Double? = nil

    private var highlightedParagraphID: String? {
        guard let currentTime else { return activeParagraphID }
        return loader.map?.paragraph(at: currentTime)?.id
    }

    private var highlightedWordIndex: Int? {
        guard let currentTime, !isBuffering else { return nil }
        return loader.map?.paragraph(at: currentTime)?.wordIndex(at: currentTime)
    }

    private var highlightedWord: IOSPDFReadAlongWord? {
        guard let currentTime, let index = highlightedWordIndex,
              let paragraph = loader.map?.paragraph(at: currentTime) else { return nil }
        return paragraph.words[index]
    }

    @StateObject private var loader = IOSPDFReadAlongLoader()
    @State private var centerRevision = 0
    @State private var followingAudio = true
    @State private var reloadRevision = 0
    @Environment(\.verticalSizeClass) private var verticalSizeClass

    private var isLandscape: Bool { verticalSizeClass == .compact }

    private var activeParagraph: IOSPDFReadAlongParagraph? {
        loader.map?.paragraphs.first { $0.id == highlightedParagraphID }
    }

    var body: some View {
        Group {
            if let map = loader.map, let fileURL = loader.fileURL {
                VStack(spacing: 0) {
                    if !isLandscape {
                        HStack(spacing: 10) {
                            Label(
                                followingAudio ? "Following audio" : "Browsing PDF",
                                systemImage: followingAudio ? "location.fill" : "hand.draw"
                            )
                            .foregroundStyle(followingAudio ? Color.cyan : Color.secondary)
                            Spacer(minLength: 8)
                            Text("\(map.pageCount) pages / \(map.paragraphs.count) playable")
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                        .font(.caption)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 9)
                        .background(.ultraThinMaterial)
                    }

                    IOSPDFParagraphCanvas(
                        fileURL: fileURL,
                        map: map,
                        activeParagraphID: highlightedParagraphID,
                        activeWord: highlightedWord, activeWordIndex: highlightedWordIndex,
                        isPlaying: isPlaying,
                        isBuffering: isBuffering,
                        centerRevision: centerRevision,
                        onPlay: onPlay,
                        onTogglePlayback: onTogglePlayback,
                        onFollowingChanged: { followingAudio = $0 },
                        onError: { loader.error = $0; loader.fileURL = nil }
                    )
                }
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    if !isLandscape {
                        playbackBar(map: map)
                    }
                }
            } else if let error = loader.error {
                ContentUnavailableView {
                    Label("PDF read-along unavailable", systemImage: "doc.questionmark")
                } description: {
                    Text(error)
                } actions: {
                    Button("Try again") { reloadRevision += 1 }
                }
            } else {
                ProgressView("Opening PDF and matching paragraphs...")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(red: 0.032, green: 0.045, blue: 0.059))
        .navigationTitle("PDF read-along")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { centerRevision += 1 } label: {
                    Label("Recenter", systemImage: "scope")
                }
                .disabled(highlightedParagraphID == nil)
            }
        }
        .toolbar(isLandscape ? .hidden : .visible, for: .navigationBar)
        .task(id: "\(jobID)|\(serverURL)|\(reloadRevision)") {
            await loader.load(server: serverURL, token: mobileToken, jobID: jobID)
        }
    }

    private func playbackBar(map: IOSPDFReadAlongMap) -> some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 3) {
                Text(activeParagraph?.chapter ?? title)
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.cyan)
                    .lineLimit(1)
                Text(activeParagraph?.title ?? "Tap a play marker to begin")
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(2)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Button(action: onTogglePlayback) {
                ZStack {
                    Circle().fill(Color.cyan)
                    if isBuffering {
                        ProgressView().tint(Color(red: 0.032, green: 0.045, blue: 0.059))
                    } else {
                        Image(systemName: isPlaying ? "pause.fill" : "play.fill")
                            .font(.headline.weight(.bold))
                            .foregroundStyle(Color(red: 0.032, green: 0.045, blue: 0.059))
                    }
                }
                .frame(width: 48, height: 48)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(isBuffering ? "Buffering" : isPlaying ? "Pause" : "Play")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 11)
        .background(.ultraThinMaterial)
    }
}

private final class IOSParagraphPlayAnnotation: PDFAnnotation {
    let paragraph: IOSPDFReadAlongParagraph
    var active: Bool
    var playing: Bool
    var buffering: Bool

    init(
        bounds: CGRect,
        paragraph: IOSPDFReadAlongParagraph,
        active: Bool,
        playing: Bool,
        buffering: Bool
    ) {
        self.paragraph = paragraph
        self.active = active
        self.playing = playing
        self.buffering = buffering
        super.init(bounds: bounds, forType: .stamp, withProperties: nil)
        contents = "Play this paragraph: " + paragraph.title
        shouldPrint = false
    }

    required init?(coder: NSCoder) { fatalError("In-memory annotation only") }

    override func draw(with box: PDFDisplayBox, in context: CGContext) {
        context.saveGState()
        defer { context.restoreGState() }
        let circle = bounds.insetBy(dx: 0.5, dy: 0.5)
        let color = active ? UIColor.systemTeal : UIColor(red: 0.08, green: 0.28, blue: 0.35, alpha: 1)
        context.setFillColor(color.cgColor)
        context.fillEllipse(in: circle)
        context.setFillColor(UIColor.white.cgColor)

        if buffering {
            for x in [0.3, 0.5, 0.7] {
                context.fillEllipse(in: CGRect(
                    x: bounds.minX + bounds.width * x - 1,
                    y: bounds.midY - 1,
                    width: 2,
                    height: 2
                ))
            }
        } else if active && playing {
            let width = bounds.width * 0.13
            let height = bounds.height * 0.46
            context.fill(CGRect(x: bounds.minX + bounds.width * 0.35, y: bounds.midY - height / 2,
                                width: width, height: height))
            context.fill(CGRect(x: bounds.minX + bounds.width * 0.58, y: bounds.midY - height / 2,
                                width: width, height: height))
        } else {
            context.move(to: CGPoint(x: bounds.minX + bounds.width * 0.39, y: bounds.minY + bounds.height * 0.27))
            context.addLine(to: CGPoint(x: bounds.minX + bounds.width * 0.73, y: bounds.midY))
            context.addLine(to: CGPoint(x: bounds.minX + bounds.width * 0.39, y: bounds.minY + bounds.height * 0.73))
            context.closePath()
            context.fillPath()
        }
    }
}

private struct IOSPDFParagraphCanvas: UIViewRepresentable {
    let fileURL: URL
    let map: IOSPDFReadAlongMap
    let activeParagraphID: String?
    let activeWord: IOSPDFReadAlongWord?
    let activeWordIndex: Int?
    let isPlaying: Bool
    let isBuffering: Bool
    let centerRevision: Int
    let onPlay: (Double) -> Void
    let onTogglePlayback: () -> Void
    let onFollowingChanged: (Bool) -> Void
    let onError: (String) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    func makeUIView(context: Context) -> PDFView {
        let view = PDFView()
        view.displayMode = .singlePageContinuous
        view.displayDirection = .vertical
        view.displayBox = .cropBox
        view.displaysPageBreaks = true
        view.pageBreakMargins = UIEdgeInsets(top: 10, left: 8, bottom: 10, right: 8)
        view.autoScales = true
        view.isInMarkupMode = false
        view.isUserInteractionEnabled = true
        view.backgroundColor = UIColor(white: 0.12, alpha: 1)
        let tap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.tapped(_:)))
        tap.delegate = context.coordinator
        tap.cancelsTouchesInView = false
        tap.delaysTouchesBegan = false
        tap.delaysTouchesEnded = false
        view.addGestureRecognizer(tap)
        view.delegate = context.coordinator
        context.coordinator.attach(view)
        return view
    }

    func updateUIView(_ view: PDFView, context: Context) {
        let coordinator = context.coordinator
        coordinator.parent = self
        let documentChanged = coordinator.revision != map.revision
        let viewportChanged = coordinator.viewportSize != view.bounds.size
        coordinator.viewportSize = view.bounds.size

        if documentChanged {
            coordinator.resetFollowing()
            coordinator.clearAnnotations()
            guard let document = PDFDocument(url: fileURL), !document.isLocked, document.pageCount > 0 else {
                DispatchQueue.main.async {
                    onError("The downloaded PDF could not be opened, or requires a password.")
                }
                return
            }
            coordinator.revision = map.revision
            coordinator.indexPages()
            view.document = document
            view.autoScales = true
            DispatchQueue.main.async { [weak coordinator] in
                coordinator?.observeScrolling()
                coordinator?.refreshVisiblePages()
                coordinator?.followAudioIfNeeded(force: true)
            }
        }

        coordinator.observeScrolling()
        coordinator.refreshVisiblePages()
        if coordinator.centerRevision != centerRevision {
            coordinator.centerRevision = centerRevision
            coordinator.resumeFollowing()
        } else if !documentChanged {
            coordinator.followAudioIfNeeded(force: viewportChanged)
        }
    }

    static func dismantleUIView(_ view: PDFView, coordinator: Coordinator) {
        coordinator.clearAnnotations()
        coordinator.detach()
        view.delegate = nil
        view.document = nil
    }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate, PDFViewDelegate {
        var parent: IOSPDFParagraphCanvas
        weak var view: PDFView?
        var revision = ""
        var centerRevision = 0
        var viewportSize = CGSize.zero
        private var pageIndex: [Int: [(IOSPDFReadAlongParagraph, IOSPDFReadAlongAnchor, Bool)]] = [:]
        private var annotations: [Int: [PDFAnnotation]] = [:]
        private var signatures: [Int: String] = [:]
        private var wordAnnotations: [Int: [PDFAnnotation]] = [:]
        private var wordSignatures: [Int: String] = [:]
        private var observers: [NSObjectProtocol] = []
        private var scrollViews: [UIScrollView] = []
        private var refreshing = false
        private var followingAudio = true
        private var lastFollowedParagraphID: String?

        init(parent: IOSPDFParagraphCanvas) { self.parent = parent }

        func attach(_ view: PDFView) {
            self.view = view
            resetFollowing()
            for name in [Notification.Name.PDFViewPageChanged, Notification.Name.PDFViewScaleChanged] {
                observers.append(NotificationCenter.default.addObserver(
                    forName: name,
                    object: view,
                    queue: .main
                ) { [weak self] _ in
                    self?.observeScrolling()
                    self?.refreshVisiblePages()
                })
            }
        }

        func detach() {
            observers.forEach(NotificationCenter.default.removeObserver)
            observers.removeAll()
            stopObservingScrolling()
            view = nil
        }

        private func descendants(of parent: UIView) -> [UIScrollView] {
            parent.subviews.flatMap { child in
                (child as? UIScrollView).map { [$0] } ?? descendants(of: child)
            }
        }

        func observeScrolling() {
            guard let view else { return }
            let current = descendants(of: view)
            let currentIDs = Set(current.map { ObjectIdentifier($0) })
            guard currentIDs != Set(scrollViews.map { ObjectIdentifier($0) }) else { return }
            stopObservingScrolling()
            scrollViews = current
            for scroll in current {
                scroll.panGestureRecognizer.addTarget(self, action: #selector(userScrolled(_:)))
            }
        }

        private func stopObservingScrolling() {
            for scroll in scrollViews {
                scroll.panGestureRecognizer.removeTarget(self, action: #selector(userScrolled(_:)))
            }
            scrollViews.removeAll()
        }

        @objc private func userScrolled(_ recognizer: UIPanGestureRecognizer) {
            guard recognizer.state == .began || recognizer.state == .changed else { return }
            pauseFollowing()
            refreshVisiblePages()
        }

        private func publishFollowingState() {
            DispatchQueue.main.async { [weak self] in
                guard let self, self.view != nil else { return }
                self.parent.onFollowingChanged(self.followingAudio)
            }
        }

        func resetFollowing() {
            followingAudio = true
            lastFollowedParagraphID = nil
            publishFollowingState()
        }

        private func pauseFollowing() {
            guard followingAudio else { return }
            followingAudio = false
            publishFollowingState()
        }

        func resumeFollowing() {
            followingAudio = true
            publishFollowingState()
            followAudioIfNeeded(force: true)
        }

        func followAudioIfNeeded(force: Bool = false) {
            guard followingAudio else { return }
            guard let id = parent.activeParagraphID else {
                lastFollowedParagraphID = nil
                return
            }
            guard force || lastFollowedParagraphID != id else { return }
            if centerOnAudio() { lastFollowedParagraphID = id }
        }

        func indexPages() {
            pageIndex.removeAll()
            var markedParagraphs = Set<String>()
            for paragraph in parent.map.paragraphs {
                var firstValidAnchor = true
                for anchor in paragraph.anchors {
                    guard anchor.page >= 1, anchor.page <= parent.map.pageCount,
                          !anchor.rectangles.isEmpty else { continue }
                    let showsMarker = firstValidAnchor && paragraph.start.isFinite && paragraph.start >= 0
                        && markedParagraphs.insert(paragraph.id).inserted
                    firstValidAnchor = false
                    pageIndex[anchor.page - 1, default: []].append((paragraph, anchor, showsMarker))
                }
            }
        }

        func clearAnnotations() {
            for values in annotations.values {
                for annotation in values { annotation.page?.removeAnnotation(annotation) }
            }
            annotations.removeAll()
            signatures.removeAll()
            for values in wordAnnotations.values {
                for annotation in values { annotation.page?.removeAnnotation(annotation) }
            }
            wordAnnotations.removeAll()
            wordSignatures.removeAll()
        }

        func refreshVisiblePages() {
            guard !refreshing, let view, let document = view.document, document.pageCount > 0 else { return }
            refreshing = true
            defer { refreshing = false }

            var wanted = Set<Int>()
            for page in view.visiblePages {
                let index = document.index(for: page)
                if index >= 0, index < document.pageCount { wanted.insert(index) }
            }
            if let page = view.currentPage {
                let current = document.index(for: page)
                if current >= 0, current < document.pageCount {
                    for index in max(0, current - 2)...min(document.pageCount - 1, current + 2) {
                        wanted.insert(index)
                    }
                }
            }
            if wanted.isEmpty { wanted.insert(0) }

            for index in Array(annotations.keys) where !wanted.contains(index) {
                for annotation in annotations.removeValue(forKey: index) ?? [] {
                    annotation.page?.removeAnnotation(annotation)
                }
                signatures[index] = nil
            }

            for index in wanted {
                let hasActiveParagraph = (pageIndex[index] ?? []).contains {
                    $0.0.id == parent.activeParagraphID
                }
                let signature = hasActiveParagraph
                    ? "\(parent.activeParagraphID ?? "")|\(parent.isPlaying)|\(parent.isBuffering)" : ""
                guard signatures[index] != signature, let page = document.page(at: index) else { continue }
                for annotation in annotations[index] ?? [] { page.removeAnnotation(annotation) }
                var created: [PDFAnnotation] = []
                var occupied: [CGRect] = []

                for (paragraph, anchor, showsMarker) in pageIndex[index] ?? [] {
                    let rectangles = anchor.rectangles
                    guard let first = rectangles.first else { continue }
                    let active = paragraph.id == parent.activeParagraphID
                    if active {
                        for rect in rectangles {
                            let highlight = PDFAnnotation(bounds: rect, forType: .square, withProperties: nil)
                            highlight.color = .clear
                            highlight.interiorColor = UIColor.systemTeal.withAlphaComponent(0.16)
                            highlight.shouldPrint = false
                            page.addAnnotation(highlight)
                            created.append(highlight)
                        }
                    }
                    guard showsMarker else { continue }

                    let crop = page.bounds(for: .cropBox)
                    let size: CGFloat = 20
                    var icon = CGRect(
                        x: max(crop.minX + 2, first.minX - size - 4),
                        y: min(crop.maxY - size - 2, max(crop.minY + 2, first.midY - size / 2)),
                        width: size,
                        height: size
                    )
                    for _ in 0..<8 where occupied.contains(where: { $0.intersects(icon) }) {
                        let nextX = icon.minX - size - 2
                        guard nextX >= crop.minX + 2 else { break }
                        icon.origin.x = nextX
                    }
                    occupied.append(icon)
                    let marker = IOSParagraphPlayAnnotation(
                        bounds: icon,
                        paragraph: paragraph,
                        active: active,
                        playing: active && parent.isPlaying,
                        buffering: active && parent.isBuffering
                    )
                    page.addAnnotation(marker)
                    created.append(marker)
                }
                annotations[index] = created
                signatures[index] = signature
                // Reinsert only this page's word overlay above a changed paragraph highlight.
                wordSignatures[index] = nil
            }
            refreshWordAnnotations(on: wanted, document: document)
        }

        private func refreshWordAnnotations(on wanted: Set<Int>, document: PDFDocument) {
            var rectanglesByPage: [Int: [CGRect]] = [:]
            if let word = parent.activeWord {
                for anchor in word.anchors
                    where anchor.page >= 1 && anchor.page <= document.pageCount
                        && wanted.contains(anchor.page - 1) {
                    rectanglesByPage[anchor.page - 1, default: []].append(contentsOf: anchor.rectangles)
                }
            }
            for index in Array(wordAnnotations.keys) where rectanglesByPage[index]?.isEmpty != false {
                for annotation in wordAnnotations.removeValue(forKey: index) ?? [] {
                    annotation.page?.removeAnnotation(annotation)
                }
                wordSignatures[index] = nil
            }
            let signature = "\(parent.activeParagraphID ?? "")|\(parent.activeWordIndex ?? -1)"
            for (index, rectangles) in rectanglesByPage where !rectangles.isEmpty {
                guard wordSignatures[index] != signature, let page = document.page(at: index) else { continue }
                for annotation in wordAnnotations[index] ?? [] { page.removeAnnotation(annotation) }
                wordAnnotations[index] = rectangles.map { rect in
                    let highlight = PDFAnnotation(bounds: rect, forType: .square, withProperties: nil)
                    highlight.color = .clear
                    highlight.interiorColor = UIColor.systemTeal.withAlphaComponent(0.42)
                    highlight.shouldPrint = false
                    page.addAnnotation(highlight)
                    return highlight
                }
                wordSignatures[index] = signature
            }
        }

        private func paragraph(at point: CGPoint) -> IOSPDFReadAlongParagraph? {
            guard let view, let page = view.page(for: point, nearest: false),
                  let document = view.document else { return nil }
            let pagePoint = view.convert(point, to: page)
            let index = document.index(for: page)

            if let marker = annotations[index]?.compactMap({ $0 as? IOSParagraphPlayAnnotation })
                .filter({ $0.bounds.insetBy(dx: -8, dy: -8).contains(pagePoint) })
                .min(by: {
                    let left = CGPoint(x: $0.bounds.midX - pagePoint.x, y: $0.bounds.midY - pagePoint.y)
                    let right = CGPoint(x: $1.bounds.midX - pagePoint.x, y: $1.bounds.midY - pagePoint.y)
                    return left.x * left.x + left.y * left.y < right.x * right.x + right.y * right.y
                }) {
                return marker.paragraph
            }
            return nil
        }

        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
            guard let view else { return false }
            return paragraph(at: touch.location(in: view)) != nil
        }

        func gestureRecognizer(
            _ gestureRecognizer: UIGestureRecognizer,
            shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer
        ) -> Bool {
            true
        }

        func gestureRecognizer(
            _ gestureRecognizer: UIGestureRecognizer,
            shouldBeRequiredToFailBy otherGestureRecognizer: UIGestureRecognizer
        ) -> Bool {
            otherGestureRecognizer is UILongPressGestureRecognizer
        }

        func pdfViewParentViewController() -> UIViewController {
            var responder: UIResponder? = view
            while let current = responder {
                if let controller = current as? UIViewController { return controller }
                responder = current.next
            }
            return UIApplication.shared.connectedScenes
                .compactMap { ($0 as? UIWindowScene)?.keyWindow?.rootViewController }
                .first ?? UIViewController()
        }

        @objc func tapped(_ recognizer: UITapGestureRecognizer) {
            guard recognizer.state == .ended, let view,
                  let paragraph = paragraph(at: recognizer.location(in: view)) else { return }
            if paragraph.id == parent.activeParagraphID {
                parent.onTogglePlayback()
            } else {
                pauseFollowing()
                parent.onPlay(paragraph.start)
            }
        }

        private func centerOnAudio() -> Bool {
            guard let view, view.bounds.height > 0, let document = view.document,
                  let paragraph = parent.map.paragraphs.first(where: { $0.id == parent.activeParagraphID }),
                  let anchor = paragraph.anchors.first(where: {
                      $0.page >= 1 && $0.page <= document.pageCount && !$0.rectangles.isEmpty
                  }),
                  let rect = anchor.rectangles.first,
                  let page = document.page(at: anchor.page - 1) else { return false }
            UIView.performWithoutAnimation {
                view.go(to: PDFDestination(page: page, at: CGPoint(x: rect.minX, y: rect.maxY + 70)))
                view.layoutIfNeeded()
            }
            refreshVisiblePages()
            return true
        }
    }
}
#endif
