#!/usr/bin/env python3
"""buzz-agent-identity — provision a Buzz agent identity. One file, pure stdlib.

Subcommands:

  provision --name <n> [--about ..] [--avatar path] [--nip05 id] [--conditions ..] [--out path]
      Full flow: generate a fresh agent key, sign a NIP-OA owner attestation
      (owner key from a hidden prompt or stdin — never persisted), sanitize +
      upload the avatar the way Buzz Desktop does, publish the profile, and
      write a 0600 env file with BUZZ_PRIVATE_KEY + BUZZ_AUTH_TAG.

  keygen
      Just generate a keypair (AGENT_NSEC / AGENT_NPUB / AGENT_PUBKEY_HEX).

  attest --agent-pubkey <hex> [--conditions <str>]
      Just sign a NIP-OA auth tag (owner key from prompt/stdin). Prints the JSON.

The signature is BIP-340 Schnorr over secp256k1, using the reference
implementation below (validated against the spec's NIP-OA test vector). The
owner key is read from a hidden prompt or stdin only — never an env var, flag,
or file.

Needs: python3 (>= 3.6), the `buzz` CLI (for `provision`), and ImageMagick
(`magick`/`convert`) only when using --avatar. Nothing to build, nothing to pip.
"""

import sys
import os
import json
import hashlib
import secrets
import getpass
import argparse
import subprocess
import tempfile
import datetime

# ─────────────────────────────────────────────────────────────────────────────
# BIP-340 reference implementation (secp256k1 Schnorr). Adapted from the
# canonical reference in BIP-340. Pure Python, stdlib only.
# ─────────────────────────────────────────────────────────────────────────────

p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)

Point = tuple  # (x, y) or None for infinity


def tagged_hash(tag: str, msg: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + msg).digest()


def is_infinite(P):
    return P is None


def x(P):
    assert not is_infinite(P)
    return P[0]


def y(P):
    assert not is_infinite(P)
    return P[1]


def point_add(P1, P2):
    if P1 is None:
        return P2
    if P2 is None:
        return P1
    if x(P1) == x(P2) and y(P1) != y(P2):
        return None
    if P1 == P2:
        lam = (3 * x(P1) * x(P1) * pow(2 * y(P1), p - 2, p)) % p
    else:
        lam = ((y(P2) - y(P1)) * pow(x(P2) - x(P1), p - 2, p)) % p
    x3 = (lam * lam - x(P1) - x(P2)) % p
    return (x3, (lam * (x(P1) - x3) - y(P1)) % p)


def point_mul(P, k):
    R = None
    for i in range(256):
        if (k >> i) & 1:
            R = point_add(R, P)
        P = point_add(P, P)
    return R


def bytes_from_int(n_: int) -> bytes:
    return n_.to_bytes(32, byteorder="big")


def bytes_from_point(P) -> bytes:
    return bytes_from_int(x(P))


def int_from_bytes(b: bytes) -> int:
    return int.from_bytes(b, byteorder="big")


def has_even_y(P) -> bool:
    assert not is_infinite(P)
    return y(P) % 2 == 0


def lift_x(x_: int):
    if x_ >= p:
        return None
    y_sq = (pow(x_, 3, p) + 7) % p
    y_ = pow(y_sq, (p + 1) // 4, p)
    if pow(y_, 2, p) != y_sq:
        return None
    return (x_, y_ if y_ % 2 == 0 else p - y_)


def pubkey_gen(seckey: bytes) -> bytes:
    d0 = int_from_bytes(seckey)
    if not (1 <= d0 <= n - 1):
        raise ValueError("secret key out of range")
    P = point_mul(G, d0)
    assert P is not None
    return bytes_from_point(P)


def schnorr_sign(msg: bytes, seckey: bytes, aux_rand: bytes) -> bytes:
    d0 = int_from_bytes(seckey)
    if not (1 <= d0 <= n - 1):
        raise ValueError("secret key out of range")
    P = point_mul(G, d0)
    assert P is not None
    d = d0 if has_even_y(P) else n - d0
    t = bytes(
        a ^ b for a, b in zip(bytes_from_int(d), tagged_hash("BIP0340/aux", aux_rand))
    )
    k0 = (
        int_from_bytes(
            tagged_hash("BIP0340/nonce", t + bytes_from_point(P) + msg)
        )
        % n
    )
    if k0 == 0:
        raise RuntimeError("nonce is zero")
    R = point_mul(G, k0)
    assert R is not None
    k = k0 if has_even_y(R) else n - k0
    e = (
        int_from_bytes(
            tagged_hash(
                "BIP0340/challenge", bytes_from_point(R) + bytes_from_point(P) + msg
            )
        )
        % n
    )
    sig = bytes_from_point(R) + bytes_from_int((k + e * d) % n)
    return sig


def schnorr_verify(msg: bytes, pubkey: bytes, sig: bytes) -> bool:
    if len(pubkey) != 32 or len(sig) != 64:
        return False
    P = lift_x(int_from_bytes(pubkey))
    if P is None:
        return False
    r = int_from_bytes(sig[0:32])
    s = int_from_bytes(sig[32:64])
    if r >= p or s >= n:
        return False
    e = (
        int_from_bytes(tagged_hash("BIP0340/challenge", sig[0:32] + pubkey + msg)) % n
    )
    R = point_add(point_mul(G, s), point_mul(P, n - e))
    if R is None or not has_even_y(R) or x(R) != r:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# bech32 (NIP-19 nsec/npub). Reference impl from BIP-173.
# ─────────────────────────────────────────────────────────────────────────────

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def bech32_polymod(values):
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def bech32_hrp_expand(hrp):
    return [ord(x_) >> 5 for x_ in hrp] + [0] + [ord(x_) & 31 for x_ in hrp]


def bech32_create_checksum(hrp, data):
    values = bech32_hrp_expand(hrp) + data
    polymod = bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def bech32_encode(hrp, data):
    combined = data + bech32_create_checksum(hrp, data)
    return hrp + "1" + "".join([CHARSET[d] for d in combined])


def convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def to_bech32(hrp: str, raw: bytes) -> str:
    return bech32_encode(hrp, convertbits(list(raw), 8, 5))


def from_bech32(expected_hrp: str, s: str) -> bytes:
    pos = s.rfind("1")
    hrp = s[:pos]
    if hrp != expected_hrp:
        raise ValueError(f"expected {expected_hrp}, got {hrp}")
    data = [CHARSET.find(c) for c in s[pos + 1 :]]
    if any(d == -1 for d in data):
        raise ValueError("invalid bech32 char")
    decoded = convertbits(data[:-6], 5, 8, False)
    return bytes(decoded)


# ─────────────────────────────────────────────────────────────────────────────
# NIP-OA
# ─────────────────────────────────────────────────────────────────────────────


def build_preimage(agent_pubkey_hex: str, conditions: str) -> bytes:
    return f"nostr:agent-auth:{agent_pubkey_hex}:{conditions}".encode()


def compute_auth_tag(owner_seckey: bytes, agent_pubkey_hex: str, conditions: str) -> str:
    owner_pub = pubkey_gen(owner_seckey)
    owner_pub_hex = owner_pub.hex()
    if owner_pub_hex == agent_pubkey_hex:
        die("owner and agent pubkeys must differ (self-attestation rejected)")
    preimage = build_preimage(agent_pubkey_hex, conditions)
    msg = hashlib.sha256(preimage).digest()
    # Deterministic aux (32 zero bytes): reproducible tags, still spec-valid.
    sig = schnorr_sign(msg, owner_seckey, bytes(32))
    return json.dumps(["auth", owner_pub_hex, conditions, sig.hex()])


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration: keygen -> attest -> (avatar) -> profile -> env file
# ─────────────────────────────────────────────────────────────────────────────


def die(msg: str):
    sys.stderr.write(f"error: {msg}\n")
    sys.exit(1)


def info(msg: str):
    sys.stderr.write(f">> {msg}\n")


def parse_owner_key(s: str) -> bytes:
    s = s.strip()
    if s.startswith("nsec1"):
        return from_bech32("nsec", s)
    if len(s) == 64:
        try:
            return bytes.fromhex(s)
        except ValueError:
            pass
    die("owner key must be an nsec or 64-char hex secret key")


def read_owner_key() -> bytes:
    """Owner key from a hidden prompt (tty) or piped stdin. Never persisted."""
    if sys.stdin.isatty():
        raw = getpass.getpass("Paste OWNER key (nsec or hex) — input hidden: ")
    else:
        raw = sys.stdin.readline()
    if not raw.strip():
        die("no owner key provided")
    return parse_owner_key(raw)


# ── image sniffing / sanitizing (mirrors Buzz Desktop) ───────────────────────


def sniff_image(path: str):
    """Return ('static'|'gif'|'animated'|'unknown'|'unsupported', mime|None)."""
    with open(path, "rb") as f:
        head = f.read(64)
        f.seek(0)
        body = f.read()
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("animated" if b"acTL" in body else "static", "image/png")
    if head.startswith(b"\xff\xd8\xff"):
        return ("static", "image/jpeg")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        animated = b"ANIM" in body or b"ANMF" in body
        return ("animated" if animated else "static", "image/webp")
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ("gif", "image/gif")
    return ("unsupported", None)


def sanitize_avatar(path: str, note: str = "") -> str:
    """Bake EXIF orientation + strip metadata, matching Buzz Desktop's
    sanitize_image_for_upload. Returns a path to a cleaned temp PNG. Refuses the
    formats the desktop handles with structural sanitizers (GIF, animated)."""
    suffix = f" ({note})" if note else ""
    kind, mime = sniff_image(path)
    if kind == "gif":
        die("GIF avatars need Buzz Desktop's structural sanitizer. "
            "Set an animated/GIF avatar via Desktop, or use a static PNG/JPEG "
            f"here.{suffix}")
    if kind == "animated":
        die("animated PNG/WebP avatars need Buzz Desktop's structural "
            f"sanitizer. Set this one via Desktop, or use a static image here.{suffix}")
    if kind == "unsupported":
        die(f"unsupported avatar type (use a static PNG, JPEG, or WebP){suffix}")

    magick = None
    for candidate in ("magick", "convert"):
        if shutil_which(candidate):
            magick = candidate
            break
    if magick is None:
        die("ImageMagick not found (need 'magick' or 'convert') to sanitize the "
            "avatar the way Buzz Desktop does. Install it, or re-run without "
            f"--avatar and set the picture via Desktop.{suffix}")

    out = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    out.close()
    # -auto-orient == apply_orientation (bake rotation), -strip == drop EXIF/ICC.
    rc = subprocess.run(
        [magick, path, "-auto-orient", "-strip", out.name],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        os.unlink(out.name)
        die(f"image sanitize failed ({magick}): {rc.stderr.strip()}{suffix}")

    # ImageMagick's -strip removes EXIF/comments but can still leave ancillary
    # PNG chunks the relay rejects (pHYs, tIME, and version-dependent others).
    # The relay walks a strict chunk allowlist and returns 422 "media contains
    # metadata or a non-canonical metadata channel" for anything else. So do a
    # deterministic post-pass: rewrite the PNG keeping only allowlisted chunks.
    try:
        canonicalize_png_chunks(out.name)
    except Exception as e:  # noqa: BLE001
        os.unlink(out.name)
        die(f"PNG canonicalization failed: {e}{suffix}")

    # Belt and suspenders: validate against the relay's exact rules locally, so a
    # future encoder quirk surfaces as a clear message here, not a relay 422.
    with open(out.name, "rb") as f:
        ok, why = relay_png_ok(f.read())
    if not ok:
        os.unlink(out.name)
        die(f"sanitized PNG still fails the relay's metadata check ({why}). "
            f"This is a bug in the sanitizer — please report it.{suffix}")
    return out.name


# PNG chunks the relay's validator accepts (crates/buzz-media/src/validation.rs
# ::validate_png_metadata_free). Critical (uppercase) chunks are always kept;
# these are the ONLY ancillary chunks allowed. Everything else (pHYs, tIME,
# eXIf, tEXt/zTXt/iTXt, iCCP, unknown ancillary) is stripped.
_PNG_ALLOWED_ANCILLARY = {
    b"cHRM", b"gAMA", b"sBIT", b"sRGB", b"bKGD", b"hIST",
    b"tRNS", b"sPLT", b"acTL", b"fcTL", b"fdAT",
}


def relay_png_ok(data: bytes):
    """Faithful port of the relay's validate_png_metadata_free
    (crates/buzz-media/src/validation.rs). Returns (ok, reason)."""
    sig = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(sig):
        return (False, "bad signature")
    i = len(sig)
    saw_iend = False
    saw_snapshot = False
    snap_keywords = (b"buzz_agent_snapshot", b"buzz_team_snapshot")
    while i < len(data):
        if i + 12 > len(data):
            return (False, "truncated chunk")
        length = int.from_bytes(data[i:i + 4], "big")
        kind = data[i + 4:i + 8]
        end = i + 12 + length
        if end > len(data):
            return (False, "chunk length overruns file")
        if kind == b"tEXt":
            payload = data[i + 8:end - 4]
            is_snap = any(payload.startswith(k + b"\x00") for k in snap_keywords)
            if saw_snapshot or not is_snap:
                return (False, "forbidden tEXt")
            saw_snapshot = True
            i = end
            continue
        if kind in (b"eXIf", b"zTXt", b"iTXt", b"iCCP"):
            return (False, f"forbidden {kind.decode('latin1')}")
        ancillary = bool(kind[0] & 0x20)
        if ancillary and kind not in _PNG_ALLOWED_ANCILLARY:
            return (False, f"forbidden ancillary {kind.decode('latin1')}")
        i = end
        if kind == b"IEND":
            saw_iend = True
            break
    if not saw_iend or i != len(data):
        return (False, "trailing bytes or missing IEND")
    return (True, "ok")


def canonicalize_png_chunks(path: str):
    """Rewrite a PNG in place, keeping only critical chunks + the relay's
    ancillary allowlist. Drops pHYs, tIME, text, ICC, and any unknown ancillary
    chunk, so the result passes the relay's metadata-free validator."""
    with open(path, "rb") as f:
        data = f.read()
    sig = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(sig):
        raise ValueError("not a PNG after sanitize")
    out = bytearray(sig)
    i = len(sig)
    saw_iend = False
    while i < len(data):
        if i + 12 > len(data):
            raise ValueError("truncated PNG chunk")
        length = int.from_bytes(data[i:i + 4], "big")
        kind = data[i + 4:i + 8]
        end = i + 12 + length
        if end > len(data):
            raise ValueError("PNG chunk length overruns file")
        is_critical = not (kind[0] & 0x20)  # uppercase 1st byte = critical
        if is_critical or kind in _PNG_ALLOWED_ANCILLARY:
            out += data[i:end]
        # else: drop the chunk entirely
        i = end
        if kind == b"IEND":
            saw_iend = True
            break
    if not saw_iend:
        raise ValueError("no IEND chunk")
    with open(path, "wb") as f:
        f.write(out)


def shutil_which(name):
    from shutil import which
    return which(name)


# ── buzz CLI calls ───────────────────────────────────────────────────────────


def buzz(args, env, capture=True):
    buzz_bin = os.environ.get("BUZZ_BIN", "buzz")
    rc = subprocess.run([buzz_bin, *args], env=env,
                        capture_output=capture, text=True)
    if rc.returncode != 0:
        err = (rc.stderr or rc.stdout or "").strip()
        die(f"`buzz {' '.join(args)}` failed: {err}")
    return rc.stdout if capture else ""


def extract_url(text: str):
    import re
    m = re.search(r'https?://[^\s"]+', text)
    return m.group(0) if m else None


# ── subcommands ──────────────────────────────────────────────────────────────


def cmd_keygen(_args):
    while True:
        sk = secrets.token_bytes(32)
        d = int_from_bytes(sk)
        if 1 <= d <= n - 1:
            break
    pub = pubkey_gen(sk)
    print(f"AGENT_NSEC={to_bech32('nsec', sk)}")
    print(f"AGENT_NPUB={to_bech32('npub', pub)}")
    print(f"AGENT_PUBKEY_HEX={pub.hex()}")


def cmd_attest(args):
    if len(args.agent_pubkey) != 64:
        die("--agent-pubkey must be 64-char hex")
    owner_seckey = read_owner_key()
    print(compute_auth_tag(owner_seckey, args.agent_pubkey, args.conditions))


def cmd_provision(args):
    relay = os.environ.get("BUZZ_RELAY_URL")
    if not relay:
        die("BUZZ_RELAY_URL not set")
    if not shutil_which(os.environ.get("BUZZ_BIN", "buzz")):
        die("`buzz` CLI not found on PATH")

    # 1. fresh agent identity
    info("generating fresh agent keypair")
    while True:
        sk = secrets.token_bytes(32)
        if 1 <= int_from_bytes(sk) <= n - 1:
            break
    agent_nsec = to_bech32("nsec", sk)
    agent_pub = pubkey_gen(sk)
    agent_npub = to_bech32("npub", agent_pub)
    agent_hex = agent_pub.hex()
    sys.stderr.write(f"   agent npub: {agent_npub}\n")

    # 2. owner attestation (owner key read here, used once, never persisted)
    owner_seckey = read_owner_key()
    info("signing NIP-OA owner attestation")
    auth_tag = compute_auth_tag(owner_seckey, agent_hex, args.conditions)
    del owner_seckey  # drop from memory

    # env the buzz CLI needs to act as this agent
    child_env = dict(os.environ)
    child_env["BUZZ_RELAY_URL"] = relay
    child_env["BUZZ_PRIVATE_KEY"] = agent_nsec
    child_env["BUZZ_AUTH_TAG"] = auth_tag

    # 3. persist the identity NOW — before any network call. The key + tag are
    # the only unrecoverable artifacts; once written, no later failure (avatar,
    # upload, profile) can orphan a published-but-lost key. Everything after
    # this point is retryable by hand with the saved env file.
    out_path = args.out or f"./{args.name}.env"
    write_env_file(out_path, args.name, relay, agent_nsec, auth_tag,
                   agent_npub, agent_hex)
    info(f"identity saved to {out_path} (mode 0600) — safe to retry from here")

    # 4. avatar (optional) — sanitized to match Buzz Desktop
    avatar_url = None
    if args.avatar:
        if not os.path.isfile(args.avatar):
            die(f"avatar file not found: {args.avatar} "
                f"(identity already saved to {out_path}; re-run or set the "
                f"avatar by hand)")
        clean = sanitize_avatar(args.avatar, note=f"identity already saved to {out_path}")
        try:
            info("uploading avatar to Blossom (sanitized)")
            out = buzz(["upload", "file", "--file", clean], child_env)
            avatar_url = extract_url(out)
            if not avatar_url:
                die(f"could not parse avatar URL from upload output:\n{out}")
            sys.stderr.write(f"   avatar url: {avatar_url}\n")
        finally:
            if os.path.exists(clean):
                os.unlink(clean)

    # 5. publish profile (carries the auth tag via BUZZ_AUTH_TAG)
    info("publishing agent profile")
    prof = ["users", "set-profile", "--name", args.name]
    if args.about:
        prof += ["--about", args.about]
    if avatar_url:
        prof += ["--avatar", avatar_url]
    if args.nip05:
        prof += ["--nip05", args.nip05]
    buzz(prof, child_env, capture=False)

    sys.stderr.write(
        f"\n>> done. agent identity saved at: {out_path} (mode 0600)\n"
        f"   npub: {agent_npub}\n"
        f"   verify: BUZZ_PRIVATE_KEY={agent_nsec} "
        f"{os.environ.get('BUZZ_BIN', 'buzz')} users get\n"
    )


def write_env_file(out_path, name, relay, agent_nsec, auth_tag, agent_npub, agent_hex):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = (
        f'# Buzz agent identity for "{name}" — generated {ts}\n'
        f"# Source this where the agent runs:  set -a; source {out_path}; set +a\n"
        f"# CONTAINS A SECRET (nsec). Do not commit.\n"
        f"BUZZ_RELAY_URL={relay}\n"
        f"BUZZ_PRIVATE_KEY={agent_nsec}\n"
        f"BUZZ_AUTH_TAG={auth_tag}\n"
        f"# npub (public):  {agent_npub}\n"
        f"# pubkey hex:     {agent_hex}\n"
    )
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


def build_parser():
    p = argparse.ArgumentParser(
        prog="buzz-agent-identity.py",
        description="Provision a Buzz agent identity (separate key + owner "
        "attestation + profile). Pure stdlib; needs the `buzz` CLI, and "
        "ImageMagick only when using --avatar.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("keygen", help="generate a fresh agent keypair").set_defaults(
        func=cmd_keygen
    )

    a = sub.add_parser(
        "attest", help="sign a NIP-OA auth tag (owner key from stdin/prompt)"
    )
    a.add_argument("--agent-pubkey", required=True, help="64-char hex")
    a.add_argument("--conditions", default="", help="NIP-OA conditions string")
    a.set_defaults(func=cmd_attest)

    r = sub.add_parser("provision", help="full flow: keygen + attest + avatar + profile")
    r.add_argument("--name", required=True, help="display name")
    r.add_argument("--about", default="", help="bio / about text")
    r.add_argument("--avatar", default="", help="path to a static PNG/JPEG/WebP")
    r.add_argument("--nip05", default="", help="NIP-05 id, e.g. dev@lucian.earth")
    r.add_argument(
        "--conditions",
        default="",
        help="NIP-OA conditions (empty = unconstrained; e.g. 'kind=0')",
    )
    r.add_argument("--out", default="", help="env file path (default ./<name>.env)")
    r.set_defaults(func=cmd_provision)
    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
