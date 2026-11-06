# efibootdude: working on the source tree

This is the developer companion to [`README.md`](../README.md), which documents
the tool for its users. Here we document the two things a maintainer needs and a
user does not: the **test gate** and the **release gate** (`./bless`).

This file lives in `tests/`, so its links point one directory up. The commands
assume the repository root, which is where the gate has to be run.

## Layout and single sources of truth

| Path | Owns |
| --- | --- |
| [`efibootdude/__init__.py`](../efibootdude/__init__.py) | `__version__`, and nothing else. The build backend reads it, `efibootdude --version` prints it, `bless` reads the line without importing the package. |
| [`pyproject.toml`](../pyproject.toml) | Name, description, keywords, `requires-python`, runtime dependencies, the SPDX `license` and `license-files`, the `test`/`dev` extras, and the pytest, coverage and ruff configuration. |
| [`bless`](../bless) | The release gate. Copied verbatim from `../../dwipe/bless`; it holds no copy of this project's identity. |
| [`tests/inspect_dist.py`](inspect_dist.py) | The distribution inspector. `bless` refuses to upload a wheel and sdist it has not inspected with this file. |
| [`runner`](../runner) | Runs the tree from source (`python -m efibootdude.main`) for debugging. |
| [`.gitignore`](../.gitignore) | Keeps gate artifacts (`build/`, `dist/`, `.coverage*`, caches, `*.whl`, `*.tar.gz`) out of `git status`, because the gate refuses a dirty tree. |

The version lives in exactly one place. `pyproject.toml` declares
`dynamic = ["version"]`, so flit reads `efibootdude/__init__.py`; a
`X.Y.Z.99.N` rehearsal rewrites that line only inside a throwaway copy.

Almost nothing needs a `[tool.bless]` table: `NAME = efibootdude`,
`MODULE = efibootdude`, the console script and the inspector are all derived
from what is already there. The one setting written out is `branches`, which
names the branches `publish` releases from -- see
[the branch model](#the-branch-model-is-a-setting-not-a-convention) below.

## The test gate

Install what the gate needs (this is the interpreter that must run `bless`,
too):

    python3 -m venv .venv
    .venv/bin/python -m pip install -e '.[dev]'

`bless` runs every stage with `sys.executable`, so it has to be *that*
interpreter -- the one holding `console-window` and the dev extra. Activate the
virtualenv first, or start the gate by hand:

    .venv/bin/python ./bless

Then the commands `bless` runs, in the same order, on the same interpreter:

    python -m ruff check .
    python -m compileall -q efibootdude conftest.py tests
    python -m pytest -q --cov=efibootdude --cov-report=term-missing

The coverage floor (30%) is enforced through
`[tool.coverage.report] fail_under`. It was set just below the measured number
when the gate was adopted; raise it when the number rises, never lower it.

The ruff selection is committed rather than inherited
(`[tool.ruff.lint] select = ["E4", "E7", "E9", "F", "I", "UP", "B"]`,
`target-version = "py311"`) so that no user or global configuration can change
what the gate enforces.

`efibootdude --version` is answered before the curses screen is touched
([`main()`](../efibootdude/main.py:1133)), because the smoke stage of the release
gate runs it as an ordinary user on a machine with no saved state.

## The release gate: `./bless`

`./bless` takes no arguments and has no flag that skips a stage. It always runs
everything, then prints what it has in hand and asks one question:

    ====> Enter test or publish or quit:

Only `test` and `publish` are accepted, spelled in full; anything else is asked
again. Enter, `quit` or Ctrl-C stops with nothing tagged, uploaded or pushed.

**Stages, in order**

1. **preflight** -- clean tree, the `vX.Y.Z` tag absent, `twine>=7` and
   `packaging>=26` present.
2. **index check** -- refuses if `__version__` is already on PyPI, printing
   every published version, before anything is built or uploaded.
3. **gates** -- the three commands above.
4. **build** -- into `build/bless-<version>/`, wiped first, so no older
   artifact can be sent by mistake.
5. **inspect** -- `tests/inspect_dist.py` against the wheel and the sdist.
6. **check** -- `twine check --strict` on both files.
7. **smoke** -- installs the built wheel into a throwaway venv and runs
   `efibootdude --version`, which must print the version being released.
8. **branch plan** -- refuses a detached HEAD, a branch that is not one of the
   release branches, a release branch missing locally or on origin, two
   branches that have diverged, or a push that would not fast-forward.

**`test`** is the rehearsal and never spends the working version. It copies the
tree to `build/bless-test-<version>/src`, rewrites `__version__` to
`X.Y.Z.99.N` (99 keeps it clear of real releases; `N` is one past the highest
suffix TestPyPI already holds for that base), builds, inspects and smoke-tests
the copy, and uploads only to `test.pypi.org`. No tag, no branch, no push. It
prints the install line, which needs `python -m pip cache purge` first and
`--extra-index-url https://pypi.org/simple/`, because TestPyPI carries no
dependencies.

**`publish`** fast-forwards the release branches to one commit, tags `vX.Y.Z`,
uploads the two inspected files to PyPI, verifies the index, and pushes those
branches and the tag.

**Credentials** are resolved per index -- `TWINE_PASSWORD` for PyPI,
`TWINE_TESTPYPI_PASSWORD` for TestPyPI, each overruling the `~/.pypirc` section
whose `repository` matches that index's upload URL -- so a PyPI token is never
spent on a rehearsal. A missing credential is a refusal before `twine` starts.

**Verification** reads the index back after an upload. The read is cache-busted
and patient (up to a quarter of an hour), because both indexes list new releases
through a CDN that lags; a release that is not listed yet is reported as lag
with the project page to check, never as a failed upload, since an accepted
upload cannot be withdrawn.

There is no CI publish job, on purpose: the upload is irreversible and is run by
the owner from the development tree, so the safety lives in one inspectable
command. CI stays a gate.

## The branch model is a setting, not a convention

`bless` publishes only from a branch it was told about, and pushes every branch
it was told about. Which ones those are is `[tool.bless] branches` in
[`pyproject.toml`](../pyproject.toml), and there are two shapes:

* `branches = ["main"]` -- one release branch. Nothing is aligned, and `publish`
  pushes `main` and the tag. This is what this project uses, so a small change
  can be published straight from the trunk.
* `branches = ["main", "dev"]` -- the default when the setting is absent. While
  you are on `main`, `dev` does not have to exist yet: the trunk alone satisfies
  the model, so `publish` releases from `main` and pushes only what exists. Once
  `dev` has been created it also has to exist as `origin/dev`, and whichever of
  the two lags is fast-forwarded onto the other before the tag, so a release
  always leaves them at one commit.

Either way the gate refuses a branch it was not told about, a branch it has to
push that is missing on origin, two branches that have diverged, and a push that
would not fast-forward. What a single branch cannot do is notice that a
development branch was left behind: merging `dev` into `main` stays your job,
and the pair exists to make it the gate's job instead.

Switching to the pair is one line in `pyproject.toml` plus the branch itself:

    git branch dev
    git push -u origin dev

## What the inspector asserts

[`tests/inspect_dist.py`](inspect_dist.py) is the one implementation, used by
`bless` and, once CI is wired up, by CI; expectations are read from
`pyproject.toml` and the package, never from the artifact under test.

* **wheel** -- `Name`, `Version`, `Summary`, `Requires-Python`, `Keywords`,
  `License-Expression` and the *runtime* `Requires-Dist` names match
  `pyproject.toml`; every declared `license-files` entry is listed in
  `License-File`; every `efibootdude/*.py` is present; a `LICENSE` is carried;
  no `tests/`, no `.venv`, no `__pycache__`, no `.pyc`.
* **sdist** -- carries `pyproject.toml`, `README.md`, `LICENSE`,
  `efibootdude/__init__.py`, `efibootdude/main.py`; the `__version__` inside the
  sdist equals the artifact version; no venv or bytecode. The sdist is
  deliberately lean: flit's default payload is enough and `tests/` need not
  ride along.

Run it by hand on any built pair:

    python tests/inspect_dist.py dist/efibootdude-2.1.1-py3-none-any.whl \
                                dist/efibootdude-2.1.1.tar.gz

## Releasing, in three steps

1. Bump `__version__` in [`efibootdude/__init__.py`](../efibootdude/__init__.py)
   and commit.
2. Run `./bless`. The gate runs in full.
3. Answer `test` to rehearse on TestPyPI, then run `./bless` again and answer
   `publish` -- or go straight to `publish`.

`[tool.bless] branches` names what `publish` pushes, and each of those branches
must exist and track origin; `publish` leaves them all at the tagged commit.
With `["main"]` that is one branch, with `["main", "dev"]` it is the pair.
Everything else about the release is one of the stages above.

## Notes on this adoption

* [`bless`](../bless) is a byte-for-byte copy of `../../dwipe/bless` (SHA-256
  `2acf524461de07f0b6cc9dff986388b92e3c6bc6bfaded6520b040f55515b3f6`) and is not
  edited here: reuse is "copy verbatim", and any later upstream change is
  adopted as a new copy. The one-branch/two-branch setting was added to
  `dwipe/bless` first for exactly that reason, so both projects keep one file.
* `efibootdude` needed five things: one version location
  (`dynamic = ["version"]`), an inspector, the `dev` extra with `build`,
  `twine>=7` and `packaging>=26`, a console script that answers `--version`, and
  a branch model for the branch plan -- here `branches = ["main"]`. The metadata
  moved to the PEP 639 `license` expression and `license-files` at the same
  time, which is why the build backend floor is `flit_core>=3.12`.
* The ruff rule set was added with the gate, so the whole tree was brought to a
  clean `ruff check .` once. [`tests/test_bless.py`](test_bless.py) and
  [`tests/test_repo_hygiene.py`](test_repo_hygiene.py) keep the gate, the
  inspector and the single version location from drifting.
* CI is not wired up yet. When it is, it must run the same three gate commands
  and the same inspector, never an equivalent.
