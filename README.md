# Pdf2Podcast

Turn papers and ebook PDFs into chaptered audio companions with a shared library,
streaming playback, and synchronized transcripts.

- Near-verbatim source narration, with citations and page furniture filtered out.
- A second voice introduces chapters and gives concise paragraph summaries.
- Native iOS and Mac Catalyst apps, plus a browser client.
- Paragraph-level audio checkpoints, parallel processing, and resumable conversions.
- Sleep timers, playback speed controls, transcript navigation, and summary skipping.

## Repository layout

```text
apps/Pdf2Podcast/ios/       iOS app, PDF share extension, and shared player
apps/Pdf2Podcast/macos/     Mac Catalyst target and copy-based PDF importer
apps/Pdf2Podcast/web/       Browser client
plugins/pdf2podcast/        Standalone backend and PDF processing pipeline
docs/                      Processing design notes
```

This repository contains the standalone application, not the older Jarvis fleet
gateway. Xcode projects are generated from the checked-in `project.yml` files.

## Run the backend

Install Python 3.10 or newer and FFmpeg, including `ffprobe`. Then:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
chmod 600 .env
```

Edit the local `.env` file with your own OpenAI API key and a newly generated
mobile token. Generate a token locally with `openssl rand -hex 32`. The provider
API key belongs only on the backend; clients need only the mobile token.

```sh
set -a
source .env
set +a
pdf2podcast-server --host 127.0.0.1 --port 8766
```

The server accepts `OPENAI_API_KEY`, or a private key file selected with
`PDF2PODCAST_OPENAI_KEY_FILE`. Uploaded PDFs, checkpoints, transcripts, and finished
audio remain under `PDF2PODCAST_DATA_DIR`, which defaults to `~/.pdf2podcast`.
Speech currently uses Edge TTS; an ElevenLabs integration is not included.

For remote devices, expose the backend through an HTTPS reverse proxy or tunnel.
For example, with Cloudflare's `cloudflared` installed:

```sh
cloudflared tunnel --url http://127.0.0.1:8766
```

Use the resulting HTTPS URL in the clients. A temporary tunnel URL can change
when the tunnel restarts. All clients using the shared mobile token see the same
library. Treat that token as a credential and do not publish it or put it in a URL.
Native clients continue to use the private mobile token. The hosted browser client uses
Google Identity Services and a secure server-side session instead: configure a Web OAuth
client whose authorized JavaScript origin is the public HTTPS origin, then set
`PDF2PODCAST_GOOGLE_CLIENT_ID`. Set `PDF2PODCAST_ADMIN_EMAILS` to a comma-separated list
of owner accounts that may see aggregate usage and every conversion.

Browser uploads are capped at 50 MB, 20 upload attempts per rolling minute, and one new
processing submission per rolling minute. Identical PDF bytes share one canonical job
across users, while each user gets the conversion in their own library. These limits are
configurable through the corresponding variables in `.env.example`.

Every web account requires one-time acceptance of versioned public-upload terms during
initial account setup. The server records the accepting user, timestamp, and terms
version; every subsequent browser upload appears in the public catalogue and exposes its
source PDF and generated media. Native-app uploads remain private unless separately
released by the operator or linked to a browser account whose owner accepted public
distribution. Google emails are never exposed by the public catalogue.

## iOS app

Install Xcode and XcodeGen. From `apps/Pdf2Podcast/ios`:

```sh
xcodegen generate
open Pdf2Podcast.xcodeproj
```

Choose your own development team for the app and its share extension, then build
for your iPhone. Open connection settings and enter your backend URL and mobile
token. No provider key or prefilled mobile token is included in the source.

## Mac app

See [the Mac build instructions](apps/Pdf2Podcast/macos/README.md). The Mac target
shares the iOS player and uses a copy-based importer with temporary upload storage.
Connection settings are stored separately on each device; library data lives on
the shared backend.

## Web app

```sh
python3 -m http.server 8080 --directory apps/Pdf2Podcast/web
```

The production server serves this client at `/` so browser sessions, uploads, and audio
remain same-origin. During frontend-only development, serve the directory locally and
proxy `/api` to the backend; Google sign-in requires that local origin to be registered
on the OAuth client.

## Credential checks

Keep `.env`, key files, signing material, local settings exports, uploaded PDFs,
generated audio, and build products out of Git. The repository's ignore rules
exclude these artifacts, but ignore rules do not sanitize files already tracked.

With Gitleaks installed, scan the working tree and every reachable commit before
pushing:

```sh
gitleaks dir . --redact --max-decode-depth 2
gitleaks git . --log-opts="--all" --redact --max-decode-depth 2
```

The configuration extends the standard secret rules with a rule for unlabelled
long hexadecimal credentials. If a real credential is ever published, rotate it;
deleting the current file alone does not remove historical exposure.
