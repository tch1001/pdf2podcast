"""Fetch public PDFs without letting user-supplied links reach private services."""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.parse


DOWNLOAD_SLOTS = threading.BoundedSemaphore(4)
DOWNLOAD_SECONDS = 45


class URLImportError(ValueError):
    """Carry actionable download failures through the application's JSON protocol."""

    def __init__(self, message: str, code: str = "invalid_pdf_url", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def normalize_pdf_url(value: str) -> str:
    """Accept ordinary web URLs, never credentials, arbitrary ports or local schemes."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise URLImportError("Provide a direct PDF URL of at most 4096 characters.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise URLImportError("The PDF URL contains invalid characters.")
    try:
        parts = urllib.parse.urlsplit(value.strip())
        host = (parts.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parts.port
    except (ValueError, UnicodeError) as error:
        raise URLImportError("The PDF URL is invalid.") from error
    if parts.scheme not in {"https", "http"} or not host:
        raise URLImportError("Use an https:// or http:// link to a public PDF.")
    if parts.username is not None or parts.password is not None:
        raise URLImportError("PDF links containing usernames or passwords are not supported.")
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        raise URLImportError("PDF links must use the standard HTTP or HTTPS port.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
            raise URLImportError("Use a public internet hostname for the PDF.")
    else:
        if not public_address(address):
            raise URLImportError("Private or reserved network addresses are not allowed.")
    authority = f"[{host}]" if ":" in host else host
    path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parts.query, safe="=&;%:+,/?@!$'()*-._~")
    return urllib.parse.urlunsplit((parts.scheme, authority, path, query, ""))


def public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Exclude transition addresses as well as ordinary non-public destinations."""
    return (
        address.is_global and not address.is_multicast and not address.is_reserved
        and not (
            isinstance(address, ipaddress.IPv6Address)
            and (address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None)
        )
    )


def remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("PDF download deadline exceeded")
    return min(10.0, remaining)


def pinned_connection(parts: urllib.parse.SplitResult, deadline: float) -> http.client.HTTPConnection:
    """Connect to a validated numeric address while retaining the hostname for TLS."""
    host = parts.hostname or ""
    port = 443 if parts.scheme == "https" else 80
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    if not addresses:
        raise URLImportError("The PDF host could not be resolved.", "pdf_host_unavailable", 502)
    for _, _, _, _, endpoint in addresses:
        if not public_address(ipaddress.ip_address(endpoint[0])):
            raise URLImportError("The PDF URL resolves to a private or reserved network address.")
    last_error = None
    for family, socktype, protocol, _, endpoint in addresses[:8]:
        connection = http.client.HTTPConnection(host, port, timeout=remaining_timeout(deadline))
        raw = socket.socket(family, socktype, protocol)
        try:
            raw.settimeout(remaining_timeout(deadline))
            raw.connect(endpoint)
            if parts.scheme == "https":
                raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            connection.sock = raw
            return connection
        except OSError as error:
            last_error = error
            raw.close()
            connection.close()
    raise URLImportError("Could not connect to the PDF source.", "pdf_host_unavailable", 502) from last_error


def download_pdf(value: str, maximum_bytes: int) -> tuple[str, bytes]:
    """Bound downloads and revalidate every redirect without forwarding user cookies."""
    url = normalize_pdf_url(value)
    if not DOWNLOAD_SLOTS.acquire(blocking=False):
        raise URLImportError("PDF imports are busy. Please try again shortly.", "pdf_import_busy", 503)
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    try:
        for redirect in range(6):
            parts = urllib.parse.urlsplit(url)
            connection = pinned_connection(parts, deadline)
            try:
                target = parts.path + ("?" + parts.query if parts.query else "")
                connection.request("GET", target, headers={
                    "User-Agent": "Pdf2Podcast/3 (+https://pdf2podcast.tchlabs.net)",
                    "Accept": "application/pdf, application/octet-stream;q=0.9",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                })
                response = connection.getresponse()
                try:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.getheader("Location")
                        if not location or redirect == 5:
                            raise URLImportError("The PDF link redirects too many times or has no destination.")
                        url = normalize_pdf_url(urllib.parse.urljoin(url, location))
                        continue
                    if response.status != 200:
                        raise URLImportError(
                            f"The PDF source returned HTTP {response.status}. If it requires sign-in, upload the PDF manually.",
                            "pdf_source_http_error", 502,
                        )
                    if (response.getheader("Content-Encoding") or "identity").lower() != "identity":
                        raise URLImportError("The source returned an unsupported compressed download. Upload the PDF manually.")
                    length = response.getheader("Content-Length")
                    if length is not None:
                        try:
                            size = int(length)
                        except ValueError as error:
                            raise URLImportError("The PDF source returned an invalid download length.") from error
                        if size < 0 or size > maximum_bytes:
                            raise URLImportError("This PDF exceeds the upload size limit.", "upload_too_large", 413)
                    data = bytearray()
                    while True:
                        if connection.sock is not None:
                            connection.sock.settimeout(remaining_timeout(deadline))
                        else:
                            remaining_timeout(deadline)
                        chunk = response.read1(min(65536, maximum_bytes + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > maximum_bytes:
                            raise URLImportError("This PDF exceeds the upload size limit.", "upload_too_large", 413)
                        if len(data) >= 5 and not data.startswith(b"%PDF-"):
                            raise URLImportError("This link does not return a PDF. Use a direct PDF download link.", "source_not_pdf")
                    if not data.startswith(b"%PDF-"):
                        raise URLImportError("The source did not return a PDF.", "source_not_pdf")
                    if length is not None and len(data) != int(length):
                        raise URLImportError("The PDF download was interrupted. Please try again.", "pdf_download_incomplete", 502)
                    filename = urllib.parse.unquote(parts.path).rsplit("/", 1)[-1]
                    filename = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip(" .")[:180] or "paper"
                    if not filename.lower().endswith(".pdf"):
                        filename += ".pdf"
                    return filename, bytes(data)
                finally:
                    response.close()
            finally:
                connection.close()
    except TimeoutError as error:
        raise URLImportError("The PDF source took too long to respond. Please retry or upload the file manually.", "pdf_download_timeout", 504) from error
    except (OSError, http.client.HTTPException) as error:
        raise URLImportError("The PDF download failed. Please retry or upload the file manually.", "pdf_download_failed", 502) from error
    finally:
        DOWNLOAD_SLOTS.release()
    raise URLImportError("Could not resolve the PDF download.")
