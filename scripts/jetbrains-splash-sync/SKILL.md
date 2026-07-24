---
name: jetbrains-splash-sync
description: "Use when the user says '更新 splash 资源', 'splash 换新版本', 'sync splash screens', 'JetBrains 出新版本了抽 splash 图', or wants to refresh splash/*.png from a new JetBrains IDE release into this repo's img/. Loads the manual-trigger sync workflow for the splash-screens repo at /Users/han/coding/splash-screens. Parses README.md to derive each IDE's jar path and target filename, extracts PNGs from ~/Applications/<X>.app (Toolbox default on Apple Silicon) or ~/Library/Caches/JetBrains/Toolbox/download/ (incl. dmg mount). When README's jar path is stale (JetBrains moved the splash/logo asset between jars — happens often), auto-discovers the new location by scanning every jar in Contents/lib and SHA256-diffs against existing img/. Rewrites the [VER] header in README.md, optionally rewrites the stale Path(路径) lines with --update-readme-paths, then `git add img/ README.md` without committing."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [jetbrains, splash-screens, sync, toolbox, dmg, jar, ide, version]
    related_skills: [git-commit-hygiene, plan]
---

# JetBrains Splash Sync (splash-screens repo)

Pull splash screens from a freshly installed JetBrains IDE release into this repo's `img/` directory. The repo is a versioned collection of JetBrains IDE startup splash images — eight IDEs (CLion, GoLand, IDEA, PyCharm, Rider, RustRover, WebStorm, PhpStorm). Every JetBrains release may change a splash; this skill automates the swap.

## When to Use

- User says "更新 splash 资源 jar 路径", "splash 出新版本了", "jetbrains 抽 splash 图", "sync splash screens to 2026.2"
- User mentions a new JetBrains version number and wants the corresponding splash images in this repo
- README.md `## N` sections need their `Path(路径)："lib\\...jar\\artwork\\..."` updated because JetBrains moved assets between jars

## When NOT to Use

- User wants a one-off image extraction for something other than this repo → use plain `unzip -p <jar> <entry>` instead
- User wants to scrape splash screens from a non-JetBrains product → out of scope
- User wants to commit the resulting changes → this skill stops at `git add`; the user reviews and writes the commit message (matches the existing `2026.2: 更新 splash 资源 jar 路径` style)

## Core Workflow

The script lives at `scripts/jetbrains-splash-sync/sync.py`. It is self-contained Python 3 stdlib only.

```
python3 scripts/jetbrains-splash-sync/sync.py <version>
                                              [--only CL,GO,IU,PY,RD,RR,WS,PS]
                                              [--source toolbox|installed|auto]
                                              [--dry-run]
```

1. **Parse README.md.** Each `## N [<Name>]` section has a markdown image and a `Path(路径)："lib\<jar>\artwork\<file>"` line. The script derives:
   - IDE code from the section name (alias table maps `IntelliJ IDEA` → `IU`, etc.)
   - jar filename (first `.jar` segment) + entry path inside the jar
   - target filename in `img/`
   - current version from the top-level `# JetBrains splash screens[<ver>]` header
2. **Resolve PNG bytes per IDE.** Three sources, tried in `--source` order:
   - `installed` — `/Applications/<App>.app/Contents/lib/<jar>` (path overridable per code via `JSS_APP_DIR_CL` etc.)
   - `toolbox-cache` — `<jar>` file directly in `~/Library/Caches/JetBrains/Toolbox/download/`
   - `toolbox-dmg` — mount a `<name>-<ver>-<arch>.dmg` from the same Toolbox cache, read the jar inside, unmount
   Default `--source auto` tries `installed` first, then `toolbox-cache`, then `toolbox-dmg`.
3. **Sanity-check** the extracted bytes start with the PNG magic (`\x89PNG\r\n\x1a\n`). Junk is treated as "no source."
4. **SHA256-diff** against the existing `img/<file>`. Different → overwrite; same → leave alone.
5. **Rewrite README header** `# JetBrains splash screens[<old>]` → `[<new>]` only when the version actually changes.
6. **`git add img/ README.md`** (no commit; the user picks the message).
7. **Print report** with one row per targeted IDE: `OK updated` / `OK unchanged` / `WARN no-source` / `WARN no-match` / `ERR …`. Exit code 0 if at least one targeted IDE ended OK; 2 if all warned/failed.

## Outputs / What the User Sees

```
version:    2026.2 (current README: 2026.2)
source:     auto
targets:    CL, GO, IU, PY, RD, RR, WS, PS

=== jetbrains-splash-sync v2026.2 ===
  [OK  updated  ] CL  CLion       src=installed:/Applications/CLion.app/Contents/lib/intellij.clion.main.nolang.jar
  [OK  unchanged] GO  GoLand      src=toolbox-cache:/Users/han/Library/Caches/JetBrains/Toolbox/download/...jar
  [WARN no-source ] IU  IDEA        jar=intellij.idea.ultimate.customization.jar entry=idea_logo@2x.png
  ...
--- UPDATED ---

  git add img/ README.md  (done)
  README header: -> [2026.2]  (changed)
  git commit -m '<your message>'  (manual)
```

## Run Recipe — Full New Release

```bash
cd /Users/han/coding/splash-screens

# 1. dry-run first to see what would happen (no hdiutil mount, no writes)
python3 scripts/jetbrains-splash-sync/sync.py 2026.2 --dry-run

# 2. if any STALE rows appear, JetBrains moved a splash to a different jar.
#    Apply the new jar paths to README.md so future runs are clean.
python3 scripts/jetbrains-splash-sync/sync.py 2026.2 --update-readme-paths

# 3. run for real
python3 scripts/jetbrains-splash-sync/sync.py 2026.2

# 4. inspect the diff
git status
git diff --stat

# 5. commit in the existing style — short prefix + Chinese description
git commit -m "2026.2"
```

## Edge Cases Worth Knowing

- **Missing IDEs on this machine.** `installed` source returns None when `/Applications/<App>.app` doesn't exist. Fall through to `toolbox-cache`, then `toolbox-dmg`. If all three miss → `WARN no-source`, the script moves on (does not abort the run).
- **Toolbox cache only holds patch jars.** Files like `*-GO-262.x-...-patch-aarch64-mac.jar` are *differential* update jars — they don't contain `artwork/splash@2x.png`. The script filters by exact jar filename match; patches are skipped.
- **Toolbox just downloaded a fresh dmg but no full IDE yet.** `--source toolbox` (or `auto`) will mount the dmg in read-only mode (`hdiutil attach -nobrowse -readonly`), extract the PNG, unmount. Slow first time per dmg because of mount overhead.
- **`git-lfs` tracked PNGs.** The repo's `img/*.png` are LFS pointers. After cloning, `git lfs pull` is needed before running — otherwise `SHA256(img/<file>)` is the LFS pointer text and will *always* differ from the real PNG, causing spurious updates. If `git status` shows LFS placeholders after a sync, restore them with `git lfs checkout img/`.
- **IDEA section header quirk.** README writes `## 3 IntelliJ [IDEA](https://...)`. The parser strips the `IntelliJ ` prefix and matches the alias `IDEA → IU`.
- **Version header unchanged.** `update_readme_version` no-ops when `<new>` equals the current header, so re-running with the same arg is safe.

## Environment Overrides (testing + custom installs)

| Env var | Purpose |
| --- | --- |
| `JSS_APP_DIR_<CODE>` | Override `/Applications/<App>.app` for code `<CODE>` (CL/GO/IU/PY/RD/RR/WS/PS). Useful when an IDE is installed outside `/Applications`. |
| `JSS_TOOLBOX_DOWNLOAD` | Override `~/Library/Caches/JetBrains/Toolbox/download`. Use this to point at a different Toolbox cache, e.g. for testing. |

Example: dry-run with a fake install path on the test bench.

```bash
JSS_APP_DIR_CL=/tmp/fake/CLion.app \
JSS_APP_DIR_GO= JSS_APP_DIR_IU= JSS_APP_DIR_PY= JSS_APP_DIR_RD= \
JSS_APP_DIR_RR= JSS_APP_DIR_WS= JSS_APP_DIR_PS= \
JSS_TOOLBOX_DOWNLOAD=/tmp/no-toolbox \
python3 scripts/jetbrains-splash-sync/sync.py 2026.2 --source installed --dry-run
```

## Common Pitfalls

1. **Forgetting `git lfs pull` after cloning.** Synchronously the script will *always* report "updated" because LFS pointer bytes differ from real PNG bytes. Verify with `file img/<name>.png` — should say `PNG image data`, not `ASCII text`.
2. **Reading `/Users/han/Library/Application Support/JetBrains/Toolbox/state.json`.** It's Toolbox's internal state file. Do not cat / parse it without permission — it can leak credentials. The skill reads only `~/Library/Caches/JetBrains/Toolbox/download/`, which is filesystem-visible artifact data, not config.
3. **Running without `--dry-run` first.** dmg mount is a real side-effect (`hdiutil attach` shows up in Finder). Always dry-run when testing a new version.
4. **Trusting a `WARN no-source` as "all good."** If JetBrains truly shipped a new splash but the script can't find it, the user needs to know. Surface the WARN lines in your reply; don't silently move on.
5. **Assuming `/Applications/<App>.app`.** On this Mac, Toolbox installed the IDEs under `~/Applications/`, not `/Applications/`. The script defaults there; if you ever move an IDE, set `JSS_APP_DIR_<CODE>` to override.
6. **Ignoring `STALE` rows after a sync.** They mean JetBrains moved the splash asset to a different jar between releases (saw this with PyCharm `product-backend.jar → intellij.pycharm.pro.jar`, Rider `app-backend.jar → intellij.rider.branding.jar`, RustRover `product-backend.jar → intellij.rustrover.jar` in 2026.x). Run again with `--update-readme-paths` to fix README; then re-sync — the next run should report `OK unchanged` instead of going through the discovery fallback.
7. **`subn` regex eating the trailing newline.** The first version of this script used `\s*$` in the version-header regex; `\s` includes `\n`, which silently dropped the blank line after the header. Fixed by using a bare match (no `\s*`) and replacing only the `# JetBrains splash screens[<ver>]` portion. Don't put `\s*$` in any single-line replacement pattern in this codebase without verifying the diff is byte-identical when the version is unchanged.

## Verification Checklist

- [ ] `python3 scripts/jetbrains-splash-sync/sync.py <ver> --dry-run` exits 0 and shows 8 targeted rows
- [ ] Each row's `jar` + `entry` matches the corresponding README.md section
- [ ] No `STALE` rows on a normal sync; if there are, run `--update-readme-paths` once, then re-sync to confirm they go away
- [ ] Real run: report shows `OK updated` / `OK unchanged` / `WARN no-source` per IDE
- [ ] `git status` shows only `img/` + `README.md` modified (plus the new `scripts/` dir if first time)
- [ ] `git diff README.md` shows only the `[<old>]` → `[<new>]` change (and any stale-Path updates if `--update-readme-paths` was used)
- [ ] `git diff --stat img/` matches the IDEs reported as updated
- [ ] `file img/<name>.png` says `PNG image data`, not LFS pointer text
- [ ] User reviews the diff, picks a commit message, commits manually

## One-Shot Recipes

### New release, all 8 IDEs, full sync

```bash
python3 scripts/jetbrains-splash-sync/sync.py 2026.2
```

### Only Rider + RustRover (other 6 left alone)

```bash
python3 scripts/jetbrains-splash-sync/sync.py 2026.2 --only RD,RR
```

### Force using the Toolbox dmg cache even if /Applications is present

```bash
python3 scripts/jetbrains-splash-sync/sync.py 2026.2 --source toolbox
```

### IDE installed at a non-standard path

```bash
JSS_APP_DIR_PS=/Volumes/External/Apps/PhpStorm.app \
python3 scripts/jetbrains-splash-sync/sync.py 2026.2
```