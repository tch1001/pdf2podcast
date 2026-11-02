import Foundation

enum ConnectionState: Equatable {
    case notConfigured
    case checking
    case connected(node: String)
    case unavailable(message: String)

    var label: String {
        switch self {
        case .notConfigured: "Set up"
        case .checking: "Connecting"
        case let .connected(node): node
        case .unavailable: "Offline"
        }
    }

    var isConnected: Bool {
        if case .connected = self { return true }
        return false
    }
}

@MainActor
final class AppModel: ObservableObject {
    @Published var connectionState: ConnectionState = .checking
    @Published var messages: [ChatMessage] = []
    @Published var historyHasMore = false
    @Published var historyNextBefore: Int?
    @Published var pendingTurn: PendingTurn?
    @Published var isSending = false
    @Published var isLoadingHistory = false
    @Published var chatError: String?

    @Published var tasks: [TaskItem] = []
    @Published var agents: [AgentItem] = []
    @Published var tasksSummary = ""
    @Published var includeFinished = false
    @Published var isLoadingTasks = false
    @Published var tasksError: String?
    @Published var pendingTask: PendingTask?

    @Published var nodes: [NodeInfo] = []
    @Published var alerts: [FleetAlert] = []
    @Published var fleetErrors: [String] = []
    @Published var isLoadingFleet = false
    @Published var fleetError: String?

    @Published var uploadedJobs: [PDFAudioJob] = []
    @Published var isUploadingPDF = false
    @Published var isLoadingPDFJobs = false
    @Published var downloadingPDFJobID: String?
    @Published var downloadedAudioURLs: [String: URL] = [:]
    @Published var pdfUploadError: String?

    let settings: SettingsStore
    private let defaults: UserDefaults
    private let pendingTurnKey = "pending-mobile-turn"
    private let pendingTaskKey = "pending-mobile-task"
    private let uploadedJobsKey = "uploaded-pdf-jobs"
    private var pdfPollingTask: Task<Void, Never>?

    static let verbatimPrompt = """
    Produce a transcript that follows the source PDF text as closely as possible.
    Keep wording, punctuation, numbering, capitalization, and section headings intact.
    Do not summarize, reframe, or editorialize. If a page has OCR uncertainty, preserve
    the raw characters with minimal cleanup and do not invent missing content.
    """.trimmingCharacters(in: .whitespacesAndNewlines)

    init(settings: SettingsStore, defaults: UserDefaults = .standard) {
        self.settings = settings
        self.defaults = defaults
        pendingTurn = Self.decode(PendingTurn.self, from: defaults.data(forKey: pendingTurnKey))
        pendingTask = Self.decode(PendingTask.self, from: defaults.data(forKey: pendingTaskKey))
        uploadedJobs = Self.decode([PDFAudioJob].self, from: defaults.data(forKey: uploadedJobsKey)) ?? []
        connectionState = settings.isConfigured ? .checking : .notConfigured
    }

    func bootstrap() async {
        guard settings.isConfigured else {
            connectionState = .notConfigured
            return
        }
        await checkConnection()
        guard connectionState.isConnected else { return }
        await refreshHistory()
        await refreshTasks()
        await refreshFleet()
        await refreshPDFJobs()
        startPDFJobPollingIfNeeded()
    }

    func checkConnection() async {
        guard let client = makeClient() else {
            connectionState = .notConfigured
            return
        }
        connectionState = .checking
        do {
            let response = try await client.connection()
            connectionState = response.connected
                ? .connected(node: response.node.name)
                : .unavailable(message: "The backend did not connect to the server.")
        } catch {
            connectionState = .unavailable(message: error.localizedDescription)
        }
    }

    func saveSettingsAndReconnect() async {
        guard settings.save() else { return }
        await bootstrap()
    }

    func refreshHistory() async {
        guard let client = makeClient() else { return }
        isLoadingHistory = true
        defer { isLoadingHistory = false }
        do {
            let page = try await client.history(limit: 100, before: nil)
            messages = page.messages
            historyHasMore = page.hasMore
            historyNextBefore = page.nextBefore
            chatError = nil
        } catch {
            chatError = error.localizedDescription
        }
    }

    func loadOlderHistory() async {
        guard let client = makeClient(), let before = historyNextBefore, !isLoadingHistory else { return }
        isLoadingHistory = true
        defer { isLoadingHistory = false }
        do {
            let page = try await client.history(limit: 100, before: before)
            let existing = Set(messages.map(\.id))
            messages.insert(contentsOf: page.messages.filter { !existing.contains($0.id) }, at: 0)
            historyHasMore = page.hasMore
            historyNextBefore = page.nextBefore
            chatError = nil
        } catch {
            chatError = error.localizedDescription
        }
    }

    func send(_ rawMessage: String) async {
        let text = rawMessage.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isSending else { return }
        let pending = PendingTurn(
            id: "ios-\(UUID().uuidString.lowercased())",
            message: text,
            createdAt: Date().timeIntervalSince1970
        )
        pendingTurn = pending
        persistPendingTurn()
        await submitPendingTurn()
    }

    func retryPendingTurn() async {
        guard pendingTurn != nil, !isSending else { return }
        await submitPendingTurn()
    }

    func uploadPDF(
        from fileURL: URL,
        withTitle rawTitle: String?,
        provider: String,
        voice rawVoice: String?,
        transcriptPrompt: String?
    ) async {
        guard let client = makeClient() else { return }
        isUploadingPDF = true
        pdfUploadError = nil
        defer { isUploadingPDF = false }

        let hasScopedAccess = fileURL.startAccessingSecurityScopedResource()
        defer {
            if hasScopedAccess {
                fileURL.stopAccessingSecurityScopedResource()
            }
        }

        let payload: Data
        do {
            // Read while the security-scoped URL is active. A detached task can
            // lose the document-provider access granted by the Files picker.
            payload = try Data(contentsOf: fileURL, options: .mappedIfSafe)
        } catch {
            pdfUploadError = "Could not read \(fileURL.lastPathComponent): \(error.localizedDescription)"
            return
        }
        if payload.isEmpty {
            pdfUploadError = "The selected file has no data."
            return
        }
        if !payload.starts(with: Data("%PDF-".utf8)) {
            pdfUploadError = "Selected file is not a PDF."
            return
        }
        let sizeLimitBytes = 14_000_000
        if payload.count > sizeLimitBytes {
            pdfUploadError = "PDF is too large for the mobile upload limit."
            return
        }
        let encoded = payload.base64EncodedString()
        let title = rawTitle?.trimmingCharacters(in: .whitespacesAndNewlines)
        let voice = rawVoice?.trimmingCharacters(in: .whitespacesAndNewlines)
        let prompt = transcriptPrompt?.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanPrompt = (prompt?.isEmpty == true) ? Self.verbatimPrompt : prompt
        do {
            let job = try await client.submitPDFForAudio(
                filename: fileURL.lastPathComponent,
                fileB64: encoded,
                title: title?.isEmpty == true ? nil : title,
                source: "mobile-ios",
                transcriptPrompt: cleanPrompt,
                ttsProvider: provider,
                ttsVoice: voice?.isEmpty == true ? nil : voice
            )
            persistUploadedJob(job)
            pdfUploadError = nil
            await refreshPDFJobs()
            startPDFJobPollingIfNeeded()
        } catch {
            pdfUploadError = error.localizedDescription
        }
    }

    func refreshPDFJobs() async {
        guard let client = makeClient(), !isLoadingPDFJobs else { return }
        isLoadingPDFJobs = true
        defer { isLoadingPDFJobs = false }
        do {
            let response = try await client.pdfJobs(limit: 100)
            uploadedJobs = response.jobs
            persist(uploadedJobs, key: uploadedJobsKey)
            pdfUploadError = nil
        } catch {
            pdfUploadError = error.localizedDescription
        }
    }

    func downloadAudio(for job: PDFAudioJob) async {
        guard job.isComplete, let client = makeClient() else { return }
        downloadingPDFJobID = job.id
        defer { downloadingPDFJobID = nil }
        do {
            downloadedAudioURLs[job.id] = try await client.downloadPDFAudio(jobID: job.id)
            pdfUploadError = nil
        } catch {
            pdfUploadError = error.localizedDescription
        }
    }

    func startPDFJobPollingIfNeeded() {
        pdfPollingTask?.cancel()
        guard uploadedJobs.contains(where: \.isActive) else { return }
        pdfPollingTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                guard !Task.isCancelled else { return }
                await self.refreshPDFJobs()
                if !self.uploadedJobs.contains(where: \.isActive) {
                    return
                }
            }
        }
    }

    private func submitPendingTurn() async {
        guard let pending = pendingTurn, let client = makeClient() else { return }
        isSending = true
        chatError = nil
        defer { isSending = false }
        do {
            let response = try await client.chat(message: pending.message, turnID: pending.id)
            let user = ChatMessage(
                role: "user",
                text: pending.message,
                timestamp: pending.createdAt,
                turnID: pending.id
            )
            let assistant = ChatMessage(
                role: "assistant",
                text: response.answer,
                engine: response.engine,
                turnID: pending.id
            )
            if !messages.contains(where: { $0.turnID == pending.id }) {
                messages.append(contentsOf: [user, assistant])
            }
            pendingTurn = nil
            persistPendingTurn()
            await refreshHistory()
        } catch {
            chatError = error.localizedDescription
        }
    }

    func refreshTasks() async {
        guard let client = makeClient() else { return }
        isLoadingTasks = true
        defer { isLoadingTasks = false }
        do {
            let response = try await client.tasks(includeFinished: includeFinished)
            tasks = response.tasks
            agents = response.agents
            tasksSummary = response.summary
            let unavailable = response.unavailableNodes.map { "\($0.key): \($0.value)" }
            tasksError = unavailable.isEmpty ? nil : unavailable.sorted().joined(separator: "\n")
        } catch {
            tasksError = error.localizedDescription
        }
    }

    func createTask(summary: String, detail: String, priority: String) async -> Bool {
        let cleaned = summary.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty, let client = makeClient() else { return false }
        let pending = PendingTask(
            id: "ios-\(UUID().uuidString.lowercased())",
            summary: cleaned,
            detail: detail.trimmingCharacters(in: .whitespacesAndNewlines),
            priority: priority
        )
        pendingTask = pending
        persistPendingTask()
        do {
            _ = try await client.upsertTask(
                id: pending.id,
                summary: pending.summary,
                detail: pending.detail,
                priority: pending.priority
            )
            pendingTask = nil
            persistPendingTask()
            tasksError = nil
            await refreshTasks()
            return true
        } catch {
            tasksError = error.localizedDescription
            return false
        }
    }

    func retryPendingTask() async -> Bool {
        guard let pending = pendingTask, let client = makeClient() else { return false }
        do {
            _ = try await client.upsertTask(
                id: pending.id,
                summary: pending.summary,
                detail: pending.detail,
                priority: pending.priority
            )
            pendingTask = nil
            persistPendingTask()
            tasksError = nil
            await refreshTasks()
            return true
        } catch {
            tasksError = error.localizedDescription
            return false
        }
    }

    func setTaskStatus(_ task: TaskItem, status: String) async {
        guard let client = makeClient() else { return }
        do {
            let updated = try await client.setTaskStatus(id: task.id, status: status)
            if let index = tasks.firstIndex(where: { $0.id == updated.id }) {
                tasks[index] = updated
            }
            if !includeFinished && ["done", "failed"].contains(status) {
                tasks.removeAll { $0.id == updated.id }
            }
            tasksError = nil
        } catch {
            tasksError = error.localizedDescription
        }
    }

    func refreshFleet() async {
        guard let client = makeClient() else { return }
        isLoadingFleet = true
        defer { isLoadingFleet = false }
        do {
            let response = try await client.fleet(alertLimit: 100)
            nodes = response.nodes.compactMap(\.ok).sorted { $0.name < $1.name }
            alerts = response.alerts.flatMap { result in
                (result.ok?.alerts ?? []).map { alert in
                    var copy = alert
                    copy.sourceNode = result.name ?? result.node
                    return copy
                }
            }.sorted { $0.at > $1.at }
            fleetErrors = response.errors.map { "\($0.key): \($0.value.message)" }
                + response.nodes.compactMap { row in row.error.map { "\(row.name ?? "node"): \($0.message)" } }
                + response.alerts.compactMap { row in row.error.map { "\(row.name ?? "alerts"): \($0.message)" } }
            fleetError = nil
        } catch {
            fleetError = error.localizedDescription
        }
    }

    private func makeClient() -> GatewayServing? {
        guard let url = settings.baseURL else {
            connectionState = .notConfigured
            return nil
        }
        return GatewayClient(baseURL: url, token: settings.token)
    }

    private func persistPendingTurn() {
        persist(pendingTurn, key: pendingTurnKey)
    }

    private func persistPendingTask() {
        persist(pendingTask, key: pendingTaskKey)
    }

    private func persistUploadedJob(_ job: PDFAudioJob) {
        uploadedJobs.removeAll { $0.id == job.id }
        uploadedJobs.insert(job, at: 0)
        if uploadedJobs.count > 20 {
            uploadedJobs.removeLast(uploadedJobs.count - 20)
        }
        persist(uploadedJobs, key: uploadedJobsKey)
    }

    private func persist<Value: Encodable>(_ value: Value?, key: String) {
        guard let value, let data = try? JSONEncoder().encode(value) else {
            defaults.removeObject(forKey: key)
            return
        }
        defaults.set(data, forKey: key)
    }

    private static func decode<Value: Decodable>(_ type: Value.Type, from data: Data?) -> Value? {
        guard let data else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }
}

struct PendingTask: Codable, Identifiable, Equatable {
    let id: String
    let summary: String
    let detail: String
    let priority: String
}

// Searches absolute, half-open audio intervals without carrying stale state across seeks.
// Prefix maxima also make overlapping intervals safe: the latest containing start wins.
struct PDFReadAlongIntervalIndex: Sendable {
    struct Entry: Sendable {
        let start: Double
        let end: Double
        let index: Int
    }

    private let entries: [Entry]
    private let maximumEnds: [Double]

    init(_ candidates: [Entry]) {
        entries = candidates.filter {
            $0.start.isFinite && $0.end.isFinite && $0.start >= 0 && $0.end > $0.start
        }.sorted {
            $0.start == $1.start ? $0.index < $1.index : $0.start < $1.start
        }
        var maximum = -Double.infinity
        maximumEnds = entries.map {
            maximum = max(maximum, $0.end)
            return maximum
        }
    }

    func index(at time: Double) -> Int? {
        guard time.isFinite, time >= 0 else { return nil }
        var lower = 0
        var upper = entries.count
        while lower < upper {
            let middle = lower + (upper - lower) / 2
            if entries[middle].start <= time { lower = middle + 1 }
            else { upper = middle }
        }
        var candidate = lower
        while candidate > 0 {
            candidate -= 1
            guard maximumEnds[candidate] > time else { break }
            if time < entries[candidate].end { return entries[candidate].index }
        }
        return nil
    }
}
