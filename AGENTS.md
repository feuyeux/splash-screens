# Repository Guidelines

## Project Structure & Module Organization

This repository versions JetBrains IDE splash-screen assets. The root
[`README.md`](README.md) is the source of truth for the supported IDEs, the
current release version, and each image's path inside its distribution JAR.
Store the eight tracked PNGs in `img/`, retaining their existing names such as
`clion_splash@2x.png` and `webstorm_webide_logo@2x.png`.

The refresh workflow lives in `scripts/jetbrains-splash-sync/`. Its
`sync.py` script parses the README, finds the specified JAR entries, and
updates changed images. `SKILL.md` documents the manual release procedure.

## Build, Test, and Development Commands

No build system or automated test suite is currently configured. Use these
checks when changing the utility or assets:

```bash
python3 -m py_compile scripts/jetbrains-splash-sync/sync.py
python3 scripts/jetbrains-splash-sync/sync.py 2026.3 --dry-run
git diff --check
```

The dry run validates README parsing and reports discoverable sources without
writing files or mounting DMGs. Run `git lfs pull` after cloning before
refreshing images; otherwise LFS pointer files produce false image changes.

## Coding Style & Naming Conventions

Use Python 3 with four-space indentation, type annotations for public helper
interfaces, `snake_case` functions and variables, and `UPPER_SNAKE_CASE`
module constants. Keep `sync.py` dependency-free: it intentionally uses only
the Python standard library. Prefer `pathlib.Path` over string path handling.

Do not rename existing image files unless the matching README image reference
and JAR path are updated together. Keep README entries ordered by their
numbered IDE sections and use forward-facing release versions such as `2026.3`.

## Testing Guidelines

For script edits, compile the module and run a targeted dry run, for example:

```bash
python3 scripts/jetbrains-splash-sync/sync.py 2026.3 --only CL,GO --dry-run
```

For asset refreshes, verify the report covers every requested IDE, inspect
`git diff --stat`, and confirm README changes are limited to the intended
version and JAR paths. Check images are real PNG data with
`file img/<name>.png`.

## Commit & Pull Request Guidelines

History favors short release-based subjects, for example `2026.2` or
`2026.2: 更新 splash 资源 jar 路径`. Keep commits narrowly scoped to one
release refresh or one utility/documentation change. In pull requests,
describe the JetBrains version, affected IDEs, and any changed JAR paths;
include screenshots or rendered image previews when visual assets changed.
Do not commit local `.DS_Store` files or unrelated working-tree changes.
