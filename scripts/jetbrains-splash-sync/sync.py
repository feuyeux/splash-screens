#!/usr/bin/env python3
"""jetbrains-splash-sync — sync splash screens from a JetBrains IDE install
or Toolbox download cache into the splash-screens repo.

Usage:
    python3 sync.py <version> [--only CL,GO,IU,PY,RD,RR,WS,PS]
                       [--source toolbox|installed|auto]
                       [--dry-run]

Exit codes:
    0 — run completed; per-IDE outcomes are in the report (not all need to be OK)
    1 — fatal error (could not read README, no source matched any IDE, etc.)
    2 — every targeted IDE warned/failed AND none OK
"""

from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
IMG_DIR = REPO_ROOT / "img"

# IDE code → human-readable name (matches README "## N [<Name>]").
IDE_CODES = ["CL", "GO", "IU", "PY", "RD", "RR", "WS", "PS"]
IDE_NAMES = {
    "CL": "CLion",
    "GO": "GoLand",
    "IU": "IDEA",
    "PY": "PyCharm",
    "RD": "Rider",
    "RR": "RustRover",
    "WS": "WebStorm",
    "PS": "PhpStorm",
}
# macOS install paths Toolbox generates. Default is ~/Applications because
# Toolbox on Apple Silicon commonly installs there. Override per-code via env
# like JSS_APP_DIR_CL=/Applications/CLion.app. Set to empty string to skip that
# IDE in installed-source lookups.
def _app(code: str, default: str) -> str:
    return os.environ.get(f"JSS_APP_DIR_{code}", default)


APP_DIRS = {
    "CL": _app("CL", str(Path.home() / "Applications/CLion.app")),
    "GO": _app("GO", str(Path.home() / "Applications/GoLand.app")),
    "IU": _app("IU", str(Path.home() / "Applications/IntelliJ IDEA.app")),
    "PY": _app("PY", str(Path.home() / "Applications/PyCharm.app")),
    "RD": _app("RD", str(Path.home() / "Applications/Rider.app")),
    "RR": _app("RR", str(Path.home() / "Applications/RustRover.app")),
    "WS": _app("WS", str(Path.home() / "Applications/WebStorm.app")),
    "PS": _app("PS", str(Path.home() / "Applications/PhpStorm.app")),
}
TOOLBOX_DOWNLOAD = Path(os.environ.get("JSS_TOOLBOX_DOWNLOAD", str(Path.home() / "Library/Caches/JetBrains/Toolbox/download")))


# ---------- result model ----------

@dataclass
class IDEResult:
    code: str
    name: str
    status: str  # "ok-updated" | "ok-unchanged" | "warn-no-source" | "warn-no-match" | "error"
    detail: str = ""
    img_path: str = ""
    sha_before: str = ""
    sha_after: str = ""


@dataclass
class Report:
    version: str
    results: list[IDEResult] = field(default_factory=list)

    def overall(self) -> str:
        if not self.results:
            return "EMPTY"
        if all(r.status == "ok-unchanged" for r in self.results):
            return "ALL_UNCHANGED"
        if any(r.status == "ok-updated" for r in self.results):
            return "UPDATED"
        return "FAILED"

    def render(self) -> str:
        out = [f"=== jetbrains-splash-sync v{self.version} ==="]
        for r in self.results:
            tag = {
                "ok-updated": "OK  updated  ",
                "ok-unchanged": "OK  unchanged",
                "warn-no-source": "WARN no-source ",
                "warn-no-match": "WARN no-match  ",
                "error": "ERR            ",
            }[r.status]
            line = f"  [{tag}] {r.code:<3} {r.name:<10}  {r.detail}"
            out.append(line)
        out.append(f"--- {self.overall()} ---")
        return "\n".join(out)


# ---------- README parsing ----------

IDE_SECTION_RE = re.compile(
    r"^##\s+\d+\s+[\w\s]*\[(?P<name>[^\]]+)\][^\n]*\n+"
    r"(?P<body>(?:(?!^##\s).*\n?)*)",
    re.MULTILINE,
)
JAR_PATH_RE = re.compile(
    r"Path\s*[(:（]\s*路径\s*[):）][^\"\n]*\"(?P<jar>[^\"\n]+)\"",
    re.MULTILINE,
)
IMG_REF_RE = re.compile(r"!\[.*?\]\(img/(?P<file>[^)]+)\)")
VERSION_HEADER_RE = re.compile(
    r"^#\s+JetBrains splash screens\[(?P<ver>[^\]]+)\]\s*$",
    re.MULTILINE,
)

# Sub-replace pattern for the version header — preserves the trailing newline
# (the regex below matches without eating it).
_VERSION_HEADER_BARE_RE = re.compile(
    r"^#\s+JetBrains splash screens\[[^\]]+\]",
    re.MULTILINE,
)


@dataclass
class IDESpec:
    code: str
    name: str
    jar_path: str           # relative inside the IDE install (e.g. "lib\intellij.clion.main.nolang.jar\artwork\clion_splash@2x.png")
    jar_file: str           # the jar filename (first path component)
    entry_in_jar: str       # path inside the jar (rest of the path, with separators normalized)
    img_filename: str       # local img/ filename (e.g. "clion_splash@2x.png")


def _normalize_jar_path(raw: str) -> tuple[str, str]:
    """README writes paths like "lib\\\\intellij.clion.main.nolang.jar\\\\artwork\\\\bar.png".
    A jar file is the first segment ending in ".jar"; everything before is a prefix dir,
    everything after is the entry path inside the jar (forward slashes)."""
    parts = raw.replace("\\", "/").split("/")
    # find first segment ending in ".jar"
    for i, p in enumerate(parts):
        if p.endswith(".jar"):
            jar_file = p
            entry = "/".join(parts[i + 1:]) if i + 1 < len(parts) else ""
            return jar_file, entry
    # fallback: treat first segment as the jar (older path style)
    return parts[0], "/".join(parts[1:]) if len(parts) > 1 else ""


def parse_readme() -> tuple[dict[str, IDESpec], str]:
    """Return (specs by IDE code, current version in header)."""
    text = README.read_text(encoding="utf-8")

    # detect IDE code from the human name in the section header
    name_to_code = {v: k for k, v in IDE_NAMES.items()}
    # IDEA section header reads "IntelliJ [IDEA]" — strip prefix; bare "IDEA" must also map
    code_aliases = {
        "IDEA": "IU",
        "IntelliJ IDEA": "IU",
        "PyCharm": "PY",
        "CLion": "CL",
        "GoLand": "GO",
        "Rider": "RD",
        "RustRover": "RR",
        "WebStorm": "WS",
        "PhpStorm": "PS",
    }

    specs: dict[str, IDESpec] = {}
    for m in IDE_SECTION_RE.finditer(text):
        name = m.group("name").strip()
        # name may be like "IntelliJ [IDEA]" or "JetBrains [IDEA]"; strip prefix
        bare = re.sub(r"^[A-Za-z]+\s*", "", name).strip()
        code = code_aliases.get(bare) or name_to_code.get(name)
        if not code:
            continue
        body = m.group("body")
        jar_m = JAR_PATH_RE.search(body)
        img_m = IMG_REF_RE.search(body)
        if not (jar_m and img_m):
            continue
        jar_raw = jar_m.group("jar").strip().rstrip(")").strip()
        jar_file, entry = _normalize_jar_path(jar_raw)
        specs[code] = IDESpec(
            code=code,
            name=IDE_NAMES[code],
            jar_path=jar_raw,
            jar_file=jar_file,
            entry_in_jar=entry,
            img_filename=img_m.group("file"),
        )

    ver_m = VERSION_HEADER_RE.search(text)
    current_version = ver_m.group("ver") if ver_m else "unknown"
    return specs, current_version


def update_readme_version(new_version: str) -> bool:
    """Rewrite the [# JetBrains splash screens[<ver>]] header line. Returns True if changed."""
    text = README.read_text(encoding="utf-8")
    new_text, n = _VERSION_HEADER_BARE_RE.subn(
        f"# JetBrains splash screens[{new_version}]", text, count=1
    )
    if n and new_text != text:
        README.write_text(new_text, encoding="utf-8")
        return True
    return False


def update_readme_paths(specs: dict[str, "IDESpec"]) -> list[str]:
    """For every spec marked _stale_path, rewrite its ## N section's Path(路径)
    line in README.md with the discovered jar + entry. Returns list of updated IDE codes."""
    text = README.read_text(encoding="utf-8")
    updated: list[str] = []
    for code, spec in specs.items():
        if not getattr(spec, "_stale_path", False):
            continue
        d_jar_name = Path(getattr(spec, "_discovered_jar", "")).name
        d_entry = getattr(spec, "_discovered_entry", "")
        new_path = f"lib\\{d_jar_name}\\{d_entry}".replace("/", "\\")
        # find the section that contains img/<spec.img_filename>
        marker = f"(img/{spec.img_filename})"
        idx = text.find(marker)
        if idx < 0:
            continue
        # walk back to the ## section header
        header_idx = text.rfind("##", 0, idx)
        next_section = text.find("\n## ", header_idx + 1)
        body_end = next_section if next_section > 0 else len(text)
        body = text[header_idx:body_end]
        # replace the first Path(路径) line in this section with literal text
        m = JAR_PATH_RE.search(body)
        if not m:
            continue
        new_body = body[: m.start()] + f'Path(路径)："{new_path}"' + body[m.end():]
        if new_body != body:
            text = text[:header_idx] + new_body + text[body_end:]
            updated.append(code)
    if updated:
        README.write_text(text, encoding="utf-8")
    return updated


# ---------- source discovery ----------

def find_installed_jar(spec: IDESpec) -> Path | None:
    """Look for <jar_file> directly under /Applications/<App>.app/Contents/lib/.
    This is the simplest source — what Toolbox writes when the IDE is fully installed."""
    app = APP_DIRS.get(spec.code)
    if not app:
        return None
    candidate = Path(app) / "Contents/lib" / spec.jar_file
    return candidate if candidate.is_file() else None


def _dmg_contains(dmg: Path, needle: str) -> bool:
    """Mount dmg read-only and check if needle appears inside the .app/lib."""
    mount = None
    try:
        out = subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-readonly", str(dmg)],
            check=True, capture_output=True, text=True,
        )
        # last line is "/Volumes/<name>"
        mount = out.stdout.strip().splitlines()[-1]
        root = Path(mount)
        # find .app inside
        apps = list(root.glob("*.app"))
        if not apps:
            return False
        return (apps[0] / "Contents/lib" / needle).is_file()
    except Exception:
        return False
    finally:
        if mount:
            subprocess.run(["hdiutil", "detach", mount], capture_output=True)


def find_toolbox_dmg(spec: IDESpec) -> Path | None:
    """Look for a dmg in the Toolbox download cache that contains the expected jar."""
    if not TOOLBOX_DOWNLOAD.is_dir():
        return None
    name_part = spec.name  # e.g. "GoLand", "RustRover"
    # Toolbox names dmgs like "<hash>-GoLand-2026.2-aarch64.dmg" or
    # "<hash>-<CODE>-<build>-...-mac.dmg". Match on the IDE human name.
    candidates = sorted(
        p for p in TOOLBOX_DOWNLOAD.iterdir()
        if p.suffix == ".dmg" and name_part.lower() in p.name.lower()
    )
    for dmg in candidates:
        try:
            if _dmg_contains(dmg, spec.jar_file):
                return dmg
        except Exception:
            continue
    return None


def extract_png_from_jar(jar: Path, entry: str) -> bytes | None:
    """Open jar as zip and read the named entry. Returns bytes or None."""
    if not jar.is_file() or not entry:
        return None
    try:
        with zipfile.ZipFile(jar) as zf:
            # entry separators inside zips are always '/'
            for name in zf.namelist():
                if name == entry or name.endswith("/" + entry):
                    return zf.read(name)
    except (zipfile.BadZipFile, KeyError, OSError):
        return None
    return None


def extract_png_from_dmg(dmg: Path, spec: IDESpec) -> bytes | None:
    """Mount dmg read-only, find <jar>, extract entry, return bytes."""
    mount = None
    try:
        out = subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-readonly", str(dmg)],
            check=True, capture_output=True, text=True,
        )
        mount = out.stdout.strip().splitlines()[-1]
        root = Path(mount)
        apps = list(root.glob("*.app"))
        if not apps:
            return None
        jar = apps[0] / "Contents/lib" / spec.jar_file
        return extract_png_from_jar(jar, spec.entry_in_jar)
    except Exception:
        return None
    finally:
        if mount:
            subprocess.run(["hdiutil", "detach", mount], capture_output=True)


# ---------- per-IDE resolution ----------

def _png_sanity_ok(data: bytes) -> bool:
    """Return True if data looks like a valid PNG. Catches corrupted extracts."""
    if len(data) < 8:
        return False
    return data[:8] == b"\x89PNG\r\n\x1a\n"


def _lib_jars(app_dir: str) -> list[Path]:
    """All *.jar under <app>/Contents/lib/, sorted by name."""
    p = Path(app_dir) / "Contents/lib"
    if not p.is_dir():
        return []
    return sorted(j for j in p.iterdir() if j.suffix == ".jar")


def _guess_filename_keywords(spec: IDESpec) -> list[str]:
    """Derive likely splash filename keywords from the target img filename and
    the IDE code. Returns a list of substrings; if any matches an entry path
    inside an IDE jar, marks that entry as a candidate.

    e.g. rustrover_splash@2x.png -> {'rustrover_splash', 'rustrover', 'splash'}
         rider_splash@2x.png -> {'rider_splash', 'rider', 'splash'}
         pycharm_logo@2x.png -> {'pycharm_logo', 'pycharm', 'logo'}
    Used as a fallback when the README's jar path no longer matches the install."""
    stem = spec.img_filename
    if stem.endswith("@2x.png"):
        stem = stem[:-len("@2x.png")]
    elif stem.endswith(".png"):
        stem = stem[:-len(".png")]
    code_stem = IDE_NAMES[spec.code].lower()  # e.g. "rustrover"
    kws = [stem]
    if code_stem != stem:
        kws.append(code_stem)
    # generic splash/logo fallback so we still hit entries like artwork/splash.png
    for generic in ("splash", "logo"):
        if generic not in kws:
            kws.append(generic)
    return kws


def _discover_png(
    spec: IDESpec, app_dir: str
) -> tuple[bytes | None, str]:
    """Fallback: when README's jar path is stale, scan every jar in the IDE's
    Contents/lib for an entry whose name contains the splash/logo keywords.
    Prefers @2x variants; ties broken by jar name (more specific first)."""
    keywords = _guess_filename_keywords(spec)
    code_stem = IDE_NAMES[spec.code].lower()
    # primary stem = target filename without @2x.png
    stem = spec.img_filename
    if stem.endswith("@2x.png"):
        stem = stem[:-len("@2x.png")]
    elif stem.endswith(".png"):
        stem = stem[:-len(".png")]
    candidates: list[tuple[int, str, Path, str, bytes]] = []
    for jar in _lib_jars(app_dir):
        jar_low = jar.name.lower()
        # require: jar name contains the IDE code (avoid platform/shared jars)
        if code_stem not in jar_low:
            continue
        try:
            with zipfile.ZipFile(jar) as zf:
                for name in zf.namelist():
                    if not name.endswith(".png"):
                        continue
                    low = name.lower()
                    # require: entry matches a descriptive keyword
                    if not any(kw.lower() in low for kw in keywords if kw):
                        continue
                    data = zf.read(name)
                    if not _png_sanity_ok(data):
                        continue
                    base = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]  # "pycharm_logo@2x"
                    if base.endswith("@2x"):
                        base = base[:-3]  # "pycharm_logo"
                    # score: exact basename match wins (-1), release path (0),
                    # any @2x (1), other (2); penalize eap (3) and non-release paths
                    path_low = name.lower()
                    if base == stem:
                        score = -1
                    elif "/release/" in path_low:
                        score = 0
                    elif "/eap/" in path_low:
                        score = 3
                    elif "@2x" in name:
                        score = 1
                    else:
                        score = 2
                    candidates.append((score, jar.name, jar, name, data))
        except (zipfile.BadZipFile, OSError):
            continue
    if not candidates:
        return None, ""
    # sort: score (best=lowest), then @2x preferred (lowest), then jar name
    candidates.sort(key=lambda c: (c[0], 0 if "@2x" in c[3] else 1, c[1]))
    _, _, jar, name, data = candidates[0]
    return data, f"discovered:{jar}::{name}"


def resolve_png(
    spec: IDESpec, source: str, dry_run: bool, allow_discover: bool = True
) -> tuple[bytes | None, str]:
    """Returns (png_bytes, source_label). source_label is for the report."""
    if source in ("installed", "auto"):
        app_dir = APP_DIRS.get(spec.code, "")
        jar = find_installed_jar(spec) if app_dir else None
        if jar:
            png = extract_png_from_jar(jar, spec.entry_in_jar)
            if png and _png_sanity_ok(png):
                return png, f"installed:{jar}"
        # Fallback: README's jar/entry may have moved. Discover by filename.
        if allow_discover and app_dir:
            png, label = _discover_png(spec, app_dir)
            if png:
                return png, label
    if source in ("toolbox", "auto"):
        # cache directory can hold either dmgs or pre-downloaded jars
        # 1) try direct jar (Toolbox sometimes caches the IDE with jars in place)
        if TOOLBOX_DOWNLOAD.is_dir():
            for cand in TOOLBOX_DOWNLOAD.iterdir():
                if cand.suffix == ".jar" and cand.name.endswith(spec.jar_file):
                    png = extract_png_from_jar(cand, spec.entry_in_jar)
                    if png and _png_sanity_ok(png):
                        return png, f"toolbox-cache:{cand}"
        # 2) try dmg (skip mount in dry-run — hdiutil attach is a side-effect)
        if not dry_run:
            dmg = find_toolbox_dmg(spec)
            if dmg:
                png = extract_png_from_dmg(dmg, spec)
                if png and _png_sanity_ok(png):
                    return png, f"toolbox-dmg:{dmg}"
        else:
            # dry-run: report whether a candidate dmg exists by name match only
            if TOOLBOX_DOWNLOAD.is_dir():
                name_part = spec.name
                for cand in TOOLBOX_DOWNLOAD.iterdir():
                    if cand.suffix == ".dmg" and name_part.lower() in cand.name.lower():
                        return None, f"dry-skip-dmg:{cand.name}"  # signals "candidate exists but not read"
    return None, ""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------- main ----------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("version", help="New version label, e.g. 2026.2")
    p.add_argument("--only", help="Comma-separated IDE codes to process (default: all 8)",
                   default="")
    p.add_argument("--source", choices=["toolbox", "installed", "auto"], default="auto",
                   help="Where to look for the IDE jars (default: auto)")
    p.add_argument("--strict", action="store_true",
                   help="Disable the README-path discovery fallback. Only use jar/entry "
                        "exactly as written in README.md (catches jar moves as warn-no-source).")
    p.add_argument("--update-readme-paths", action="store_true",
                   help="After running, rewrite each stale '## N' section's Path(路径) "
                        "line with the discovered jar/entry. Use after the first sync "
                        "that reports STALE rows, then re-run.")
    p.add_argument("--dry-run", action="store_true",
                   help="Skip dmg mount + filesystem writes; print report only")
    args = p.parse_args()

    if not README.is_file():
        print(f"FATAL: README.md not found at {README}", file=sys.stderr)
        return 1

    specs, current_version = parse_readme()
    if not specs:
        print("FATAL: README parsed but no IDE sections found", file=sys.stderr)
        return 1

    only_codes = set(c.strip().upper() for c in args.only.split(",") if c.strip())
    targets = [specs[c] for c in IDE_CODES if c in specs and (not only_codes or c in only_codes)]
    skipped = [c for c in IDE_CODES if only_codes and c not in specs]

    report = Report(version=args.version)

    print(f"version:    {args.version} (current README: {current_version})")
    print(f"source:     {args.source}{' (dry-run)' if args.dry_run else ''}")
    print(f"targets:    {', '.join(s.code for s in targets) or '<none>'}")
    if skipped:
        print(f"unknown:    {', '.join(skipped)}")
    print()

    for spec in targets:
        target_img = IMG_DIR / spec.img_filename
        sha_before = sha256(target_img.read_bytes()) if target_img.is_file() else "(missing)"

        png, label = resolve_png(spec, args.source, args.dry_run, allow_discover=not args.strict)
        if png is None:
            report.results.append(IDEResult(
                code=spec.code, name=spec.name,
                status="warn-no-source",
                detail=f"jar={spec.jar_file} entry={spec.entry_in_jar}",
            ))
            continue

        # If we used the discovery fallback, the discovered jar/entry may differ
        # from what's written in README.md. Note it so the user can update README.
        # label format: "discovered:<jar_abs_path>::<entry_in_jar>"
        if label.startswith("discovered:"):
            body = label[len("discovered:"):]
            d_jar, _, d_entry = body.rpartition("::")
            if Path(d_jar).name != spec.jar_file or d_entry != spec.entry_in_jar:
                spec._stale_path = True  # type: ignore[attr-defined]
                spec._discovered_jar = d_jar  # type: ignore[attr-defined]
                spec._discovered_entry = d_entry  # type: ignore[attr-defined]

        sha_after = sha256(png)
        if sha_after == sha_before:
            report.results.append(IDEResult(
                code=spec.code, name=spec.name,
                status="ok-unchanged",
                detail=f"src={label}",
                img_path=str(target_img),
                sha_before=sha_before, sha_after=sha_after,
            ))
            continue

        if args.dry_run:
            report.results.append(IDEResult(
                code=spec.code, name=spec.name,
                status="ok-updated",
                detail=f"DRY src={label}",
                img_path=str(target_img),
                sha_before=sha_before, sha_after=sha_after,
            ))
            continue

        IMG_DIR.mkdir(parents=True, exist_ok=True)
        target_img.write_bytes(png)
        report.results.append(IDEResult(
            code=spec.code, name=spec.name,
            status="ok-updated",
            detail=f"src={label}",
            img_path=str(target_img),
            sha_before=sha_before, sha_after=sha_after,
        ))

    # README version header
    readme_changed = False
    if not args.dry_run:
        readme_changed = update_readme_version(args.version)

    # git add (not commit)
    git_hint = []
    if not args.dry_run:
        staged = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "add", "img/", "README.md"],
            capture_output=True, text=True,
        )
        if staged.returncode != 0:
            print(f"WARN: git add failed: {staged.stderr}", file=sys.stderr)
        else:
            git_hint.append("git add img/ README.md  (done)")
            if readme_changed:
                git_hint.append(f"README header: -> [{args.version}]  (changed)")
            else:
                git_hint.append(f"README header: [{args.version}]  (unchanged)")
            git_hint.append("git commit -m '<your message>'  (manual)")

    print(report.render())
    if git_hint:
        print()
        for line in git_hint:
            print(f"  {line}")

    # Stale README paths — discovered jar/entry differs from what's documented
    stale_msgs: list[str] = []
    for code in IDE_CODES:
        spec = specs.get(code)
        if not spec:
            continue
        if getattr(spec, "_stale_path", False):
            d_jar = getattr(spec, "_discovered_jar", "")
            d_entry = getattr(spec, "_discovered_entry", "")
            stale_msgs.append(
                f"  STALE  {code}  README: lib\\\\{spec.jar_file}\\\\{spec.entry_in_jar}"
                f"   ACTUAL: lib\\\\{Path(d_jar).name}\\\\{d_entry}"
            )
    if stale_msgs:
        print()
        print("README jar paths no longer match the installed IDE. Update with:")
        for m in stale_msgs:
            print(m)
        print("  rerun with --update-readme-paths to apply (then re-run sync).")

    if args.update_readme_paths and not args.dry_run and stale_msgs:
        updated = update_readme_paths(specs)
        if updated:
            print()
            print(f"README updated for: {', '.join(updated)}")
            # re-stage README so the next git commit picks it up
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "add", "README.md"],
                capture_output=True, text=True,
            )

    # exit code
    statuses = [r.status for r in report.results]
    if report.results and all(s.startswith("warn") or s == "error" for s in statuses):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())