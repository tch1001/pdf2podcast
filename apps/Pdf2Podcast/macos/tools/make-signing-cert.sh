#!/bin/bash
# Creates a stable local code-signing identity for profile-free Mac builds.

set -euo pipefail

NAME="Pdf2Podcast Local Signing"
KEYCHAIN="${HOME}/Library/Keychains/login.keychain-db"

existing="$(security find-identity "$KEYCHAIN" 2>/dev/null | grep -F "$NAME" | head -1 || true)"
if [[ -n "$existing" ]]; then
    echo "Already present: $(echo "$existing" | sed 's/^ *[0-9]*) *//')"
    exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS="$(openssl rand -hex 16)"

cat > "${TMP}/openssl.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = ${NAME}
[v3]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF

openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -config "${TMP}/openssl.cnf" \
    -keyout "${TMP}/key.pem" -out "${TMP}/cert.pem" 2>/dev/null
openssl pkcs12 -export -out "${TMP}/identity.p12" \
    -inkey "${TMP}/key.pem" -in "${TMP}/cert.pem" \
    -name "$NAME" -passout "pass:${PASS}" \
    -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1 2>/dev/null
security import "${TMP}/identity.p12" -k "$KEYCHAIN" -P "$PASS" \
    -T /usr/bin/codesign -A >/dev/null

hash="$(security find-identity "$KEYCHAIN" 2>/dev/null | grep -F "$NAME" | head -1 | awk '{print $2}')"
[[ -n "$hash" ]] || { echo "The imported identity is unavailable." >&2; exit 1; }
echo "Created ${NAME}; it is valid for ten years."
