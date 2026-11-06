"""Regression tests for repository hygiene.

The repository carries its own lint configuration, one version location and a
release gate named `bless`.  These tests fail if any of those artefacts quietly
disappears again.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEV_DOC = ROOT / 'tests/README-DEV.md'
VERSION_VALUE = re.compile(r"""^__version__ = ['"]([^'"]+)['"]""", re.MULTILINE)


def pyproject():
    return tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))


def test_virtualenvs_caches_and_artifacts_are_ignored():
    lines = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
    for entry in ('.venv/', '.ruff_cache/', '.pytest_cache/', 'build/',
                  'dist/', '.coverage', '*.whl', '*.tar.gz'):
        assert entry in lines, entry


def test_ruff_configuration_lives_in_the_repository():
    ruff = pyproject()['tool']['ruff']
    assert ruff['target-version'] == 'py311'
    assert set(ruff['lint']['select']) >= {'E4', 'E7', 'E9', 'F', 'I', 'UP', 'B'}


def test_the_version_lives_in_exactly_one_place():
    project = pyproject()['project']
    assert project['dynamic'] == ['version']
    assert 'version' not in project

    package = (ROOT / 'efibootdude' / '__init__.py').read_text(encoding='utf-8')
    match = VERSION_VALUE.search(package)
    assert match, 'efibootdude/__init__.py must declare __version__'

    import efibootdude

    assert efibootdude.__version__ == match.group(1)


def test_the_dev_extra_installs_the_release_toolchain():
    dev = pyproject()['project']['optional-dependencies']['dev']
    # Spacing around the specifier is cosmetic; the floors are not.
    joined = '\n'.join(dev).replace(' ', '')
    assert 'pytest-cov' in joined
    # `bless` refuses to start without these floors, so the extra must meet them.
    assert 'build>=1.2' in joined
    assert 'twine>=7' in joined
    assert 'packaging>=26' in joined


def test_the_branch_model_is_declared_and_documented():
    # `publish` pushes exactly the branches named here, so the setting has to
    # exist and the developer note has to say what it chooses.
    branches = pyproject()['tool']['bless']['branches']
    assert 1 <= len(branches) <= 2, branches
    assert 'branches' in DEV_DOC.read_text(encoding='utf-8')


def test_the_release_gate_exists_and_the_dev_document_names_it():
    gate = ROOT / 'bless'
    assert gate.is_file(), 'the release gate is missing'
    assert gate.stat().st_mode & 0o111, './bless must be executable'

    # README.md stays about using the tool; the gate is a maintainer concern and
    # is documented only in the developer note under tests/.
    text = DEV_DOC.read_text(encoding='utf-8')
    assert 'bless' in text, DEV_DOC
    assert './release' not in text, DEV_DOC


def test_no_documented_command_uploads_the_whole_dist_directory():
    # Split so this test's own source does not trip the scan it performs.
    needle = 'twine upload ' + 'dist/*'
    skipped = {'.git', 'build', 'dist', '.venv', '.ruff_cache', '.pytest_cache'}
    text_suffixes = {'', '.md', '.toml', '.yml', '.yaml', '.py', '.cfg', '.txt'}
    offenders = []
    for path in ROOT.rglob('*'):
        relative = path.relative_to(ROOT)
        if not path.is_file() or skipped & set(relative.parts):
            continue
        if path.suffix not in text_suffixes:
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        if needle in text:
            offenders.append(str(relative))
    assert not offenders, offenders


def test_the_documents_describe_the_testpypi_rehearsal():
    """Answering `test` at the prompt is documented, not tribal knowledge."""
    readme = DEV_DOC.read_text(encoding='utf-8')
    assert '====> Enter test or publish or quit:' in readme
    assert 'test.pypi.org' in readme
    assert 'TWINE_TESTPYPI_PASSWORD' in readme
    assert '--extra-index-url' in readme


def test_the_coverage_gate_is_wired_and_cannot_quietly_disappear():
    """The suite reports coverage and the release gate enforces a floor."""
    report = pyproject()['tool']['coverage']['report']
    assert report['fail_under'] >= 30
    assert '--cov=efibootdude' in DEV_DOC.read_text(encoding='utf-8')
    assert '.coverage' in (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()


def test_bless_and_the_documents_share_the_distribution_inspector():
    inspector = ROOT / 'tests' / 'inspect_dist.py'
    assert inspector.is_file(), 'the distribution inspector is missing'
    assert 'DEFAULT_INSPECTOR' in (ROOT / 'bless').read_text(encoding='utf-8')
    assert 'tests/inspect_dist.py' in DEV_DOC.read_text(encoding='utf-8')
