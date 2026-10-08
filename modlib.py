"""Mod engine: derives a mod's level files from the newest official ones.

Layout:
    levels/official/<platform>/<version>/*.json   official files, shared by every mod (synced)
    levels/mods/PLANTTYPES.json                   plant list used by the fill_plants op
    levels/mods/<name>/mod.json                   {"description", "includes": [mods], "rules": [{"match": [globs],
                                                  "exclude": [globs], "ops": [{"op": ..., args}]}]}
    levels/mods/<name>/files/*.json               optional hand-made files that replace the result outright
Ops: max_plant_level, no_reload, max_buffs, fill_plants, set_global_level(value), regex_sub(pattern, repl, flags).
Each mod is published at <site>/<name>/hotupdate/<platform>/level_shipping/<version>/ -- add a mod by adding a folder.

A mod is never stored as a modified copy: it is re-derived from the official files on every build,
so a new official level or an official edit lands in every mod while the mod's changes stay applied.
"""
import fnmatch
import json
import re
from pathlib import Path

MODS_DIR = Path(__file__).parent / "levels" / "mods"
PLANTTYPES = MODS_DIR / "PLANTTYPES.json"


def _dump(data: dict) -> str:
    return json.dumps(data, indent=4, ensure_ascii=False) + "\n"


def _objects(content: str) -> tuple[dict | None, list]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None, []
    objects = data.get("objects") if isinstance(data, dict) else None
    return (data, objects) if isinstance(objects, list) else (None, [])


def plant_names() -> list[str]:
    data = json.loads(PLANTTYPES.read_text("utf-8"))
    names = {o["objdata"]["TypeName"].strip() for o in data.get("objects", [])
             if isinstance(o.get("objdata", {}).get("TypeName"), str) and o["objdata"]["TypeName"].strip()}
    return sorted(names)


# --- ops: each takes (content, **args) and returns the new content -------------------------------

def op_regex_sub(content: str, pattern: str, repl: str, flags: str = "") -> str:
    return re.sub(pattern, repl, content, flags=sum((getattr(re, f.upper()) for f in flags), 0))


def op_max_plant_level(content: str) -> str:
    """Every plant starts at its MaxLevel."""
    return re.sub(r'("MinLevel":\s*)\d+(\s*,\s*"MaxLevel":\s*)(\d+)', r'\g<1>\3\2\3', content)


def op_no_reload(content: str) -> str:
    """0 cost, 0 cooldown, and no stage blacklists so every plant works everywhere (incl. the moon)."""
    for key in ("Cost", "PowerCost", "PacketCooldown", "StartingCooldown"):
        content = re.sub(rf'("{key}":\s*)\d+(?:\.\d+)?', r'\g<1>0', content)
    return re.sub(r'("BlackListStages":\s*\[)[^\]]*(\])', r'\1\2', content)


def op_max_buffs(content: str) -> str:
    """Cherry Bomb is a 1-level, never-dying 9999999-damage bomb; dart plants fire 99999 darts."""
    content = re.sub(r'(?s)("aliases":\s*\[\s*"CherryBombDefault".*?)("MinLevel":\s*)\d+(\s*,\s*"MaxLevel":\s*)\d+',
                     r'\1\g<2>1\3 1', content)
    content = re.sub(r'(?s)("aliases":\s*\[\s*"CherryBombDefault".*?"Actions":\s*\[\s*\{[^}]*?"ExplodeType":\s*"[^"]*",'
                     r'\s*)("ExplodeRadius":\s*)[^,]+(,\s*"Burns":\s*true,\s*"Damage":\s*)\d+',
                     r'\1"DieAfterExplode": false,\g<2>999\3 9999999', content)
    for key in ("DartCount", "DartCountLv2", "DartCountLv3", "PiercingDamage"):
        content = re.sub(rf'("{key}":\s*)\d+', r'\g<1>99999', content)
    return content


def op_fill_plants(content: str) -> str:
    """Every plant allowed in every level: full whitelist, empty black/prior lists, full conveyor lists."""
    names = plant_names()
    body = "" if not names else ("\n\t\t\t\t\t" + ",\n\t\t\t\t\t".join(f'"{n}"' for n in names) + "\n\t\t\t\t")
    content = re.sub(r'("PlantWhiteList"\s*:\s*\[)[^\]]*(\])', lambda m: m.group(1) + body + m.group(2), content, flags=re.DOTALL)
    content = re.sub(r'("PlantBlackList"\s*:\s*\[)[^\]]*(\])', r'\1\2', content, flags=re.DOTALL)
    content = re.sub(r'("PlantPriorList"\s*:\s*\[)[^\]]*(\])', r'\1\2', content, flags=re.DOTALL)
    # Conveyor-belt seed banks ignore the whitelist and draw from InitialPlantList instead
    data, objects = _objects(content)
    changed = False
    for obj in objects:
        objdata = obj.get("objdata") if isinstance(obj, dict) else None
        if isinstance(objdata, dict) and "InitialPlantList" in objdata:
            objdata["InitialPlantList"] = [{"PlantType": n, "Weight": 10} for n in names]
            changed = True
    return _dump(data) if changed else content


def op_set_global_level(content: str, value: int) -> str:
    """Forces the plant level: replaces GlobalLevel where present, adds it to every *SeedBankProperties object lacking it."""
    content = re.sub(r'("GlobalLevel"\s*:\s*)\d+', rf'\g<1>{int(value)}', content)
    data, objects = _objects(content)
    inserted = False
    for obj in objects:
        if isinstance(obj, dict) and str(obj.get("objclass", "")).endswith("SeedBankProperties"):
            objdata = obj.setdefault("objdata", {})
            if "GlobalLevel" not in objdata:
                objdata["GlobalLevel"] = int(value)
                inserted = True
    return _dump(data) if inserted else content


OPS = {name[3:]: fn for name, fn in globals().items() if name.startswith("op_")}


# --- mods ----------------------------------------------------------------------------------------

def load_mod(name: str, _seen: tuple = ()) -> dict:
    """The mod's rules (its includes first) and the folder holding its override files."""
    if name in _seen:
        raise ValueError(f"mod include cycle: {' -> '.join(_seen + (name,))}")
    folder = MODS_DIR / name
    spec = json.loads((folder / "mod.json").read_text("utf-8"))
    rules: list[dict] = []
    for inc in spec.get("includes", []):
        rules += load_mod(inc, _seen + (name,))["rules"]
    return {"rules": rules + spec.get("rules", []), "files_dir": folder / "files"}


def list_mods() -> list[str]:
    return sorted(p.parent.name for p in MODS_DIR.glob("*/mod.json"))


def _matches(rule: dict, filename: str) -> bool:
    name = filename.lower()
    return (any(fnmatch.fnmatch(name, p.lower()) for p in rule.get("match", ["*.json"]))
            and not any(fnmatch.fnmatch(name, p.lower()) for p in rule.get("exclude", [])))


def apply_mod(mod: dict, filename: str, plain: bytes) -> bytes:
    """Official file bytes -> the mod's bytes for that file (unchanged when no rule matches)."""
    override = mod["files_dir"] / filename
    if override.is_file():
        return override.read_bytes()
    rules = [r for r in mod["rules"] if _matches(r, filename)]
    if not rules:
        return plain
    content = plain.decode("utf-8")
    for rule in rules:
        for op in rule["ops"]:
            content = OPS[op["op"]](content, **{k: v for k, v in op.items() if k != "op"})
    return content.encode("utf-8")


def mod_files(mod: dict, src_dir: Path) -> dict[str, bytes]:
    """Every file of the mod's bundle for one official folder: the official files transformed, plus any override-only files."""
    files = {p.name: apply_mod(mod, p.name, p.read_bytes()) for p in sorted(src_dir.glob("*.json"))}
    if mod["files_dir"].is_dir():
        for p in sorted(mod["files_dir"].glob("*.json")):
            files.setdefault(p.name, p.read_bytes())
    return files
