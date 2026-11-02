#!/usr/bin/env bash
# Generate the group CA and per-peer certificates for TLS mode (PROJECT_CONTEXT.md §6.6).
# Usage: scripts/gen_certs.sh <outdir> <peer> [peer...]
#   e.g. scripts/gen_certs.sh certs ahmed mujtaba huzaifa
# Output must never be committed. ca.key is reused if present, never overwritten.
set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "usage: $0 <outdir> <peer> [peer...]" >&2
    exit 2
fi

outdir=$1
shift
mkdir -p "$outdir"
cd "$outdir"
umask 077

# Group CA (ECDSA P-256)
if [ -f ca.key ]; then
    echo "Reusing existing ca.key"
else
    openssl ecparam -name prime256v1 -genkey -noout -out ca.key
fi
chmod 600 ca.key
if [ ! -f ca.crt ]; then
    openssl req -new -x509 -key ca.key -out ca.crt -days 365 -sha256 \
        -subj "/CN=meshsync-group-ca" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign"
fi

# Per-peer key + CSR, signed by the CA (mutual TLS: server + client auth)
for peer in "$@"; do
    openssl ecparam -name prime256v1 -genkey -noout -out "$peer.key"
    chmod 600 "$peer.key"
    openssl req -new -key "$peer.key" -out "$peer.csr" -subj "/CN=$peer"
    ext=$(mktemp)
    cat > "$ext" <<EXT
subjectAltName=DNS:$peer
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth,clientAuth
EXT
    openssl x509 -req -in "$peer.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
        -out "$peer.crt" -days 365 -sha256 -extfile "$ext"
    rm -f "$ext"
    openssl verify -CAfile ca.crt "$peer.crt"
done

echo
echo "Certificates written to $(pwd)"
for peer in "$@"; do
    echo "  laptop '$peer': ca.crt $peer.crt $peer.key"
done
echo "ca.key stays offline with one member. Never commit any of these files."
