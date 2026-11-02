// Uploads a shared PDF without opening the app or retaining the whole book in memory.
import UIKit
import UniformTypeIdentifiers

final class ShareViewController: UIViewController, URLSessionDownloadDelegate {
    private struct PreparedUpload {
        let directory: URL
        let file: URL
        let name: String
        let bytes: Int64
        let boundary: String
    }

    private struct ShareError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    private let filenameLabel = UILabel()
    private let statusLabel = UILabel()
    private let progressView = UIProgressView(progressViewStyle: .default)
    private let spinner = UIActivityIndicatorView(style: .medium)
    private let uploadButton = UIButton(type: .system)
    private let closeButton = UIButton(type: .system)
    private var prepared: PreparedUpload?
    private var connection: ShareConnection?
    private var preparation: Progress?
    private var session: URLSession?
    private var uploadTask: URLSessionUploadTask?
    private var downloadTask: URLSessionDownloadTask?
    private var downloadName: String?
    private var accepted = false
    private var closed = false

    override func viewDidLoad() {
        super.viewDidLoad()
        configureView()
        preparePDF()
    }

    private func configureView() {
        overrideUserInterfaceStyle = .dark
        view.backgroundColor = UIColor(red: 0.055, green: 0.07, blue: 0.065, alpha: 1)
        preferredContentSize = CGSize(width: 420, height: 380)
        let title = UILabel()
        title.text = "Pdf2Podcast"
        title.font = .preferredFont(forTextStyle: .title1)
        title.adjustsFontForContentSizeCategory = true
        filenameLabel.font = .preferredFont(forTextStyle: .headline)
        filenameLabel.adjustsFontForContentSizeCategory = true
        filenameLabel.numberOfLines = 3
        filenameLabel.text = "Preparing your PDF"
        statusLabel.font = .preferredFont(forTextStyle: .subheadline)
        statusLabel.adjustsFontForContentSizeCategory = true
        statusLabel.numberOfLines = 0
        statusLabel.textColor = .secondaryLabel
        statusLabel.text = "Loading the shared file..."
        progressView.tintColor = .systemGreen
        progressView.isHidden = true
        spinner.startAnimating()

        var uploadConfiguration = UIButton.Configuration.filled()
        uploadConfiguration.title = "Convert to audio"
        uploadConfiguration.baseBackgroundColor = .systemGreen
        uploadConfiguration.baseForegroundColor = .black
        uploadConfiguration.cornerStyle = .capsule
        uploadConfiguration.contentInsets = NSDirectionalEdgeInsets(top: 14, leading: 20, bottom: 14, trailing: 20)
        uploadButton.configuration = uploadConfiguration
        uploadButton.isEnabled = false
        uploadButton.addTarget(self, action: #selector(sendPDF), for: .touchUpInside)
        closeButton.setTitle("Cancel", for: .normal)
        closeButton.addTarget(self, action: #selector(closeShare), for: .touchUpInside)

        let loadingRow = UIStackView(arrangedSubviews: [spinner, statusLabel])
        loadingRow.spacing = 12
        loadingRow.alignment = .center
        let stack = UIStackView(arrangedSubviews: [title, filenameLabel, loadingRow, progressView, uploadButton, closeButton])
        stack.axis = .vertical
        stack.spacing = 20
        let scrollView = UIScrollView()
        scrollView.alwaysBounceVertical = false
        view.addSubview(scrollView)
        scrollView.addSubview(stack)
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        stack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            scrollView.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor),
            scrollView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
            stack.leadingAnchor.constraint(equalTo: scrollView.contentLayoutGuide.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: scrollView.contentLayoutGuide.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: scrollView.contentLayoutGuide.topAnchor, constant: 24),
            stack.bottomAnchor.constraint(equalTo: scrollView.contentLayoutGuide.bottomAnchor, constant: -24),
            stack.widthAnchor.constraint(equalTo: scrollView.frameLayoutGuide.widthAnchor, constant: -48)
        ])
    }

    private func preparePDF() {
        do {
            guard let saved = try ShareConnectionStore.load(), saved.uploadURL != nil,
                  !saved.token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw ShareError(message: "Open Pdf2Podcast once to sync your server connection, then share the PDF again.")
            }
            connection = saved
            let items = extensionContext?.inputItems.compactMap { $0 as? NSExtensionItem } ?? []
            let providers = items.flatMap { $0.attachments ?? [] }
            let pdfs = providers.filter { $0.hasItemConformingToTypeIdentifier(UTType.pdf.identifier) }
            let files = providers.filter { $0.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) }
            let data = providers.filter { $0.registeredTypeIdentifiers.contains(UTType.data.identifier) }
            let links = providers.filter { $0.hasItemConformingToTypeIdentifier(UTType.url.identifier) }
            // A PDF may arrive alongside a title, thumbnail, or the page's URL.
            let candidates = !pdfs.isEmpty ? pdfs : !files.isEmpty ? files : !data.isEmpty ? data : links
            guard candidates.count == 1, let provider = candidates.first else {
                let message = candidates.isEmpty
                    ? "No PDF or file link was supplied. In the source app, export or download the PDF, then share that file."
                    : "Share one PDF at a time to convert it to audio."
                throw ShareError(message: message)
            }
            if !pdfs.isEmpty {
                loadPDFFile(provider, type: UTType.pdf.identifier)
            } else if !files.isEmpty {
                loadSharedURL(provider, type: UTType.fileURL.identifier)
            } else if !data.isEmpty {
                loadPDFFile(provider, type: UTType.data.identifier)
            } else {
                loadSharedURL(provider, type: UTType.url.identifier)
            }
        } catch { showError(Self.importError(error)) }
    }

    private func loadPDFFile(_ provider: NSItemProvider, type: String, inPlace: Bool = false) {
        guard !closed else { return }
        let handler: (URL?, Error?) -> Void = { [weak self] url, error in
            if let url {
                // Provider URLs expire when this callback returns. Copy before returning.
                let result = Result { try Self.prepareLocalPDF(from: url, suggestedName: provider.suggestedName) }
                self?.finishPreparation(result)
                if self == nil, case let .success(upload) = result {
                    try? FileManager.default.removeItem(at: upload.directory)
                }
            } else if !inPlace {
                DispatchQueue.main.async { [weak self] in
                    self?.loadPDFFile(provider, type: type, inPlace: true)
                }
            } else if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
                DispatchQueue.main.async { [weak self] in
                    self?.loadSharedURL(provider, type: UTType.fileURL.identifier)
                }
            } else {
                self?.finishPreparation(.failure(error ?? ShareError(
                    message: "The source app could not provide the PDF file. Download it into Files and share the downloaded PDF."
                )))
            }
        }
        if inPlace {
            preparation = provider.loadInPlaceFileRepresentation(forTypeIdentifier: type) { url, _, error in
                handler(url, error)
            }
        } else {
            preparation = provider.loadFileRepresentation(forTypeIdentifier: type, completionHandler: handler)
        }
    }

    private func loadSharedURL(_ provider: NSItemProvider, type: String) {
        guard !closed else { return }
        preparation = nil
        provider.loadItem(forTypeIdentifier: type, options: nil) { [weak self] item, error in
            let url: URL?
            if let value = item as? URL {
                url = value
            } else if let value = item as? String {
                url = URL(string: value)
            } else if let value = item as? Data, let string = String(data: value, encoding: .utf8) {
                url = URL(string: string.trimmingCharacters(in: .whitespacesAndNewlines))
            } else {
                url = nil
            }
            guard let url else {
                self?.finishPreparation(.failure(error ?? ShareError(message: "The source app did not supply a readable file link.")))
                return
            }
            if url.isFileURL {
                let result = Result { try Self.prepareLocalPDF(from: url, suggestedName: provider.suggestedName) }
                self?.finishPreparation(result)
                if self == nil, case let .success(upload) = result {
                    try? FileManager.default.removeItem(at: upload.directory)
                }
            } else {
                DispatchQueue.main.async { [weak self] in
                    self?.downloadPDF(url, suggestedName: provider.suggestedName)
                }
            }
        }
    }

    private func finishPreparation(_ result: Result<PreparedUpload, Error>) {
        DispatchQueue.main.async { [weak self] in
            guard let self, !self.closed else {
                if case let .success(upload) = result { try? FileManager.default.removeItem(at: upload.directory) }
                return
            }
            self.preparation = nil
            self.spinner.stopAnimating()
            self.progressView.isHidden = true
            switch result {
            case let .success(upload):
                self.prepared = upload
                self.filenameLabel.text = upload.name
                let size = ByteCountFormatter.string(fromByteCount: upload.bytes, countStyle: .file)
                self.statusLabel.text = "\(size). Upload to your server and add it to the conversion queue."
                self.uploadButton.isEnabled = true
            case let .failure(error):
                self.showError(Self.importError(error))
            }
        }
    }

    private static func prepareLocalPDF(from source: URL, suggestedName: String?) throws -> PreparedUpload {
        let scoped = source.startAccessingSecurityScopedResource()
        defer { if scoped { source.stopAccessingSecurityScopedResource() } }
        var coordinationError: NSError?
        var result: Result<PreparedUpload, Error>?
        NSFileCoordinator().coordinate(readingItemAt: source, options: .withoutChanges, error: &coordinationError) { readable in
            result = Result { try prepareMultipart(from: readable, suggestedName: suggestedName) }
        }
        if let coordinationError { throw coordinationError }
        guard let result else { throw ShareError(message: "The file provider could not open this PDF. Download it in Files first.") }
        return try result.get()
    }

    private func downloadPDF(_ url: URL, suggestedName: String?) {
        guard !closed else { return }
        guard ["https", "http"].contains(url.scheme?.lowercased() ?? ""),
              url.host != nil, url.user == nil, url.password == nil else {
            showError("This link cannot be downloaded. Share the PDF file itself from Files.")
            return
        }
        downloadName = suggestedName
        statusLabel.text = "Downloading the shared PDF link. Keep this screen open..."
        progressView.progress = 0
        progressView.isHidden = false
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 60
        configuration.timeoutIntervalForResource = 300
        // This session has no backend credentials or app cookies.
        let session = URLSession(configuration: configuration, delegate: self, delegateQueue: .main)
        self.session = session
        var request = URLRequest(url: url)
        request.setValue("application/pdf", forHTTPHeaderField: "Accept")
        let task = session.downloadTask(with: request)
        downloadTask = task
        task.resume()
    }

    private func finishDownload(_ result: Result<PreparedUpload, Error>) {
        downloadTask = nil
        session?.invalidateAndCancel()
        session = nil
        finishPreparation(result)
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                    didWriteData bytesWritten: Int64, totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        guard !closed, downloadTask === self.downloadTask else { return }
        guard max(totalBytesWritten, totalBytesExpectedToWrite) <= 100 * 1024 * 1024 else {
            downloadTask.cancel()
            finishDownload(.failure(ShareError(message: "This PDF exceeds the 100 MB share limit.")))
            return
        }
        if totalBytesExpectedToWrite > 0 {
            let fraction = min(1, Float(totalBytesWritten) / Float(totalBytesExpectedToWrite))
            progressView.progress = fraction
            statusLabel.text = "Downloading PDF: \(Int(fraction * 100))%. Keep this screen open."
        }
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        guard !closed, downloadTask === self.downloadTask else { return }
        guard let response = downloadTask.response as? HTTPURLResponse, (200..<300).contains(response.statusCode) else {
            let status = (downloadTask.response as? HTTPURLResponse)?.statusCode ?? 0
            finishDownload(.failure(ShareError(message: "The PDF link returned HTTP \(status). If it requires login, download the PDF in your browser and share it from Files.")))
            return
        }
        let suggestedName = response.suggestedFilename ?? downloadName
        let result = Result { try Self.prepareMultipart(from: location, suggestedName: suggestedName) }
        finishDownload(result)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard !closed, task === downloadTask, let error else { return }
        finishDownload(.failure(error))
    }

    private static func importError(_ error: Error) -> String {
        if let error = error as? ShareError { return error.message }
        let failure = error as NSError
        return "Could not open the shared PDF: \(failure.localizedDescription) [\(failure.domain) \(failure.code)]. Try downloading the PDF in Files first."
    }

    private static func prepareMultipart(from source: URL, suggestedName: String?) throws -> PreparedUpload {
        let scoped = source.startAccessingSecurityScopedResource()
        defer { if scoped { source.stopAccessingSecurityScopedResource() } }
        let manager = FileManager.default
        let directory = manager.temporaryDirectory.appendingPathComponent("pdf2podcast-share-\(UUID().uuidString)", isDirectory: true)
        try manager.createDirectory(at: directory, withIntermediateDirectories: true)
        do {
            var name = suggestedName ?? source.lastPathComponent
            name = name.components(separatedBy: .controlCharacters).joined()
                .replacingOccurrences(of: "\"", with: "_")
                .replacingOccurrences(of: "\\", with: "_")
                .replacingOccurrences(of: "/", with: "_")
            if name.isEmpty { name = "Shared PDF" }
            if !name.lowercased().hasSuffix(".pdf") { name += ".pdf" }
            let file = directory.appendingPathComponent("upload.multipart")
            guard manager.createFile(atPath: file.path, contents: nil) else {
                throw ShareError(message: "Could not prepare the upload. Check your iPhone's available storage.")
            }
            let input = try FileHandle(forReadingFrom: source)
            defer { try? input.close() }
            let output = try FileHandle(forWritingTo: file)
            defer { try? output.close() }
            let boundary = "Pdf2Podcast-\(UUID().uuidString)"
            let header = "--\(boundary)\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"\(name)\"\r\nContent-Type: application/pdf\r\n\r\n"
            try output.write(contentsOf: Data(header.utf8))
            var bytes: Int64 = 0
            while let chunk = try input.read(upToCount: 256 * 1024), !chunk.isEmpty {
                if bytes == 0, chunk.range(of: Data("%PDF-".utf8)) == nil {
                    throw ShareError(message: "The shared item is not a PDF. If it is a webpage or login page, download or export the actual PDF and share that file.")
                }
                bytes += Int64(chunk.count)
                guard bytes <= 100 * 1024 * 1024 else {
                    throw ShareError(message: "This PDF is larger than the 100 MB share limit. Split it into smaller PDFs first.")
                }
                try output.write(contentsOf: chunk)
            }
            guard bytes > 0 else { throw ShareError(message: "The shared PDF is empty.") }
            try output.write(contentsOf: Data("\r\n--\(boundary)--\r\n".utf8))
            return PreparedUpload(directory: directory, file: file, name: name, bytes: bytes, boundary: boundary)
        } catch {
            try? manager.removeItem(at: directory)
            throw error
        }
    }

    @objc private func sendPDF() {
        guard uploadTask == nil, let prepared, let connection, let url = connection.uploadURL else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 120
        request.setValue("Bearer \(connection.token)", forHTTPHeaderField: "Authorization")
        request.setValue("multipart/form-data; boundary=\(prepared.boundary)", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        uploadButton.isEnabled = false
        spinner.startAnimating()
        statusLabel.textColor = .secondaryLabel
        statusLabel.text = "Uploading... Keep this screen open until the server confirms receipt."
        progressView.progress = 0
        progressView.isHidden = false
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForResource = 600
        let session = URLSession(configuration: configuration, delegate: self, delegateQueue: .main)
        self.session = session
        let task = session.uploadTask(with: request, fromFile: prepared.file) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self, !self.closed else { return }
                self.uploadTask = nil
                self.session?.finishTasksAndInvalidate()
                self.session = nil
                self.spinner.stopAnimating()
                if let error {
                    self.showError("Upload was not confirmed: \(error.localizedDescription) Check the app's library before retrying, to avoid creating a duplicate conversion.")
                    return
                }
                guard let response = response as? HTTPURLResponse else {
                    self.showError("The server returned no response. Check the app's library before retrying.")
                    return
                }
                guard (200..<300).contains(response.statusCode) else {
                    self.showError(Self.serverError(status: response.statusCode, data: data))
                    return
                }
                // A tunnel's HTML page is not an acknowledgement from the conversion API.
                guard let data, (try? JSONSerialization.jsonObject(with: data)) is [String: Any] else {
                    self.showError("The server did not return a conversion receipt. Check the app's library and server address before retrying.")
                    return
                }
                self.accepted = true
                self.progressView.progress = 1
                self.statusLabel.text = "Added to the conversion queue. You can close this screen; processing continues on the server. Find progress and audio in Pdf2Podcast."
                self.uploadButton.isHidden = true
                self.closeButton.setTitle("Done", for: .normal)
                self.cleanup()
                UIAccessibility.post(notification: .announcement, argument: "PDF added to the conversion queue")
            }
        }
        uploadTask = task
        task.resume()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                    totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
        guard !closed, totalBytesExpectedToSend > 0 else { return }
        let fraction = min(1, Float(totalBytesSent) / Float(totalBytesExpectedToSend))
        progressView.progress = fraction
        statusLabel.text = fraction >= 1 ? "Upload sent. Waiting for the server to confirm..."
            : "Uploading \(Int(fraction * 100))%. Keep this screen open."
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        // Only unauthenticated PDF downloads may follow redirects; uploads never do.
        if task === downloadTask, ["https", "http"].contains(request.url?.scheme?.lowercased() ?? ""),
           request.url?.user == nil, request.url?.password == nil,
           request.value(forHTTPHeaderField: "Authorization") == nil {
            completionHandler(request)
        } else {
            completionHandler(nil)
        }
    }

    private static func serverError(status: Int, data: Data?) -> String {
        if status == 401 || status == 403 {
            return "The server rejected the mobile token (HTTP \(status)). Update the connection in Pdf2Podcast, then share again."
        }
        if status == 413 { return "The server's upload limit is smaller than this PDF (HTTP 413). Try a smaller PDF." }
        if let data, let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            let nested = json["error"] as? [String: Any]
            let detail = nested?["message"] as? String ?? json["message"] as? String
                ?? json["detail"] as? String ?? json["error"] as? String
            let code = nested?["code"] as? String ?? json["code"] as? String
            if let detail {
                return "Upload failed (HTTP \(status)): \(String(detail.prefix(500)))\(code.map { " [\($0)]" } ?? "")"
            }
        }
        return "Upload failed (HTTP \(status)). Check the server connection in Pdf2Podcast and try again."
    }

    private func showError(_ message: String) {
        spinner.stopAnimating()
        progressView.isHidden = true
        statusLabel.textColor = .systemOrange
        statusLabel.text = message
        uploadButton.isEnabled = prepared != nil && connection != nil
        if prepared != nil { uploadButton.configuration?.title = "Retry upload" }
    }

    @objc private func closeShare() {
        if uploadTask != nil {
            let alert = UIAlertController(title: "Stop uploading?", message: "The upload may not be complete. If the server has already received it, conversion will continue; check the app's library before retrying.", preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "Keep uploading", style: .cancel))
            alert.addAction(UIAlertAction(title: "Stop upload", style: .destructive) { [weak self] _ in self?.finish() })
            present(alert, animated: true)
        } else { finish() }
    }

    private func finish() {
        closed = true
        preparation?.cancel()
        downloadTask?.cancel()
        uploadTask?.cancel()
        session?.invalidateAndCancel()
        session = nil
        cleanup()
        if accepted {
            extensionContext?.completeRequest(returningItems: nil)
        } else {
            extensionContext?.cancelRequest(withError: NSError(domain: NSCocoaErrorDomain, code: NSUserCancelledError))
        }
    }

    private func cleanup() {
        if let prepared { try? FileManager.default.removeItem(at: prepared.directory) }
        prepared = nil
    }

    deinit {
        preparation?.cancel()
        session?.invalidateAndCancel()
        if let prepared { try? FileManager.default.removeItem(at: prepared.directory) }
    }
}
