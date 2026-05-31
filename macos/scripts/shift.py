#!/usr/bin/env python3
"""
shift.py — Shift / Unshift a device through the Shift (shiftyourphone.com) API,
handling token refresh automatically.

It reuses the credentials that the Shift Desktop app already stored on this Mac:
  ~/Library/Application Support/Shift Desktop/user.encrypted
which is encrypted with Electron's safeStorage (AES-128-CBC, key in the login
Keychain under "Shift Desktop Safe Storage"). A plaintext user.json fallback is
also supported.

Because the access token only lives 15 minutes, every run first mints a fresh
one from the long-lived refresh token via POST /auth/refresh, then (optionally)
writes the rotated tokens back so the desktop app stays in sync.

Usage:
  python3 shift.py status                 # show account + devices
  python3 shift.py devices                # list devices (serial / name / state)
  python3 shift.py shift                  # shift the first/only device
  python3 shift.py unshift                # unshift the first/only device
  python3 shift.py shift   --serial XXXX  # target a specific device
  python3 shift.py unshift --name "Furkan's iPhone"
  python3 shift.py token                  # print a fresh access token only

Options:
  --serial S     device serial to target
  --name N       device name to target (substring match, case-insensitive)
  --no-save      do NOT write refreshed tokens back to user.encrypted
  --json         raw JSON output
  -v/--verbose   print request details

No third-party packages required (stdlib + the macOS `security` & `openssl` CLIs).
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

API_BASE = "https://api.shiftyourphone.com/v1"

# Cloudflare in front of the API blocks non-browser clients (error 1010) by
# User-Agent signature, so we present a browser/Electron-style UA like the app.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Shift Desktop/3.0.0 Chrome/138.0.0.0 "
    "Electron/39.2.7 Safari/537.36"
)

# Candidate userData dir names (Electron app name can vary).
APP_DIR_NAMES = ["Shift Desktop", "shift-electron", "Shift"]
# Candidate Keychain "Safe Storage" service names.
KEYCHAIN_SERVICES = [
    "Shift Desktop Safe Storage",
    "shift-electron Safe Storage",
    "Shift Safe Storage",
]

VERBOSE = False


def log(*a):
    if VERBOSE:
        print("[debug]", *a, file=sys.stderr)


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------- #
# Credential storage (read + write user.encrypted / user.json)
# --------------------------------------------------------------------------- #
def app_support_dir():
    base = os.path.expanduser("~/Library/Application Support")
    for name in APP_DIR_NAMES:
        d = os.path.join(base, name)
        if os.path.exists(os.path.join(d, "user.encrypted")) or os.path.exists(
            os.path.join(d, "user.json")
        ):
            log("userData dir:", d)
            return d
    die(f"could not find Shift userData dir under {base}")


def _keychain_password():
    for svc in KEYCHAIN_SERVICES:
        try:
            pw = subprocess.check_output(
                ["security", "find-generic-password", "-s", svc, "-w"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            if pw:
                log("keychain service:", svc)
                return pw
        except subprocess.CalledProcessError:
            continue
    die("could not read the Safe Storage key from Keychain "
        "(macOS may prompt for access — click Allow, then retry)")


def _crypto_params():
    pw = _keychain_password()
    key = hashlib.pbkdf2_hmac("sha1", pw.encode(), b"saltysalt", 1003, 16)
    iv = b" " * 16
    return key.hex(), iv.hex()


def _openssl(args, data):
    p = subprocess.run(
        ["openssl"] + args,
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if p.returncode != 0:
        die("openssl failed: " + p.stderr.decode(errors="replace"))
    return p.stdout


def load_creds(data_dir):
    """Return (creds_dict, encrypted_bool)."""
    enc = os.path.join(data_dir, "user.encrypted")
    plain = os.path.join(data_dir, "user.json")

    if os.path.exists(plain):
        log("reading plaintext user.json")
        with open(plain, "r") as f:
            return json.load(f), False

    if os.path.exists(enc):
        log("decrypting user.encrypted")
        with open(enc, "rb") as f:
            raw = f.read()
        body = raw[3:]  # strip the 3-byte "v10" version tag
        keyhex, ivhex = _crypto_params()
        out = _openssl(
            ["enc", "-d", "-aes-128-cbc", "-K", keyhex, "-iv", ivhex], body
        )
        return json.loads(out.decode("utf-8")), True

    die(f"no user.encrypted or user.json in {data_dir}")


def save_creds(data_dir, creds, encrypted):
    """Atomically write creds back, matching the app's storage format."""
    payload = json.dumps(creds).encode("utf-8")
    if encrypted:
        keyhex, ivhex = _crypto_params()
        ct = _openssl(
            ["enc", "-aes-128-cbc", "-K", keyhex, "-iv", ivhex], payload
        )
        blob = b"v10" + ct
        path = os.path.join(data_dir, "user.encrypted")
    else:
        blob = payload
        path = os.path.join(data_dir, "user.json")

    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".user.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)
        log("wrote refreshed tokens to", os.path.basename(path))
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        die(f"failed to persist refreshed tokens: {e}")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http(method, path, token=None, body=None):
    url = API_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    log(method, url)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"error": raw or e.reason}
    except urllib.error.URLError as e:
        die(f"network error: {e.reason}")


# --------------------------------------------------------------------------- #
# Token helpers
# --------------------------------------------------------------------------- #
def jwt_exp(tok):
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def access_token_valid(tok, skew=60):
    if not tok:
        return False
    exp = jwt_exp(tok)
    return exp is not None and exp - time.time() > skew


def refresh_tokens(data_dir, creds, encrypted, save=True):
    """Mint a fresh access token from the refresh token; persist rotation."""
    rt = creds.get("refreshToken")
    if not rt:
        die("no refreshToken stored — sign in via the app first")
    status, resp = http("POST", "/auth/refresh", body={"refreshToken": rt})
    if status != 200 or not resp.get("accessToken"):
        die(f"refresh failed ({status}): {resp.get('error', resp)} — "
            "the refresh token may be expired/revoked; sign in again in the app")
    creds["accessToken"] = resp["accessToken"]
    if resp.get("refreshToken"):
        creds["refreshToken"] = resp["refreshToken"]
    log("refreshed access token")
    if save:
        save_creds(data_dir, creds, encrypted)
    return creds["accessToken"]


def ensure_token(data_dir, creds, encrypted, save=True):
    """Use the stored access token if still valid, else refresh."""
    tok = creds.get("accessToken")
    if access_token_valid(tok):
        log("stored access token still valid")
        return tok
    log("stored access token missing/expired — refreshing")
    return refresh_tokens(data_dir, creds, encrypted, save=save)


# --------------------------------------------------------------------------- #
# API actions
# --------------------------------------------------------------------------- #
def get_me(token):
    status, resp = http("GET", "/me", token=token)
    if status != 200:
        return status, resp
    return status, resp


def extract_devices(me):
    if not isinstance(me, dict):
        return []
    return me.get("devices") or (me.get("user") or {}).get("devices") or []


def pick_device(devices, serial=None, name=None):
    if not devices:
        die("no devices on this account")
    if serial:
        for d in devices:
            if d.get("serial") == serial:
                return d
        die(f"no device with serial {serial}")
    if name:
        nl = name.lower()
        matches = [d for d in devices
                   if nl in (d.get("name") or d.get("deviceName") or "").lower()]
        if not matches:
            die(f"no device matching name '{name}'")
        if len(matches) > 1:
            die(f"name '{name}' matched {len(matches)} devices — use --serial")
        return matches[0]
    if len(devices) > 1:
        names = ", ".join(
            f"{d.get('serial')} ({d.get('name') or d.get('deviceName') or '?'})"
            for d in devices
        )
        die(f"multiple devices — pick one with --serial or --name: {names}")
    return devices[0]


def device_state(d):
    if d.get("isShifting"):
        return "shifting"
    if d.get("isUnshifting"):
        return "unshifting"
    if d.get("blocker"):
        return f"shifted ({d['blocker'].get('reason', '?')})"
    if d.get("shifted"):
        return "shifted"
    return "unshifted"


def do_action(data_dir, creds, encrypted, action, serial=None, name=None,
              save=True, as_json=False):
    token = ensure_token(data_dir, creds, encrypted, save=save)
    status, me = get_me(token)
    if status == 401:  # token rejected despite our check — refresh once, retry
        token = refresh_tokens(data_dir, creds, encrypted, save=save)
        status, me = get_me(token)
    if status != 200:
        die(f"GET /me failed ({status}): {me.get('error', me)}")

    dev = pick_device(extract_devices(me), serial=serial, name=name)
    sid = dev.get("serial")
    print(f"-> {action} '{dev.get('name') or dev.get('deviceName') or sid}' "
          f"(serial {sid}, currently {device_state(dev)})", file=sys.stderr)

    status, resp = http("POST", f"/me/devices/{sid}/{action}", token=token,
                        body={})
    if status == 401:
        token = refresh_tokens(data_dir, creds, encrypted, save=save)
        status, resp = http("POST", f"/me/devices/{sid}/{action}", token=token,
                            body={})
    if status not in (200, 201, 202):
        die(f"{action} failed ({status}): {resp.get('error', resp)}")

    if as_json:
        print(json.dumps(resp, indent=2))
    else:
        print(f"OK — {action} request accepted for {sid}")
    return resp


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    global VERBOSE
    ap = argparse.ArgumentParser(
        description="Shift / Unshift a device via the Shift API (auto-refresh)."
    )
    ap.add_argument("command",
                    choices=["status", "devices", "shift", "unshift",
                             "refresh", "token"])
    ap.add_argument("--serial", help="target device serial")
    ap.add_argument("--name", help="target device name (substring match)")
    ap.add_argument("--no-save", action="store_true",
                    help="don't write refreshed tokens back to disk")
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose
    save = not args.no_save

    data_dir = app_support_dir()
    creds, encrypted = load_creds(data_dir)

    if args.command == "token":
        print(ensure_token(data_dir, creds, encrypted, save=save))
        return

    if args.command == "refresh":
        tok = refresh_tokens(data_dir, creds, encrypted, save=save)
        exp = jwt_exp(tok)
        when = time.strftime("%H:%M:%S", time.localtime(exp)) if exp else "?"
        print(f"refreshed — new access token valid until ~{when}")
        return

    if args.command in ("status", "devices"):
        token = ensure_token(data_dir, creds, encrypted, save=save)
        status, me = get_me(token)
        if status == 401:
            token = refresh_tokens(data_dir, creds, encrypted, save=save)
            status, me = get_me(token)
        if status != 200:
            die(f"GET /me failed ({status}): {me.get('error', me)}")
        if args.json:
            print(json.dumps(me, indent=2))
            return
        devices = extract_devices(me)
        if args.command == "status":
            user = me.get("user") or me
            print(f"account: {user.get('email', '?')}  (userId {user.get('id', '?')})")
        if not devices:
            print("no devices")
            return
        for d in devices:
            print(f"  {d.get('serial')}  "
                  f"{(d.get('name') or d.get('deviceName') or '?'):24}  "
                  f"{device_state(d)}")
        return

    # shift / unshift
    do_action(data_dir, creds, encrypted, args.command,
              serial=args.serial, name=args.name, save=save, as_json=args.json)


if __name__ == "__main__":
    main()
