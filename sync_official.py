"""Mirrors the official level bundles into levels/official/ (run by .github/workflows/sync.yml)."""
import argparse
import gzip
import hashlib
import io
import json
import os
import re
import socket
import tarfile
import time
import urllib.request
from base64 import b64decode
from pathlib import Path

from compiledtext import decode
from fetch_real_hash import e_decrypt, e_encrypt, send

socket.setdefaulttimeout(20)

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.json"
STATE = ROOT / "levels" / "state.json"
COMMIT_MSG = ROOT / ".sync_message"

CDN_HOSTS = {
    "ad": "https://pvz2adcdn.ditwan.cn",
    "ios": "https://pvz2cdn.ditwan.cn",
}
LATEST_URL = "https://pvz2.ditwan.cn/backend/api/latest_version/get_latest_version"
IOS_LOOKUP_URL = "https://itunes.apple.com/lookup?id=639516529&country=cn"
LP_RE = re.compile(r"^hotupdate/(ad|ios)/level_shipping/([^/]+)/([0-9a-f]+)$")


def vkey(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def query_hash(cv: str, platform: str) -> str | None:
    """Hash the server serves for cv, or None if it serves none."""
    for _ in range(2):
        try:
            e = e_encrypt(json.dumps({"cv": cv, "t": "0"}).encode())
            resp = json.loads(send(e, platform))
            plain = e_decrypt(resp["e"])
            lp = (json.loads(plain).get("d") or {}).get("lp") if plain else None
            m = LP_RE.match(lp or "")
            return m.group(3) if m and m.group(2) == cv else None
        except Exception as ex:
            print(f"  ! {platform} {cv}: {ex}", flush=True)
    return None


def latest_android() -> str | None:
    req = urllib.request.Request(
        LATEST_URL, data=b"", method="POST",
        headers={"Content-Type": "application/json;charset=UTF-8", "token": "null",
                 "Origin": "https://pvz2.ditwan.cn", "Referer": "https://pvz2.ditwan.cn/",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/154.0.0.0 Safari/537.36"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = json.load(resp)["latestVersions"]
        vs = [r["version"] for r in rows if r.get("qudao") != "iOS" and r.get("status") == 1
              and re.fullmatch(r"\d+\.\d+\.\d+", r.get("version", "")) and r["version"] != "0.0.0"]
        return max(vs, key=vkey) if vs else None
    except Exception as ex:
        print(f"  ! latest-version lookup failed ({ex})")
        return None


def latest_ios() -> str | None:
    try:
        with urllib.request.urlopen(IOS_LOOKUP_URL, timeout=30) as resp:
            v = json.load(resp)["results"][0]["version"]
        return ".".join(v.split(".")[:3])
    except Exception as ex:
        print(f"  ! App Store lookup failed ({ex})")
        return None


def download(platform: str, cv: str, h: str) -> bytes:
    url = f"{CDN_HOSTS[platform]}/hotupdate/{platform}/level_shipping/{cv}/{h}.txt"
    req = urllib.request.Request(url, headers={"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 14)"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except OSError:
            if attempt == 2:
                raise
            time.sleep(3)


def _b64(text: bytes) -> bytes:
    s = text.decode("ascii").strip().translate(str.maketrans({",": "=", "-": "+", "_": "/"}))
    return b64decode(s + "=" * (-len(s) % 4))


def unpack(bundle: bytes) -> dict[str, bytes]:
    tar_bytes = gzip.decompress(_b64(gzip.decompress(_b64(bundle))))
    levels = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        for m in tar.getmembers():
            if not m.isfile() or not m.name.endswith(".json"):
                continue
            levels[Path(m.name).name] = decode(_b64(tar.extractfile(m).read()))
    return levels


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text("utf-8"))
    state = json.loads(STATE.read_text("utf-8")) if STATE.exists() else {}
    log: list[str] = []
    prev_keys = set(state)

    release = latest_android()
    for platform in cfg["platforms"]:
        latest = {"ad": lambda: release, "ios": latest_ios}[platform]()
        if not latest:
            continue
        print(f"{platform}: latest official version is {latest}")
        h = query_hash(latest, platform)
        if not h:
            print(f"  no official bundle for {latest} yet")
            continue

        key = f"{platform}/{latest}"
        prev = state.get(key, {"hash": None, "files": {}})
        folder = ROOT / "levels" / "official" / platform / latest
        if prev["hash"] == h and folder.is_dir():
            continue

        print(f"{key}: {'NEW VERSION' if key not in state else 'changed/missing'} (hash {h[:12]})")
        try:
            levels = unpack(download(platform, latest, h))
        except Exception as ex:
            print(f"  ! {key}: download/unpack failed: {ex}")
            continue
        added, updated = [], []

        for name, plain in sorted(levels.items()):
            path = folder / name
            if not path.exists():
                added.append(name)
            elif prev["files"].get(name) == sha(path.read_bytes()) and prev["files"][name] != sha(plain):
                updated.append(name)
            else:
                continue
            if not args.dry_run:
                folder.mkdir(parents=True, exist_ok=True)
                path.write_bytes(plain)

        state[key] = {"hash": h, "files": {n: sha(p) for n, p in levels.items()}}
        line = f"{key}: {len(levels)} levels in bundle"
        if key not in prev_keys:
            line += " (new game version)"
        if added:
            line += f", added {', '.join(added[:8])}" + (f" +{len(added) - 8} more" if len(added) > 8 else "")
        if updated:
            line += f", updated {', '.join(updated)}"
        log.append(line)

    if not log:
        print("nothing new.")
        return
    if args.dry_run:
        print("\n".join(log))
        return

    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", "utf-8")
    COMMIT_MSG.write_text("sync official levels\n\n" + "\n".join(log) + "\n", "utf-8")
    print("\n".join(log))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("### Official sync\n" + "\n".join(f"- {l}" for l in log) + "\n")


if __name__ == "__main__":
    main()
