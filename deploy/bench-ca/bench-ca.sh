#!/usr/bin/env bash
# The bench CA: the trust behind the pods' statistics on a physical bench (spec 3.6), mutual TLS
# between each pod and the broker on its router. One CA per bench. Its key stays on one host, the
# CA host, and never goes to a router, a pod or a repository. Keys are made where they are used:
# the broker's on the router, a pod's on the pod. Only their certificate requests come to the CA
# host, and only certificates and ca.pem go back.
#
#   bench-ca.sh init                    the CA, once: ca.key (0600) and ca.pem
#   bench-ca.sh ca                      prints ca.pem
#   bench-ca.sh sign-broker CSR OUT ADDRESS...
#                                       the broker's certificate (serverAuth) for its addresses (IPv4
#                                       addresses or DNS names), the names the pods connect to
#   bench-ca.sh sign-pod CSR OUT        a pod's device certificate (clientAuth). Its subject is
#                                       CN=<the pod's serial>: the broker takes it as the pod's user
#                                       name (use_identity_as_username)
#   bench-ca.sh list                    the certificates issued, one line each
#
# BENCH_CA_HOST (required): the CA host's short name; the script refuses to run on any other.
# BENCH_CA_DIR (default ~/bench-ca): the CA's directory (mode 0700). Certificates last 825 days,
# the CA 10 years. Procedure: docs/guides/bench-ca.md.
set -euo pipefail

die() { echo "bench-ca: $*" >&2; exit 1; }

[ -n "${BENCH_CA_HOST:-}" ] || die "BENCH_CA_HOST is not set (the CA host's short name)"
[ "$(hostname -s)" = "$BENCH_CA_HOST" ] || die "this is $(hostname -s), not the CA host $BENCH_CA_HOST: the CA key stays there"
command -v openssl >/dev/null || die "openssl is missing"
DIR=${BENCH_CA_DIR:-$HOME/bench-ca}
DAYS=825

have_ca() {
    if [ ! -f "$DIR/ca.key" ] || [ ! -f "$DIR/ca.pem" ]; then
        die "no CA in $DIR: bench-ca.sh init"
    fi
}

# the request's signature verified, and its subject as RFC 2253 text
csr_subject() {
    [ -f "$1" ] || die "no certificate request $1"
    openssl req -in "$1" -noout -verify 2>/dev/null || die "$1 is not a valid certificate request"
    openssl req -in "$1" -noout -subject -nameopt RFC2253 | sed 's/^subject=//'
}

# the request's key: RSA of at least 2048 bits, or EC P-256 or P-384
csr_key_ok() {
    local text
    text=$(openssl req -in "$1" -noout -text)
    if grep -q 'rsaEncryption' <<<"$text"; then
        local bits
        bits=$(sed -n 's/.*Public-Key: (\([0-9]*\) bit).*/\1/p' <<<"$text" | head -1)
        [ "${bits:-0}" -ge 2048 ] || die "$1: an RSA key of ${bits:-?} bits, at least 2048 needed"
    elif grep -q 'id-ecPublicKey' <<<"$text"; then
        grep -q -E 'NIST CURVE: P-(256|384)' <<<"$text" || die "$1: an EC key on a curve other than P-256 or P-384"
    else
        die "$1: neither an RSA nor an EC key"
    fi
}

issue() {    # issue CSR OUT KIND NAME EXTFILE: sign, record, and write OUT
    local csr=$1 out=$2 kind=$3 name=$4 ext=$5 serial
    [ ! -e "$out" ] || die "$out exists: give another name"
    serial=$(openssl rand -hex 16)
    openssl x509 -req -in "$csr" -CA "$DIR/ca.pem" -CAkey "$DIR/ca.key" -set_serial "0x$serial" \
        -days "$DAYS" -sha256 -extfile "$ext" -out "$DIR/issued/$serial.pem" 2>/dev/null ||
        die "openssl could not sign $csr"
    cp "$DIR/issued/$serial.pem" "$out"
    printf '%s %s %s %s %s\n' "$(date -u +%FT%TZ)" "$kind" "$serial" \
        "$(openssl x509 -in "$out" -noout -enddate | sed 's/^notAfter=//; s/ /_/g')" "$name" >> "$DIR/index.txt"
    echo "bench-ca: $kind certificate for $name in $out (serial $serial, $DAYS days)"
}

case ${1:-} in
init)
    [ ! -e "$DIR/ca.key" ] || die "$DIR already holds a CA"
    install -d -m 700 "$DIR" "$DIR/issued"
    (
        umask 077
        openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$DIR/ca.key" 2>/dev/null
    )
    openssl req -x509 -new -key "$DIR/ca.key" -sha256 -days 3650 -subj "/CN=EMOSA bench CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" -out "$DIR/ca.pem"
    chmod 600 "$DIR/ca.key"
    chmod 644 "$DIR/ca.pem"
    : > "$DIR/index.txt"
    echo "bench-ca: CA in $DIR ($(openssl x509 -in "$DIR/ca.pem" -noout -fingerprint -sha256))"
    ;;
ca)
    have_ca
    cat "$DIR/ca.pem"
    ;;
sign-broker)
    [ $# -ge 4 ] || die "usage: bench-ca.sh sign-broker CSR OUT ADDRESS..."
    have_ca
    csr=$2 out=$3
    shift 3
    subject=$(csr_subject "$csr")
    csr_key_ok "$csr"
    names=()
    for address in "$@"; do
        if [[ "$address" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
            names+=("IP:$address")
        elif [[ "$address" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]]; then
            names+=("DNS:$address")
        else
            die "$address is neither an IPv4 address nor a DNS name"
        fi
    done
    ext=$(mktemp)
    trap 'rm -f "$ext"' EXIT
    {
        echo "basicConstraints=critical,CA:FALSE"
        echo "keyUsage=critical,digitalSignature,keyEncipherment"
        echo "extendedKeyUsage=serverAuth"
        echo "subjectAltName=$(IFS=,; echo "${names[*]}")"
    } > "$ext"
    issue "$csr" "$out" broker "$subject [$(IFS=,; echo "${names[*]}")]" "$ext"
    ;;
sign-pod)
    [ $# -eq 3 ] || die "usage: bench-ca.sh sign-pod CSR OUT"
    have_ca
    csr=$2 out=$3
    subject=$(csr_subject "$csr")
    csr_key_ok "$csr"
    # the subject is CN=<serial> alone: the broker's user name, the pod's topic and its agent's
    serial=${subject#CN=}
    [[ "$subject" == CN=* && "$serial" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] ||
        die "$csr: the subject must be CN=<the pod's serial> alone, not $subject"
    ext=$(mktemp)
    trap 'rm -f "$ext"' EXIT
    printf '%s\n' "basicConstraints=critical,CA:FALSE" "keyUsage=critical,digitalSignature,keyEncipherment" \
        "extendedKeyUsage=clientAuth" > "$ext"
    issue "$csr" "$out" pod "$serial" "$ext"
    ;;
list)
    have_ca
    cat "$DIR/index.txt"
    ;;
*)
    sed -n '8,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
