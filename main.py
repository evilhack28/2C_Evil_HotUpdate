"""Packs level JSON into a level_shipping bundle. Usage: python main.py <cv> <src_dir> <out_dir> [--hash <hash>] [--mod <name>]

Without --mod the official files are packed as they are. With --mod the mod's changes (levels/mods/<name>/) are applied on top of
the official files first, so the bundle is the newest official content plus the mod. With --all-mods (and --platform) every mod is
built in one go into <out_dir>/<mod>/hotupdate/<platform>/level_shipping/<version>/, encoding each distinct file only once.
"""
import gzip
import hashlib
import json
import sys
import tarfile
import tempfile
from base64 import b64encode, urlsafe_b64encode
from pathlib import Path

from compiledtext import encode, decode

# Most files are identical across mods (a mod usually changes one or a few), so each distinct content is encoded once per run.
_encoded: dict[bytes, bytes] = {}


def _member_b64(name: str, plain: bytes) -> bytes:
    key = hashlib.sha256(plain).digest()
    if key not in _encoded:
        member = encode(plain)
        if decode(member) != plain:
            sys.exit(f"self-check failed encoding {name} -- refusing to publish a broken bundle")
        _encoded[key] = b64encode(member)
    return _encoded[key]


def build_bundle(cv: str, src_dir: Path, out_dir: Path, override_hash: str | None = None, mod: str | None = None, quiet: bool = False) -> None:
    if mod:
        import modlib
        files = modlib.mod_files(modlib.load_mod(mod), src_dir)
    else:
        files = {p.name: p.read_bytes() for p in sorted(src_dir.glob("*.json"))}
    if not files:
        sys.exit(f"no *.json files found in {src_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_list = []
    with tempfile.TemporaryDirectory() as work_str:
        work = Path(work_str)
        for name, plain in files.items():
            member_b64 = _member_b64(name, plain)
            (work / name).write_bytes(member_b64)
            manifest_list.append({"Name": name, "Hash": hashlib.md5(member_b64).hexdigest()})
            if not quiet:
                print(f"  encoded {name} ({len(plain)} -> {len(member_b64)} bytes)")

        tar_path = work / "bundle.tar"
        with tarfile.open(tar_path, "w", format=tarfile.USTAR_FORMAT) as tar:
            for name in files:
                tar.add(work / name, arcname=name)
        tar_bytes = tar_path.read_bytes()
    wrapped = urlsafe_b64encode(gzip.compress(tar_bytes)).decode("ascii").replace("=", ",")
    wrapped = urlsafe_b64encode(gzip.compress(wrapped.encode("ascii"))).decode("ascii").replace("=", ",")
    bundle_text = wrapped

    bundle_hash = override_hash if override_hash else hashlib.sha256(bundle_text.encode("ascii")).hexdigest()

    manifest_json = json.dumps({"File": {"List": manifest_list}}, separators=(",", ":"))
    manifest_blob = b64encode(encode(manifest_json.encode("utf-8"))).decode("ascii")
    manifest_blob = manifest_blob.replace("+", "-").replace("/", "_").replace("=", ",")

    (out_dir / f"{bundle_hash}.txt").write_text(bundle_text, encoding="ascii")
    (out_dir / f"{bundle_hash}_md5.txt").write_text(manifest_blob, encoding="ascii")

    print(f"{'  ' + mod + ': ' if mod else ''}wrote {out_dir / (bundle_hash + '.txt')} (+ _md5.txt)")


def build_all_mods(cv: str, src_dir: Path, dist_dir: Path, platform: str, bundle_hash: str) -> None:
    import modlib
    for mod in modlib.list_mods():
        build_bundle(cv, src_dir, dist_dir / mod / "hotupdate" / platform / "level_shipping" / cv, bundle_hash, mod, quiet=True)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Pack plaintext level JSON into a level_shipping bundle.")
    ap.add_argument("cv")
    ap.add_argument("src_dir", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--hash", dest="override_hash")
    ap.add_argument("--mod")
    ap.add_argument("--all-mods", action="store_true")
    ap.add_argument("--platform")
    args = ap.parse_args()
    if args.all_mods:
        if not (args.platform and args.override_hash):
            ap.error("--all-mods needs --platform and --hash")
        build_all_mods(args.cv, args.src_dir, args.out_dir, args.platform, args.override_hash)
    else:
        build_bundle(args.cv, args.src_dir, args.out_dir, args.override_hash, args.mod)
