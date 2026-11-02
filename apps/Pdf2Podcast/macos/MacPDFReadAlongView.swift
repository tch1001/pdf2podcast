// Follow audio paragraphs until the reader browses away, without modifying the original paper.
#if targetEnvironment(macCatalyst)
import CryptoKit
import PDFKit
import SwiftUI
import UIKit

private struct PDFReadAlongMap: Decodable, Sendable {
    let version: Int
    let revision: String
    let coordinateSpace: String
    let pageCount: Int
    let unmappedParagraphs: Int
    let paragraphs: [PDFReadAlongParagraph]
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
        let decodedParagraphs = try values.decode([PDFReadAlongParagraph].self, forKey: .paragraphs)
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

    func paragraph(at time: Double) -> PDFReadAlongParagraph? {
        timeline.index(at: time).map { paragraphs[$0] }
    }

}

private struct PDFReadAlongParagraph: Decodable, Identifiable, Sendable {
    let id: String
    let title: String
    let chapter: String
    let start: Double
    let summaryStart: Double?
    let end: Double?
    let kind: String?
    let anchors: [PDFReadAlongAnchor]
    let words: [PDFReadAlongWord]
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
        anchors = try values.decode([PDFReadAlongAnchor].self, forKey: .anchors)
        words = try values.decodeIfPresent([PDFReadAlongWord].self, forKey: .words) ?? []
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

private struct PDFReadAlongWord: Decodable, Sendable {
    let text: String
    let start: Double
    let end: Double
    let anchors: [PDFReadAlongAnchor]
}

private struct PDFReadAlongAnchor: Decodable, Sendable {
    let page: Int
    let rects: [[Double]]
    var rectangles: [CGRect] {
        rects.compactMap {
            guard $0.count == 4, $0.allSatisfy(\.isFinite), $0[2] > 0, $0[3] > 0 else { return nil }
            return CGRect(x: $0[0], y: $0[1], width: $0[2], height: $0[3])
        }
    }
}

private enum PDFReadAlongError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        switch self { case .message(let message): return message }
    }
}

private final class PDFReadAlongRedirectGuard: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        guard let original = task.originalRequest?.url, let next = request.url,
              original.scheme == next.scheme, original.host == next.host, original.port == next.port else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }
}

@MainActor
private final class PDFReadAlongLoader: ObservableObject {
    @Published var map: PDFReadAlongMap?
    @Published var fileURL: URL?
    @Published var error: String?
    @Published var loading = false

    func load(server: String, token: String, jobID: String) async {
        loading = true
        error = nil
        map = nil
        fileURL = nil
        defer { loading = false }
        let session = URLSession(configuration: .ephemeral, delegate: PDFReadAlongRedirectGuard(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        do {
            let base = server.trimmingCharacters(in: .whitespacesAndNewlines)
                .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            func request(_ artifact: String) throws -> URLRequest {
                guard let url = URL(string: base + "/api/audio/jobs/" + jobID + "/" + artifact),
                      ["http", "https"].contains(url.scheme?.lowercased() ?? ""), url.host != nil else {
                    throw PDFReadAlongError.message("The server URL is invalid.")
                }
                var request = URLRequest(url: url)
                request.timeoutInterval = 120
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                return request
            }
            let (data, response) = try await session.data(for: request("read-along"))
            guard let http = response as? HTTPURLResponse else {
                throw PDFReadAlongError.message("The paragraph map returned an invalid response.")
            }
            guard http.statusCode == 200 else {
                let details = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
                let reason = details?["detail"] as? String ?? "Paragraph locations are not available. The backend may need its read-along update."
                throw PDFReadAlongError.message("\(reason) (HTTP \(http.statusCode))")
            }
            let mapping = try JSONDecoder().decode(PDFReadAlongMap.self, from: data)
            guard [1, 2].contains(mapping.version), mapping.coordinateSpace == "pdf_user_space" else {
                throw PDFReadAlongError.message("This paragraph map uses an unsupported coordinate format.")
            }
            try Task.checkCancellation()
            let cacheKey = SHA256.hash(data: Data((base + "|" + jobID + "|" + mapping.revision).utf8))
                .map { String(format: "%02x", $0) }.joined()
            let directory = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("Pdf2PodcastReadAlong", isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let destination = directory.appendingPathComponent(cacheKey + ".pdf")
            if !FileManager.default.fileExists(atPath: destination.path) {
                let (temporary, response) = try await session.download(for: request("pdf"))
                defer { try? FileManager.default.removeItem(at: temporary) }
                guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                      http.mimeType?.lowercased() == "application/pdf" else {
                    let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                    throw PDFReadAlongError.message("The original PDF could not download (HTTP \(status)).")
                }
                try Task.checkCancellation()
                // A second reader may have completed the same immutable download.
                if !FileManager.default.fileExists(atPath: destination.path) {
                    try FileManager.default.moveItem(at: temporary, to: destination)
                }
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

struct MacPDFReadAlongView: View {
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

    private var highlightedWord: PDFReadAlongWord? {
        guard let currentTime, let index = highlightedWordIndex,
              let paragraph = loader.map?.paragraph(at: currentTime) else { return nil }
        return paragraph.words[index]
    }

    @StateObject private var loader = PDFReadAlongLoader()
    @State private var showParagraphs = false
    @State private var centerRevision = 0
    @State private var followingAudio = true
    @State private var reloadRevision = 0

    var body: some View {
        VStack(spacing: 0) {
            if let map = loader.map, let fileURL = loader.fileURL {
                HStack(spacing: 16) {
                    Button { showParagraphs.toggle() } label: {
                        Label("Paragraphs", systemImage: "sidebar.left")
                    }
                    Text("\(map.pageCount) pages / \(map.paragraphs.count) playable paragraphs")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    if isBuffering { ProgressView().controlSize(.small) }
                    Button { centerRevision += 1 } label: {
                        Label("Recenter", systemImage: "scope")
                    }
                    .help("Jump to the highlighted paragraph and resume following the audio.")
                    .disabled(highlightedParagraphID == nil)
                }
                .padding(14)
                Divider()
                HStack(spacing: 0) {
                    if showParagraphs {
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 6) {
                                ForEach(map.paragraphs) { paragraph in
                                    Button { onPlay(paragraph.start) } label: {
                                        HStack(alignment: .top, spacing: 10) {
                                            Image(systemName: highlightedParagraphID == paragraph.id ? "speaker.wave.2.fill" : "play.circle")
                                                .foregroundStyle(.cyan)
                                            VStack(alignment: .leading, spacing: 4) {
                                                Text(paragraph.title).font(.subheadline.weight(.medium)).lineLimit(3)
                                                Text("Page \(paragraph.anchors.first?.page ?? 1)")
                                                    .font(.caption).foregroundStyle(.secondary)
                                            }
                                            Spacer(minLength: 0)
                                        }
                                        .padding(10)
                                        .background(highlightedParagraphID == paragraph.id ? Color.cyan.opacity(0.12) : Color.clear,
                                                    in: RoundedRectangle(cornerRadius: 8))
                                        .contentShape(Rectangle())
                                    }.buttonStyle(.plain)
                                }
                            }.padding(8)
                        }.frame(width: 260)
                        Divider()
                    }
                    PDFParagraphCanvas(
                        fileURL: fileURL, map: map, activeParagraphID: highlightedParagraphID,
                        activeWord: highlightedWord, activeWordIndex: highlightedWordIndex,
                        isPlaying: isPlaying, isBuffering: isBuffering, centerRevision: centerRevision,
                        onPlay: onPlay, onTogglePlayback: onTogglePlayback,
                        onFollowingChanged: { followingAudio = $0 },
                        onError: { loader.error = $0; loader.fileURL = nil }
                    )
                }
                HStack {
                    Label(followingAudio ? "Following audio" : "Browsing PDF",
                          systemImage: followingAudio ? "location.fill" : "hand.draw")
                        .foregroundStyle(followingAudio ? Color.cyan : Color.secondary)
                    Text(followingAudio ? "Scroll to browse freely." : "Recenter to follow the highlighted paragraph again.")
                    Spacer()
                    if map.unmappedParagraphs > 0 {
                        Text("\(map.unmappedParagraphs) paragraphs have no reliable location.")
                            .foregroundStyle(.orange)
                    }
                }
                .font(.caption).foregroundStyle(.secondary).padding(12)
            } else if let error = loader.error {
                ContentUnavailableView {
                    Label("PDF read-along unavailable", systemImage: "doc.questionmark")
                } description: {
                    Text(error)
                } actions: {
                    Button("Try again") { reloadRevision += 1 }
                }
            } else {
                ProgressView("Opening PDF and matching paragraph locations...")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(red: 0.032, green: 0.045, blue: 0.059))
        .navigationTitle("PDF read-along")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: "\(jobID)|\(serverURL)|\(reloadRevision)") {
            await loader.load(server: serverURL, token: mobileToken, jobID: jobID)
        }
    }
}

private final class ParagraphPlayAnnotation: PDFAnnotation {
    let paragraph: PDFReadAlongParagraph
    var active: Bool
    var playing: Bool
    var buffering: Bool

    init(bounds: CGRect, paragraph: PDFReadAlongParagraph, active: Bool, playing: Bool, buffering: Bool) {
        self.paragraph = paragraph
        self.active = active
        self.playing = playing
        self.buffering = buffering
        super.init(bounds: bounds, forType: .stamp, withProperties: nil)
        contents = "Start reading this paragraph: " + paragraph.title
        shouldPrint = false
    }

    required init?(coder: NSCoder) { fatalError("In-memory annotation only") }

    override func draw(with box: PDFDisplayBox, in context: CGContext) {
        context.saveGState()
        defer { context.restoreGState() }
        let circle = bounds.insetBy(dx: 0.5, dy: 0.5)
        context.setFillColor((active || buffering ? UIColor.systemTeal : UIColor(red: 0.08, green: 0.28, blue: 0.35, alpha: 1)).cgColor)
        context.fillEllipse(in: circle)
        context.setFillColor(UIColor.white.cgColor)
        if buffering {
            for x in [0.3, 0.5, 0.7] {
                context.fillEllipse(in: CGRect(x: bounds.minX + bounds.width * x - 0.8,
                                              y: bounds.midY - 0.8, width: 1.6, height: 1.6))
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

private struct PDFParagraphCanvas: UIViewRepresentable {
    let fileURL: URL
    let map: PDFReadAlongMap
    let activeParagraphID: String?
    let activeWord: PDFReadAlongWord?
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
        view.autoScales = true
        view.backgroundColor = UIColor(white: 0.18, alpha: 1)
        view.isInMarkupMode = false
        view.isUserInteractionEnabled = true
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
        if documentChanged {
            coordinator.resetFollowing()
            coordinator.clearAnnotations()
            guard let document = PDFDocument(url: fileURL), !document.isLocked else {
                DispatchQueue.main.async { onError("The downloaded PDF could not be opened, or requires a password.") }
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
            coordinator.followAudioIfNeeded()
        }
    }

    static func dismantleUIView(_ view: PDFView, coordinator: Coordinator) {
        coordinator.clearAnnotations()
        coordinator.detach()
        view.delegate = nil
        view.document = nil
    }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate, PDFViewDelegate {
        var parent: PDFParagraphCanvas
        weak var view: PDFView?
        var revision = ""
        var centerRevision = 0
        private var pageIndex: [Int: [(PDFReadAlongParagraph, PDFReadAlongAnchor, Bool)]] = [:]
        private var annotations: [Int: [PDFAnnotation]] = [:]
        private var signatures: [Int: String] = [:]
        private var wordAnnotations: [Int: [PDFAnnotation]] = [:]
        private var wordSignatures: [Int: String] = [:]
        private var observers: [NSObjectProtocol] = []
        private var refreshing = false
        private var followingAudio = true
        private var lastFollowedParagraphID: String?
        private var scrollGesture: UIPanGestureRecognizer?
        private var scrollViews: [UIScrollView] = []
        private var scrollObservations: [NSKeyValueObservation] = []
        private var suppressViewportChanges = false
        private var suppressionRevision = 0

        init(parent: PDFParagraphCanvas) { self.parent = parent }

        func attach(_ view: PDFView) {
            self.view = view
            // Observe mouse-wheel and trackpad scrolling without taking it away from PDFKit.
            let scroll = UIPanGestureRecognizer(target: self, action: #selector(userScrolled(_:)))
            scroll.allowedScrollTypesMask = .all
            scroll.allowedTouchTypes = []
            scroll.cancelsTouchesInView = false
            scroll.delaysTouchesBegan = false
            scroll.delaysTouchesEnded = false
            scroll.delegate = self
            view.addGestureRecognizer(scroll)
            scrollGesture = scroll
            resetFollowing()
            for name in [Notification.Name.PDFViewPageChanged, Notification.Name.PDFViewScaleChanged] {
                observers.append(NotificationCenter.default.addObserver(forName: name, object: view, queue: .main) { [weak self] _ in
                    self?.observeScrolling()
                    self?.refreshVisiblePages()
                })
            }
        }

        func detach() {
            observers.forEach(NotificationCenter.default.removeObserver)
            observers.removeAll()
            stopObservingScrolling()
            if let scrollGesture { view?.removeGestureRecognizer(scrollGesture) }
            scrollGesture = nil
            view = nil
        }

        private func stopObservingScrolling() {
            scrollObservations.forEach { $0.invalidate() }
            scrollObservations.removeAll()
            for scroll in scrollViews {
                scroll.panGestureRecognizer.removeTarget(self, action: #selector(userScrolled(_:)))
            }
            scrollViews.removeAll()
        }

        func observeScrolling() {
            guard let view else { return }
            func descendants(_ parent: UIView) -> [UIScrollView] {
                parent.subviews.flatMap { child in
                    (child as? UIScrollView).map { [$0] } ?? descendants(child)
                }
            }
            let current = descendants(view)
            guard Set(current.map { ObjectIdentifier($0) }) != Set(scrollViews.map { ObjectIdentifier($0) }) else { return }
            stopObservingScrolling()
            scrollViews = current
            for scroll in current {
                scroll.panGestureRecognizer.addTarget(self, action: #selector(userScrolled(_:)))
                var lastSize = scroll.bounds.size
                var lastContentSize = scroll.contentSize
                var lastZoom = scroll.zoomScale
                scrollObservations.append(scroll.observe(\.contentOffset, options: [.old, .new]) { [weak self] scroll, change in
                    let geometryChanged = lastSize != scroll.bounds.size
                        || lastContentSize != scroll.contentSize || lastZoom != scroll.zoomScale
                    lastSize = scroll.bounds.size
                    lastContentSize = scroll.contentSize
                    lastZoom = scroll.zoomScale
                    guard let self, !geometryChanged, !self.suppressViewportChanges,
                          !scroll.isZooming, !scroll.isZoomBouncing,
                          let old = change.oldValue, let new = change.newValue,
                          abs(old.x - new.x) > 0.5 || abs(old.y - new.y) > 0.5 else { return }
                    // Also catches scroll-bar dragging, keyboard navigation, and accessibility scrolling.
                    self.pauseFollowing()
                })
            }
        }

        private func ignoreAutomaticViewportChanges() {
            suppressionRevision += 1
            let revision = suppressionRevision
            suppressViewportChanges = true
            DispatchQueue.main.async { [weak self] in
                guard let self, self.suppressionRevision == revision else { return }
                self.suppressViewportChanges = false
            }
        }

        private func publishFollowingState() {
            DispatchQueue.main.async { [weak self] in
                guard let self, self.view != nil else { return }
                self.parent.onFollowingChanged(self.followingAudio)
            }
        }

        func resetFollowing() {
            ignoreAutomaticViewportChanges()
            followingAudio = true
            lastFollowedParagraphID = nil
            publishFollowingState()
        }

        private func pauseFollowing() {
            guard followingAudio else { return }
            followingAudio = false
            publishFollowingState()
        }

        @objc private func userScrolled(_ recognizer: UIPanGestureRecognizer) {
            guard recognizer.state == .began || recognizer.state == .changed else { return }
            // Actual user input wins even if it arrives during a programmatic recenter.
            pauseFollowing()
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
                    // A paragraph may span blocks, columns, or pages. Only its first
                    // location gets a control; all its other regions still highlight.
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
            var wanted = Set(view.visiblePages.map { document.index(for: $0) })
            if let page = view.currentPage {
                let current = document.index(for: page)
                if current >= 0, current < document.pageCount {
                    for index in max(0, current - 2)...min(document.pageCount - 1, current + 2) { wanted.insert(index) }
                }
            }
            for index in Array(annotations.keys) where !wanted.contains(index) {
                for annotation in annotations.removeValue(forKey: index) ?? [] { annotation.page?.removeAnnotation(annotation) }
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
                            highlight.interiorColor = UIColor.systemTeal.withAlphaComponent(0.14)
                            highlight.shouldPrint = false
                            page.addAnnotation(highlight)
                            created.append(highlight)
                        }
                    }
                    guard showsMarker else { continue }
                    let crop = page.bounds(for: .cropBox)
                    let size: CGFloat = 14
                    var icon = CGRect(x: max(crop.minX + 2, first.minX - size - 4),
                                      y: min(crop.maxY - size - 2, max(crop.minY + 2, first.midY - size / 2)),
                                      width: size, height: size)
                    for _ in 0..<8 where occupied.contains(where: { $0.intersects(icon) }) {
                        let nextX = icon.minX - size - 2
                        guard nextX >= crop.minX + 2 else { break }
                        icon.origin.x = nextX
                    }
                    occupied.append(icon)
                    let marker = ParagraphPlayAnnotation(bounds: icon, paragraph: paragraph,
                                                         active: active,
                                                         playing: active && parent.isPlaying,
                                                         buffering: active && parent.isBuffering)
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

        private func marker(at point: CGPoint) -> ParagraphPlayAnnotation? {
            guard let view, let page = view.page(for: point, nearest: false), let document = view.document else { return nil }
            let pagePoint = view.convert(point, to: page)
            return annotations[document.index(for: page)]?.compactMap { $0 as? ParagraphPlayAnnotation }
                .filter { $0.bounds.insetBy(dx: -3, dy: -3).contains(pagePoint) }
                .min {
                    let leftX = $0.bounds.midX - pagePoint.x, leftY = $0.bounds.midY - pagePoint.y
                    let rightX = $1.bounds.midX - pagePoint.x, rightY = $1.bounds.midY - pagePoint.y
                    return leftX * leftX + leftY * leftY < rightX * rightX + rightY * rightY
                }
        }

        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
            if gestureRecognizer === scrollGesture { return true }
            guard let view else { return false }
            return marker(at: touch.location(in: view)) != nil
        }

        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer) -> Bool {
            gestureRecognizer === scrollGesture || otherGestureRecognizer === scrollGesture
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
            guard recognizer.state == .ended, let view, let marker = marker(at: recognizer.location(in: view)) else { return }
            if marker.paragraph.id == parent.activeParagraphID {
                parent.onTogglePlayback()
            } else {
                parent.onPlay(marker.paragraph.start)
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
            ignoreAutomaticViewportChanges()
            UIView.performWithoutAnimation {
                view.go(to: PDFDestination(page: page, at: CGPoint(x: rect.minX, y: rect.maxY + 45)))
                view.layoutIfNeeded()
            }
            refreshVisiblePages()
            return true
        }
    }
}
#endif
