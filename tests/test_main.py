"""Tests for the pure (non-curses) logic in ``efibootdude.main``.

Covered:
- ``SystemInfo`` mount parsing and UUID extraction
- ``EfiBootDude.digest_boots`` parsing of ``efibootmgr`` output
- ``EfiBootDude.format_boot_entry`` display formatting

``EfiBootDude`` instances are built with ``__new__`` so that curses /
``ConsoleWindow`` is never initialised.
"""
import os

import pytest

from efibootdude.main import BootEntry, BootModifications, EfiBootDude, SystemInfo

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, 'efibootmgr.peggy.txt')

# Partition UUID used by the HD(9,...) entries in the fixture.
HD9_UUID = '5dd8ef9c-ef8a-e24c-a3a3-d5ec461cf143'


def make_dude(testfile=None, verbose=False, uuids=None):
    """Create an ``EfiBootDude`` without running ``__init__`` (no curses).

    ``sysinfo`` is created via ``__new__`` and populated with empty mappings so
    the tests never depend on the host's real mounts/partitions.
    """
    dude = EfiBootDude.__new__(EfiBootDude)
    dude.testfile = testfile
    dude.opts = type('Opts', (), {'verbose': verbose})()
    dude.sysinfo = SystemInfo.__new__(SystemInfo)
    dude.sysinfo.mounts = {}
    dude.sysinfo.uuids = dict(uuids) if uuids else {}
    return dude


# --------------------------------------------------------------------------- #
# SystemInfo
# --------------------------------------------------------------------------- #
def test_unescape_mount_decodes_octal_escapes():
    assert SystemInfo._unescape_mount(r'/mnt/my\040dir') == '/mnt/my dir'
    assert SystemInfo._unescape_mount(r'/a\011b') == '/a\tb'
    assert SystemInfo._unescape_mount('/plain/path') == '/plain/path'


def test_get_mounts_decodes_escaped_paths(tmp_path, monkeypatch):
    fake = tmp_path / 'mounts'
    fake.write_text(
        '/dev/sda1 /boot/efi vfat rw 0 0\n'
        '/dev/sdb1 /mnt/my\\040dir ext4 rw 0 0\n',
        encoding='utf-8',
    )

    real_open = open

    def fake_open(path, *args, **kwargs):
        if path == '/proc/mounts':
            return real_open(fake, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr('builtins.open', fake_open)
    mounts = SystemInfo.get_mounts()

    assert mounts['/dev/sda1'] == '/boot/efi'
    assert mounts['/dev/sdb1'] == '/mnt/my dir'


def test_extract_uuids():
    line = (r'HD(1,GPT,00112233-4455-6677-8899-aabbccddeeff,0x800,0x1000)'
            r'/File(\EFI\x.efi)')
    assert SystemInfo.extract_uuids(line) == [
        '00112233-4455-6677-8899-aabbccddeeff']
    assert SystemInfo.extract_uuids('no uuid here') == []


# --------------------------------------------------------------------------- #
# digest_boots
# --------------------------------------------------------------------------- #
def test_digest_fixture_counts_and_order():
    dude = make_dude(testfile=FIXTURE)
    entries = dude.digest_boots()

    # 3 leading system lines (BootNext/BootCurrent/Timeout) + 10 boot entries
    assert dude.boot_idx == 3
    assert len(entries) == 13

    idents = [e.ident for e in entries]
    assert idents[:3] == ['BootNext:', 'BootCurrent:', 'Timeout:']

    # Boot order from the fixture starts with 000A, 0009, ...
    assert idents[3] == '000A'
    assert idents[4] == '0009'

    # Entries absent from BootOrder keep their relative order at the end.
    assert idents[-2:] == ['0001', '0002']


def test_digest_fixture_bootnext_is_synthesized():
    dude = make_dude(testfile=FIXTURE)
    entries = dude.digest_boots()
    assert entries[0].ident == 'BootNext:'
    assert entries[0].label == '---'
    assert entries[0].is_boot is False


def test_digest_uuid_translation_to_mount_point():
    dude = make_dude(testfile=FIXTURE, uuids={HD9_UUID: '/boot/efi'})
    dude.digest_boots()
    by_id = {e.ident: e for e in dude.boot_entries if e.is_boot}

    ubuntu = by_id['000A']
    assert ubuntu.label == 'Ubuntu'
    assert ubuntu.active == '*'
    assert ubuntu.info1 == '/boot/efi'
    assert ubuntu.info2.strip() == r'\EFI\ubuntu\shimx64.efi'
    assert ubuntu.raw_device == r'/boot/efi|\EFI\ubuntu\shimx64.efi'


def test_digest_without_bootorder_line(tmp_path):
    """Regression: output lacking a 'BootOrder:' line must not raise."""
    content = (
        'BootCurrent: 0000\n'
        'Timeout: 5 seconds\n'
        'Boot0000* Test Entry\t'
        'HD(1,GPT,00112233-4455-6677-8899-aabbccddeeff,0x800,0x1000)'
        '/File(\\EFI\\test\\bootx64.efi)\n'
    )
    path = tmp_path / 'efibootmgr.txt'
    path.write_text(content, encoding='utf-8')

    dude = make_dude(testfile=str(path))
    entries = dude.digest_boots()  # must not raise UnboundLocalError

    idents = [e.ident for e in entries]
    assert '0000' in idents
    assert 'BootOrder:' not in idents
    assert dude.boot_idx == 3  # BootNext, BootCurrent, Timeout


# --------------------------------------------------------------------------- #
# format_boot_entry
# --------------------------------------------------------------------------- #
def _entry(**kwargs):
    kwargs.setdefault('ident', '0002')
    kwargs.setdefault('is_boot', True)
    kwargs.setdefault('active', '*')
    return BootEntry(**kwargs)


def test_format_terse_replaces_firmware_volume():
    dude = make_dude(verbose=False)
    dude.width1, dude.label_wid = 0, 0
    line = dude.format_boot_entry(
        _entry(label='UEFI Shell', info1='FvVol(abc)/FvFile(def)'))
    assert '[Firmware]' in line
    assert 'FvVol' not in line


def test_format_verbose_keeps_raw_firmware_path():
    dude = make_dude(verbose=True)
    dude.width1, dude.label_wid = 0, 0
    line = dude.format_boot_entry(
        _entry(label='UEFI Shell', info1='FvVol(abc)/FvFile(def)'))
    assert 'FvVol(abc)/FvFile(def)' in line


def test_format_removed_entry_shows_rmv():
    dude = make_dude(verbose=True)
    dude.width1, dude.label_wid = 0, 0
    line = dude.format_boot_entry(_entry(ident='0007', label='Ubuntu',
                                         removed=True))
    assert '-RMV' in line
    assert '0007' not in line


# --------------------------------------------------------------------------- #
# build_commands (privileged command construction)
# --------------------------------------------------------------------------- #
def test_build_commands_are_arg_lists():
    dude = make_dude()
    dude.boot_entries = [
        BootEntry(ident='BootNext:', label='---'),
        BootEntry(ident='0007', is_boot=True, active='*', label='Ubuntu'),
        BootEntry(ident='0009', is_boot=True, active='*', label='Fedora',
                  removed=True),
    ]
    dude.sysinfo.mounts = {'/dev/nvme0n1p1': '/boot/efi'}
    dude.mods = BootModifications()
    dude.mods.removes.add('0009')
    dude.mods.tags['0007'] = 'My Label; rm -rf /'
    dude.mods.copies.append(('New Label', r'/boot/efi|\EFI\ubuntu\shimx64.efi'))
    dude.mods.order = True
    dude.mods.next = '0007'
    dude.mods.timeout = '5'

    cmds = dude.build_commands()

    # Every command is an argv list, never a shell string.
    assert cmds
    assert all(isinstance(cmd, list) for cmd in cmds)
    assert all(cmd[:2] == ['sudo', 'efibootmgr'] for cmd in cmds)

    # A label containing shell metacharacters is passed through verbatim as a
    # single argv element (so no shell can act on the '; rm -rf /').
    tag_cmd = [c for c in cmds if '--label' in c and '0007' in c][0]
    assert tag_cmd[-1] == 'My Label; rm -rf /'

    # Disk/partition are derived from the mount point for copy (--create).
    create_cmd = [c for c in cmds if '--create' in c][0]
    assert create_cmd[create_cmd.index('--disk') + 1] == '/dev/nvme0n1'
    assert create_cmd[create_cmd.index('--part') + 1] == '1'
    assert create_cmd[create_cmd.index('--loader') + 1] == r'\EFI\ubuntu\shimx64.efi'

    # Boot order and other scalar options are present.
    order_cmd = [c for c in cmds if '--bootorder' in c][0]
    assert order_cmd[-1] == '0007'  # 0009 is removed
    assert ['sudo', 'efibootmgr', '--quiet', '--bootnext', '0007'] in cmds
    assert ['sudo', 'efibootmgr', '--quiet', '--timeout', '5'] in cmds


def test_build_commands_delete_bootnext():
    dude = make_dude()
    dude.boot_entries = [BootEntry(ident='BootNext:', label='0007')]
    dude.mods = BootModifications()
    dude.mods.next = '---'
    assert ['sudo', 'efibootmgr', '--quiet', '--delete-bootnext'] in dude.build_commands()


# --------------------------------------------------------------------------- #
# Header line (console-window fancy-header markup)
# --------------------------------------------------------------------------- #
def test_get_keys_line_uses_fancy_header_markup():
    from console_window.header import KEY, parse_fancy_header

    dude = make_dude()
    dude.opts.theme = 'default'
    dude.actions = {'u': 'up', 'w': 'wRITE', '*': 'inact'}

    line = dude.get_keys_line()

    assert line == ('[u]p [w]RITE [*]:inact [v]erbose '
                    '[T]heme=default [?]:help [q]uit')
    # The brackets are markup: the rendered text drops them, and every hot key
    # is a KEY span, so add_fancy_header(mode='Underline') styles them alike.
    spans = parse_fancy_header(line)
    assert ''.join(span.text for span in spans) == (
        'up wRITE *:inact verbose Theme=default ?:help quit')
    key_chars = {'u', 'w', '*', 'v', 'T', '?', 'q'}
    assert {span.text for span in spans if span.role == KEY} == key_chars


# --------------------------------------------------------------------------- #
# write(): curses suspend/resume around the privileged commands
# --------------------------------------------------------------------------- #
def test_write_suspends_and_resumes_curses(monkeypatch):
    dude = make_dude()
    dude.mods = BootModifications()
    dude.mods.removes.add('0002')
    dude.mods.dirty = True
    calls = []

    class FakeWin:
        """Stands in for ConsoleWindow: tracks the suspend/resume pair."""

        def __init__(self):
            self.scr = object()

        def suspend_curses(self):
            calls.append('suspend')
            self.scr = None

        def resume_curses(self):
            calls.append('resume')
            self.scr = object()

        def clear(self):
            # main_loop() calls this right after do_key(); it must not see a
            # detached window (the old crash was scr=None after start_curses()).
            assert self.scr is not None, 'clear() ran with scr=None'

    dude.win = FakeWin()
    monkeypatch.setattr('builtins.input', lambda *args, **kwargs: 'n')
    monkeypatch.setattr('os.system', lambda *args, **kwargs: 0)

    dude.write()

    assert calls == ['suspend', 'resume']
    dude.win.clear()


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__]))
