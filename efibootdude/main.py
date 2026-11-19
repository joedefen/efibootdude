#!/usr/bin/env python3
"""
efibootdude - Interactive TUI wrapper for efibootmgr
"""
import argparse
import copy
import curses as cs
import os
import re
import shlex
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from types import SimpleNamespace

from console_window import (
    Appearance,
    BasicHelpScreen,
    ConsoleWindow,
    ConsoleWindowOpts,
    Glyphs,
    InlineConfirmation,
    OptionSpinner,
    Screen,
    ScreenStack,
    Theme,
    semantic_attr,
)

from . import __version__

HOME_ST = 0
HELP_ST = 1
SCREENS = ('BOOT', 'HELP')

INLINE_PROMPTS = {
    'copy': 'Enter label for new copy',
    'tag': 'Enter new label for entry',
    'timeout': 'Enter timeout (seconds)',
}

WRITE_HINT = 'wRITE'

def initial_theme(environ=None) -> str:
    """The theme a new process should start on."""
    return Theme.resolve(Appearance.resolve_appearance(
        saved_theme='', environ=environ))

# Use slots for memory efficiency and typo protection.
# (``slots=True`` requires Python 3.10+; the project floor is now 3.11.)
_dataclass_kwargs = {'slots': True}


@dataclass(**_dataclass_kwargs)
class BootEntry:
    """Represents a boot entry or system info line from efibootmgr."""
    ident: str
    is_boot: bool = False # Boot entry identifier (e.g., '0007') or system field name
    active: str = ''    # BIOS state ('*" if active here)
    label: str = ''     # Human readable name
    info1: str = ''     # primary info (mount point or firmware path)
    info2: str = ''     # EFI path
    removed: bool = False   # mark for removal?
    raw_device: str = '' # e.g., 'HD(1,GPT,uuid,...)\\File(\\EFI\\ubuntu\\shimx64.efi)'
    pending_copy: bool = False # True for fake '+ADD' row of a pending copy


@dataclass(**_dataclass_kwargs)
class BootModifications:
    """Tracks pending modifications to boot configuration."""

    dirty: bool = False     # changes pending?
    order: bool = False     # order modified?
    timeout: str | None = None  # new value if modified
    removes: set = field(default_factory=set)   # set of IDs to rmv
    tags: dict = field(default_factory=dict)    # map of new labels
    next: str | None = None                     # ID of next boot if set
    actives: set = field(default_factory=set)   # IDs to mark active
    inactives: set = field(default_factory=set) # IDs to mark inactive
    copies: list = field(default_factory=list)  # boot entries to create


class SystemInfo:
    """Gather system information about mounts and partitions."""
    def __init__(self):
        self.mounts = self.get_mounts() # paths to mount points
        self.uuids = self.get_part_uuids() # UUIDs to (dev, mount_point)

    @staticmethod
    def _unescape_mount(text):
        """Decode the octal escapes used by /proc/mounts.

           ``/mnt/my\\040dir``-> ``/mnt/my dir``
        """
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), text)

    @staticmethod
    def get_mounts():
        """Get a dictionary of device-to-mount-point"""
        mounts = {}
        with open('/proc/mounts', encoding='utf-8') as mounts_file:
            for line in mounts_file:
                parts = line.split()
                dev = SystemInfo._unescape_mount(parts[0])
                mount_point = SystemInfo._unescape_mount(parts[1])
                mounts[dev] = mount_point
        return mounts

    def get_part_uuids(self):
        """Get all partition UUIDs mapped to device paths or preferably mount points."""
        uuids = {}
        partuuid_path = '/dev/disk/by-partuuid/'

        if not os.path.exists(partuuid_path):
            return uuids
        for entry in os.listdir(partuuid_path):
            full_path = os.path.join(partuuid_path, entry)
            if os.path.islink(full_path):
                device_path = os.path.realpath(full_path)
                uuids[entry] = device_path
                # Prefer mount point over device path if available
                if device_path in self.mounts:
                    uuids[entry] = self.mounts[device_path]
        return uuids

    @staticmethod
    def extract_uuids(line):
        """Find uuid string in a line"""
        # Define the regex pattern for UUID (e.g., 25d2dea1-9f68-1644-91dd-4836c0b3a30a)
        pattern = r'\b[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\b'
        mats = re.findall(pattern, line, re.IGNORECASE)
        return mats

    def refresh(self):
        """Refresh system information"""
        self.mounts = self.get_mounts()
        self.uuids = self.get_part_uuids()


class EfiBootDude:
    """Main TUI application for managing UEFI boot entries."""
    singleton = None
    window_backend = None       # HeadlessBackend in tests
    window_singleton = True     # one terminal, one window in production

    def __init__(self, testfile=None, theme=None):
        assert not EfiBootDude.singleton
        EfiBootDude.singleton = self
        self.testfile = testfile
        self.redraw = False # force redraw after a suspend/resume
        # A testfile is a replay of efibootmgr output.
        if not testfile:
            self.check_prereqs()
        self.sysinfo = SystemInfo()
        self.mods = BootModifications()
        self.boot_entries, self.width1, self.label_wid, self.boot_idx = [], 0, 0, 0
        self.display_entries = []
        self.actions = {} # currently available actions, rebuilt every frame
        self.inline_confirm = InlineConfirmation()
        self._inline_action = None  # 'timeout', 'tag' or 'copy'
        self._inline_context = {}   # target entry (and the original label)
        self._inline_pick_pos = None

        self.win = None
        self.reinit()

        self.theme = Theme.resolve(theme)
        self.opts = SimpleNamespace(verbose=False, theme=self.theme)

        win_opts = ConsoleWindowOpts(
            head_line=True,
            body_rows=len(self.boot_entries) + 20,
            head_rows=10,
            min_cols_rows=(70, 10),
            ctrl_c_terminates=False,
            theme=self.theme,
        )
        self.win = ConsoleWindow(win_opts,
                                 backend=self.window_backend,
                                 singleton=self.window_singleton)

        self.boot_screen = BootScreen(self)
        self.help_screen = HelpScreen(self)
        self.ss = ScreenStack(self.win, self.opts, SCREENS,
                              {HOME_ST: self.boot_screen,
                               HELP_ST: self.help_screen})
        self.spin = OptionSpinner(
            stack=self.ss,
            nav_keys=self.win.opts.nav_keys,
            # The library registers quit before the app
            # registers anything; its default is Ctrl-C.  x is this app's extra quit key.
            quit_keys=(ord('q'), ord('x'), 0x3),
            escape_keys=[27], # ESC is the back button.
        )
        self._register_options()
        self.win.set_handled_keys(self.spin)
        self.boot_screen.pick_pos = self.boot_idx  # start at first boot entry
        self.boot_screen.set_pick_mode(True)

    def _register_options(self):
        """Register every key.  The library owns quit and escape."""
        spin = self.spin
        spin.add_key('help_screen', '? - help', genre='action')
        spin.add_key('verbose', 'v - toggle verbose', vals=[False, True])
        spin.add_key('up', 'u - move boot entry up', genre='action')
        spin.add_key('down', 'd - move boot entry down', genre='action')
        spin.add_key('remove', 'r - remove boot entry', genre='action')
        spin.add_key('copy', 'c - copy boot entry with new label', genre='action')
        spin.add_key('next', 'n - set next boot to boot entry OR cycle its values', genre='action')
        spin.add_key('star', '* - toggle whether entry is active', genre='action')
        spin.add_key('tag', 't - set a new label for the boot entry', genre='action')
        spin.add_key('modify', 'm - modify the value (on Timeout line)', genre='action')
        spin.add_key('write', 'w - write changes', genre='action')
        spin.add_key('boot', 'b - reboot the machine', genre='action')
        spin.add_key('theme', 'T - cycle theme', vals=Theme.list_all(),
                     comments='Deliberately shown on the help page only, never '
                              'on the one-line header.')

    def reinit(self):
        """ RESET EVERYTHING"""
        self.sysinfo.refresh()
        self.mods = BootModifications()
        self.boot_entries, self.width1, self.label_wid, self.boot_idx = [], 0, 0, 0
        self.display_entries = []
        self.digest_boots()

        # Save original state for detecting actual changes
        self.original_entries = copy.deepcopy(self.boot_entries)

        if getattr(self, 'boot_screen', None) is not None:
            self.boot_screen.pick_pos = self.boot_idx

    def digest_boots(self):
        """ Digest the output of 'efibootmgr'."""
        # Define the command to run
        lines = []
        if self.testfile:
            # if given a "testfile" (which should be just the raw output of 'efibootmgr'),
            # then parse it
            with open(self.testfile, encoding='utf-8') as fh:
                lines = fh.read().splitlines()
        else: # run efibootmgr
            command = ['efibootmgr']
            result = subprocess.run(command, stdout=subprocess.PIPE, text=True, check=True)
            lines = result.stdout.splitlines()
        rv = []
        width1 = 0  # width of info1
        label_wid = 0
        boots = {}
        boot_order = ''  # default in case the efibootmgr output lacks BootOrder:
        for line in ['BootNext: ---'] + lines:
            parts = line.split(maxsplit=1)
            if len(parts) < 2:
                continue
            key, info = parts[0], parts[1]

            if key == 'BootOrder:':
                boot_order = info
                continue

            ns = BootEntry(ident='')

            mat = re.match(r'\bBoot([0-9a-f]+)\b(\*?)' # Boot0024*
                           + r'\s+(\S.*\S|\S)\s*\t' # Linux Boot Manager
                           + r'\s*(\S.*\S|\S)\s*$', # HD(4,GPT,cd15e3b1-...
                           line, re.IGNORECASE)
            if not mat:
                ns.ident = key
                ns.label = info
                if key == 'BootNext:' and len(rv) > 0:
                    rv[0] = ns
                else:
                    rv.append(ns)
                continue

            ns.ident = mat.group(1)
            ns.is_boot = True
            ns.active = mat.group(2)
            ns.label = mat.group(3)
            label_wid = max(label_wid, len(ns.label))
            other = mat.group(4)

            # Extract EFI path (e.g., \EFI\ubuntu\shimx64.efi) from the boot entry
            pat = r'(?:/?\b\w*\(|/)(\\[^/()]+)(?:$|[()/])'
            mat = re.search(pat, other, re.IGNORECASE)
            device, subpath = '', '' # e.g., /boot/efi, \EFI\UBUNTU\SHIMX64.EFI
            if mat:
                subpath = mat.group(1) + ' '
                start, end = mat.span()
                other = other[:start] + other[end:]

            # Critical UUID translation: Convert GPT partition UUIDs to readable paths
            # Example: HD(1,GPT,abc123-...) -> /boot/efi (via sysinfo.uuids mapping)
            uuids = SystemInfo.extract_uuids(other)
            for uuid in uuids:
                if uuid and uuid in self.sysinfo.uuids:
                    device = self.sysinfo.uuids[uuid]
                    break

            # Store device and EFI path for copy operation (if both
            # available) Device could be /boot/efi or /dev/nvme0n1p1
            # Subpath is the .efi file like \EFI\ubuntu\shimx64.efi
            if device and subpath:
                ns.raw_device = f"{device}|{subpath.strip()}"

            if device:
                ns.info1 = device
                # Only show subpath if present; for USB/network devices without .efi files, leave info2 empty
                ns.info2 = subpath if subpath else ''
                width1 = max(width1, len(ns.info1))
            elif subpath:
                ns.info1 = subpath
                ns.info2 = other
            else:
                ns.info1 = other
            boots[ns.ident] = ns

        self.boot_idx = len(rv)
        self.width1 = width1
        self.label_wid = label_wid

        for ident in boot_order.split(','):
            if ident in boots:
                rv.append(boots.pop(ident))
        rv += list(boots.values())

        self.boot_entries = rv
        return rv

    def update_dirty_state(self):
        """Recalculate dirty flag based on actual changes from original state."""
        # Extract original values from the deep copy
        original_order = [ns.ident for ns in self.original_entries if ns.is_boot]
        original_actives = {ns.ident for ns in self.original_entries if ns.is_boot and ns.active}
        original_timeout = next((ns.label for ns in self.original_entries if ns.ident == 'Timeout:'), None)
        original_next = next((ns.label for ns in self.original_entries if ns.ident == 'BootNext:'), None)

        # Check if boot order actually changed (excluding removed entries)
        current_order = [ns.ident for ns in self.boot_entries if ns.is_boot and not ns.removed]
        order_changed = (self.mods.order and current_order != original_order)

        # Check if active states actually changed
        current_actives = {ns.ident for ns in self.boot_entries if ns.is_boot and ns.active}
        # Active state changed if we have pending changes that would alter the state
        actives_changed = bool(self.mods.actives or self.mods.inactives)
        if actives_changed:
            # Simulate what the actives would be after applying changes
            simulated_actives = current_actives.copy()
            simulated_actives.update(self.mods.actives)
            simulated_actives.difference_update(self.mods.inactives)
            actives_changed = simulated_actives != original_actives

        # Check timeout - compare the numeric value, not the label format
        timeout_changed = False
        if self.mods.timeout is not None:
            # Extract original timeout value (e.g., "2 seconds" -> "2")
            original_timeout_val = original_timeout.split()[0] if original_timeout else None
            timeout_changed = self.mods.timeout != original_timeout_val

        # Check next boot
        next_changed = False
        if self.mods.next is not None:
            next_changed = self.mods.next != original_next

        # Check removals, tag changes, and copies
        removals_changed = bool(self.mods.removes)
        tags_changed = bool(self.mods.tags)
        copies_changed = bool(self.mods.copies)

        # Update dirty flag
        self.mods.dirty = (
            order_changed or
            actives_changed or
            timeout_changed or
            next_changed or
            removals_changed or
            tags_changed or
            copies_changed
        )

    def format_boot_entry(self, entry: BootEntry) -> str:
        """Format a boot entry for display, applying verbose/terse filtering."""
        info1 = entry.info1
        info2 = entry.info2

        if not self.opts.verbose:
            # Clean up firmware volume references (BIOS internal apps)
            info1 = re.sub(r'FvVol\([^)]+\)/FvFile\([^)]+\)', '[Firmware]', info1)
            info2 = re.sub(r'FvVol\([^)]+\)/FvFile\([^)]+\)', '[Firmware]', info2)

            # Clean up PCI device paths for auto-created entries
            if '{auto_created_boot_option}' in info1:
                info1 = re.sub(r'PciRoot\([^{]+', '', info1)
                info1 = info1.replace('{auto_created_boot_option}', '[Auto]')
            if '{auto_created_boot_option}' in info2:
                info2 = re.sub(r'PciRoot\([^{]+', '', info2)
                info2 = info2.replace('{auto_created_boot_option}', '[Auto]')

            # Clean up vendor hardware/messaging paths
            mat = re.search(r'/?VenHw\(.*$', info1, re.IGNORECASE)
            if mat:
                start, _ = mat.span()
                info1 = info1[:start] + '[Vendor HW]'
            mat = re.search(r'/?VenMsg\(.*$', info1, re.IGNORECASE)
            if mat:
                start, _ = mat.span()
                info1 = info1[:start] + '[Vendor Msg]'

            # Strip PCI device path prefix for USB/network entries Keep just
            # USB(...) or similar instead of PciRoot(...)/Pci(...)/USB(...)
            info1 = re.sub(r'PciRoot\([^)]*\)(?:/Pci\([^)]*\))*/', '', info1)
            info2 = re.sub(r'PciRoot\([^)]*\)(?:/Pci\([^)]*\))*/', '', info2)

        # Display removed entries with -RMV instead of ident
        display_ident = '-RMV' if entry.removed else entry.ident
        line = f'{entry.active:>1} {display_ident:>4} {entry.label:<{self.label_wid}}'
        line += f' {info1:<{self.width1}} {info2}'
        return line

    def row_attr(self, entry: BootEntry):
        """The attribute for one listing row, resolved through Appearance.

        The project deliberately has no --no-color flag and no saved theme,
        but semantic_attr still gives the one thing a plain
        curses.color_pair() cannot: a monochrome attribute (and plain
        text on a terminal without color) instead of an exception,
        so a redraw never dies over decoration.
        """
        if entry.pending_copy:
            return semantic_attr(Theme.SUCCESS)
        if entry.is_boot and entry.removed:
            return semantic_attr(Theme.HOTSWAP)
        if entry.is_boot and not entry.active:
            return cs.A_DIM
        return None

    def make_display_entries(self):
        """The rows to draw: boot entries with synthetic '+ADD' rows inserted."""
        entries = []
        pending_copies_added = False
        for i, entry in enumerate(self.boot_entries):
            if not pending_copies_added and self.mods.copies:
                if (entry.is_boot and entry.removed) or i == len(self.boot_entries) - 1:
                    for label, raw_device in self.mods.copies:
                        entries.append(BootEntry(
                            ident='+ADD',
                            label=label,
                            info1='[pending copy]',
                            pending_copy=True,
                            raw_device=raw_device,
                        ))
                    pending_copies_added = True
            entries.append(entry)
        return entries

    def picked_entry(self, pos: int) -> BootEntry | None:
        """The row at display position *pos*, or None when there is no row."""
        entries = self.display_entries or self.boot_entries
        if 0 <= pos < len(entries):
            return entries[pos]
        return None

    def boot_index(self, entry: BootEntry) -> int | None:
        """The entry's position in ``boot_entries`` (identity, not equality)."""
        for index, candidate in enumerate(self.boot_entries):
            if candidate is entry:
                return index
        return None

    def display_index(self, entry: BootEntry) -> int:
        """The entry's row on the next frame, synthetic copies included."""
        for index, candidate in enumerate(self.make_display_entries()):
            if candidate is entry:
                return index
        return 0

    def move_picked(self, pos: int, delta: int) -> int:
        """Move the picked boot entry by *delta* and return its new row."""
        entry = self.picked_entry(pos)
        if entry is None or not entry.is_boot or entry.removed:
            return pos
        boot_pos = self.boot_index(entry)
        if boot_pos is None:
            return pos
        target = boot_pos + delta
        if target < self.boot_idx or target >= len(self.boot_entries):
            return pos
        if self.boot_entries[target].removed:
            return pos
        self.boot_entries[boot_pos], self.boot_entries[target] = (
            self.boot_entries[target], self.boot_entries[boot_pos])
        self.mods.order = True
        # Recompute rather than adding delta: a pending '+ADD' row above
        # the entry makes a display row and a boot-list index differ.
        return self.display_index(entry)

    def toggle_active(self, pos: int):
        """Toggle whether the picked boot entry is active."""
        entry = self.picked_entry(pos)
        if entry is None or not entry.is_boot:
            return
        ident = entry.ident
        if entry.active:
            entry.active = ''
            self.mods.actives.discard(ident)
            self.mods.inactives.add(ident)
        else:
            entry.active = '*'
            self.mods.actives.add(ident)
            self.mods.inactives.discard(ident)

    def cycle_next(self, pos: int):
        """Set BootNext: cycle the BootNext row, or point it at this entry."""
        entry = self.picked_entry(pos)
        if entry is None:
            return
        if entry.ident == 'BootNext:':
            # Cycle through non-removed boot entries + '---' (default)
            idents = [b.ident for b in self.boot_entries if b.is_boot and not b.removed]
            if not idents:
                return
            cycle = idents + ['---']
            current = entry.label
            index = cycle.index(current) if current in cycle else -1
            next_ident = cycle[(index + 1) % len(cycle)]
            entry.label = next_ident
            # Get original value to detect if we're cycling back to it
            original_next = next((ns.label for ns in self.original_entries
                                  if ns.ident == 'BootNext:'), None)
            # Only record change if different from original
            self.mods.next = next_ident if next_ident != original_next else None
        elif entry.is_boot:
            # Set this boot entry as next; row 0 is the BootNext line.
            self.boot_entries[0].label = entry.ident
            self.mods.next = entry.ident

    def remove_picked(self, pos: int) -> int:
        """Remove, un-remove, or cancel a pending copy; return the new pick row."""
        entry = self.picked_entry(pos)
        if entry is None:
            return pos
        # Handle removal of pending copy entries
        if entry.pending_copy:
            for i, (label, raw_device) in enumerate(self.mods.copies):
                if label == entry.label and raw_device == entry.raw_device:
                    self.mods.copies.pop(i)
                    break
            return min(pos, max(len(self.make_display_entries()) - 1, 0))
        if not entry.is_boot:
            return pos

        # Undoable remove feature... Mark as removed and move to bottom.
        # Entry shows as "-RMV" and can be restored with 'r'.
        boot_pos = self.boot_index(entry)
        if boot_pos is None:
            return pos  # Entry not found in boot_entries

        if entry.removed:
            # Un-remove: restore entry just before the first removed entry
            entry.removed = False
            self.mods.removes.discard(entry.ident)
            insert_pos = len(self.boot_entries)
            for i in range(self.boot_idx, len(self.boot_entries)):
                if (self.boot_entries[i].is_boot and self.boot_entries[i].removed
                        and i != boot_pos):
                    insert_pos = i
                    break
            moved = self.boot_entries.pop(boot_pos)
            # Adjust insert_pos if we removed an item before it
            if boot_pos < insert_pos:
                insert_pos -= 1
            self.boot_entries.insert(insert_pos, moved)
            self.mods.order = True
            return self.display_index(moved)

        # Mark for removal and move to bottom
        entry.removed = True
        self.mods.removes.add(entry.ident)
        self.mods.actives.discard(entry.ident)
        self.mods.inactives.discard(entry.ident)

        # Auto-fix BootNext if it points to the removed entry
        bootnext_entry = self.boot_entries[0]
        if bootnext_entry.ident == 'BootNext:' and bootnext_entry.label == entry.ident:
            bootnext_entry.label = '---'
            self.mods.next = None

        # Move to bottom of list; keep the cursor where it was,
        # so the next entry moves up into the highlighted row.
        self.boot_entries.append(self.boot_entries.pop(boot_pos))
        self.mods.order = True
        return min(pos, max(len(self.make_display_entries()) - 1, 0))

    def begin_inline(self, action: str, pos: int):
        """Start the in-place editor for *action* on the picked entry."""
        entry = self.picked_entry(pos)
        if entry is None:
            return
        if action == 'copy':
            if not (entry.is_boot and entry.raw_device):
                msg = (f"Copy blocked: is_boot={entry.is_boot}, "
                       f"raw_device={'empty' if not entry.raw_device else 'set'}")
                self.win.alert(msg)
                return
            seed = entry.label
            self._inline_context = {'entry': entry, 'original_label': entry.label}
        elif action == 'tag':
            if not entry.is_boot:
                return
            seed = entry.label
            self._inline_context = {'entry': entry}
        elif action == 'timeout':
            if entry.ident != 'Timeout:':
                return
            seed = entry.label.split()[0]
            self._inline_context = {'entry': entry}
        else:
            return
        self._inline_action = action
        self._inline_pick_pos = pos
        self.inline_confirm.start(action_type=action, mode='text', initial=seed)

    @property
    def inline_entry(self) -> BootEntry | None:
        """The entry the active in-place prompt belongs to, if any."""
        return self._inline_context.get('entry')

    def inline_prompt_line(self) -> str:
        """The in-place prompt row drawn under the entry being edited."""
        hint = self.inline_confirm.get_hint()
        current_value = self.inline_confirm.input_buffer
        action_prompt = INLINE_PROMPTS.get(
            self._inline_action, (self._inline_action or 'INPUT').upper())
        arrow = Glyphs.get('star_glow')
        if not current_value and hint:
            return f'  {arrow} {hint}'
        return f'  {arrow} {action_prompt}: {current_value}_'

    def handle_inline_key(self, key: int):
        """Route one key to the in-place editor, which is modal over the app."""
        result = self.inline_confirm.handle_key(key)
        if result == 'confirmed':
            self._process_inline_confirmation()
        elif result == 'cancelled':
            self.inline_confirm.cancel()
        # A rejected value keeps the editor open (it shows an alert and clears the buffer),
        # so only forget the context once the editor is gone.
        if not self.inline_confirm.active:
            self._inline_action = None
            self._inline_context = {}
            if self._inline_pick_pos is not None:
                self.boot_screen.pick_pos = self._inline_pick_pos
                self._inline_pick_pos = None

    @staticmethod
    def _key_hint(key: str, verb: str) -> str:
        """One hint as markup: ``[u]p`` rather than ``[u]:up`` when possible.

        The key only ever appears once,
        so drop the redundant prefix when the verb already starts with it.
        """
        if key[0] == verb[0]:
            return f' [{key[0]}]{verb[1:]}'
        return f' [{key[0]}]{key[1:]}:{verb}'

    @classmethod
    def _write_hint(cls, actions: dict) -> str:
        """The leading wRITE slot, blanked instead of dropped when clean."""
        hint = cls._key_hint('w', WRITE_HINT)
        if 'w' in actions:
            return hint
        return ' ' * len(hint.replace('[', '').replace(']', ''))

    def get_keys_line(self, actions=None) -> str:
        """ Build the context-sensitive key hints for the header."""
        actions = self.actions if actions is None else actions
        line = self._write_hint(actions)
        for key, verb in actions.items():
            if key == 'w':
                continue
            line += self._key_hint(key, verb)
        line += ' [v]:terse' if self.opts.verbose else ' [v]erbose'
        line += ' [?]:help [q]uit'
        return line[1:]

    def get_actions(self, pos: int) -> dict:
        """ Determine the type of the current line and available commands."""
        actions = {}
        entry = self.picked_entry(pos)
        if entry is None:
            return actions
        if self.mods.dirty:
            actions['w'] = WRITE_HINT # unusual case to indicate dirty
        # Pending copy entries: only allow removal
        if entry.pending_copy:
            actions['r'] = 'cancel'
        elif entry.is_boot:
            if not entry.removed:
                boot_pos = self.boot_index(entry)
                # Non-removed entry: can move up/down but not into removed section
                if boot_pos is not None and boot_pos > self.boot_idx:
                    actions['u'] = 'up'
                # Check if next entry exists and is not removed
                if (boot_pos is not None
                        and boot_pos + 1 < len(self.boot_entries)
                        and not self.boot_entries[boot_pos + 1].removed):
                    actions['d'] = 'down'
                actions['n'] = 'next'
                actions['t'] = 'tag'
                actions['*'] = 'inact' if entry.active else 'act'
                # Copy only available if we have raw device info
                if entry.raw_device:
                    actions['c'] = 'copy'
            # Removed entries: no up/down, no next, no tag, no toggle active
            actions['r'] = 'unrmv' if entry.removed else 'rmv'
        elif entry.ident == 'BootNext:':
            actions['n'] = 'cycle'
        elif entry.ident in ('Timeout:',):
            actions['m'] = 'modify'
        if not self.mods.dirty:
            actions['b'] = 'boot'
        return actions

    def apply_theme(self):
        """Install the theme the spinner selected, if it changed."""
        chosen = Theme.resolve(self.opts.theme)
        if chosen == Theme.get_current():
            return
        self.opts.theme = chosen
        self.win.set_theme(chosen)
        self.redraw = True

    def main_loop(self):
        """ Draw, wait for one key, route it, repeat. """
        while True:
            self.apply_theme()
            self.update_dirty_state()

            screen = self.ss.get_curr_obj()
            # clear() also adopts the screen on the window: without it the
            # writers below would fill a screen the window is not painting.
            screen.clear()
            screen.draw_screen()
            self.win.render(redraw=self.redraw)
            self.redraw = False

            key = self.win.prompt(seconds=300)
            if key is None:
                # A timeout is not a key: running it through the dispatcher
                # would fire the action whose key code is 0/None.
                continue
            self.redraw = True
            if self.inline_confirm.active:
                self.handle_inline_key(key)
            else:
                self.spin.do_key(key, self.win)
                self.ss.perform_actions(self.spin)

    @staticmethod
    def check_prereqs():
        """ Check that needed programs are installed. """
        ok = True
        for prog in ['efibootmgr']:
            if shutil.which(prog) is None:
                ok = False
                print(f'ERROR: cannot find {prog!r} on $PATH')
        if not ok:
            sys.exit(1)

    @staticmethod
    def get_word0(line):
        """ Get words[1] from a string. """
        words = line.split(maxsplit=1)
        return words[0]

    def reboot(self):
        """ Reboot the machine """
        self.win.suspend_curses()
        try:
            os.system('clear; stty sane')
            # Elevated via sudo, but run as an argument list (no shell) so
            # nothing can be re-interpreted by a shell.
            cmd = ['sudo', 'reboot', 'now']
            print(f' + {shlex.join(cmd)}')
            try:
                subprocess.run(cmd, check=False)
            except OSError as exce:
                print(f'    !!! {exce}')

            # NOTE: probably will not get here...
            os.system(r'/bin/echo -e "\n\n===== Press ENTER for menu ====> \c"; read FOO')
            self.reinit()
        finally:
            self.win.resume_curses()
            self.redraw = True

    def build_commands(self):
        """ Build the privileged efibootmgr commands to run.

        Returns:
            list[list[str]]: argv lists, each with ``'sudo'`` prefix.
        """
        cmds = []
        prefix = ['sudo', 'efibootmgr', '--quiet']
        for ident in self.mods.removes:
            cmds.append(prefix + ['--delete-bootnum', '--bootnum', ident])
        for ident in self.mods.actives:
            cmds.append(prefix + ['--active', '--bootnum', ident])
        for ident in self.mods.inactives:
            cmds.append(prefix + ['--inactive', '--bootnum', ident])
        for ident, tag in self.mods.tags.items():
            cmds.append(prefix + ['--bootnum', ident, '--label', tag])
        for label, raw_device in self.mods.copies:
            # raw_device: "device|efi_path" e.g., "/boot/efi|\EFI\ubuntu\shimx64.efi"
            if '|' not in raw_device:
                continue
            device_path, efi_path = raw_device.split('|', 1)
            # Extract disk and partition from device path e.g.,
            # /dev/nvme0n1p1 -> disk=/dev/nvme0n1,
            # part=1 or /boot/efi -> need to find actual device from mounts
            disk, part = None, None
            if device_path.startswith('/dev/'):
                # Direct device path Handle both NVMe
                # (/dev/nvme0n1p3) and SATA/SCSI (/dev/sda3)
                mat = re.match(r'(/dev/(?:nvme\d+n\d+|[a-z]+))p?(\d+)$', device_path)
                if mat:
                    disk, part = mat.group(1), mat.group(2)
            else:
                # Mount point - reverse lookup to device
                for dev, mnt in self.sysinfo.mounts.items():
                    if mnt == device_path:
                        # Handle both NVMe (/dev/nvme0n1p3) and SATA/SCSI (/dev/sda3)
                        mat = re.match(r'(/dev/(?:nvme\d+n\d+|[a-z]+))p?(\d+)$', dev)
                        if mat:
                            disk, part = mat.group(1), mat.group(2)
                        break

            if disk and part:
                cmds.append(prefix + ['--create', '--disk', disk, '--part', part,
                                      '--label', label, '--loader', efi_path])
        if self.mods.order:
            orders = ','.join(ns.ident for ns in self.boot_entries
                              if ns.is_boot and not ns.removed)
            cmds.append(prefix + ['--bootorder', orders])
        if self.mods.next:
            if self.mods.next == '---':
                cmds.append(prefix + ['--delete-bootnext'])
            else:
                cmds.append(prefix + ['--bootnext', self.mods.next])
        if self.mods.timeout:
            cmds.append(prefix + ['--timeout', self.mods.timeout])
        return cmds

    def write(self):
        """ Commit the changes. """
        if not self.mods.dirty:
            return

        cmds = self.build_commands()
        self.win.suspend_curses()
        try:
            os.system('clear; stty sane')
            print('Commands:')
            for cmd in cmds:
                print(f' + {shlex.join(cmd)}')
            yes = input("Run the above commands? (yes/No) ")

            if yes.lower().startswith('y'):
                print()
                print()

                for cmd in cmds:
                    # Show the command (shell-quoted for readability) then run it
                    # as an argument list so no shell interprets the arguments.
                    print(f' + {shlex.join(cmd)}')
                    try:
                        ret = subprocess.run(cmd, check=False).returncode
                    except OSError as exce:
                        print(f'    !!! {exce}')
                        ret = 127  # shell convention for "command not found"
                    print(f'    <<<ExitCode={ret}>>>')

                os.system(r'/bin/echo -e "\n\n===== Press ENTER for menu ====> \c"; read FOO')
                self.reinit()
        finally:
            self.win.resume_curses()
            self.redraw = True

    def _process_inline_confirmation(self):
        """Process inline confirmation result based on action type."""
        action = self._inline_action
        value = self.inline_confirm.input_buffer.strip()

        if action == 'timeout':
            if not value:
                self.inline_confirm.cancel()
                self._inline_action = None
                return
            if re.match(r'\d+$', value):
                entry = self._inline_context['entry']
                entry.label = f'{value} seconds'
                self.mods.timeout = value
                self.inline_confirm.cancel()
                self._inline_action = None
            else:
                self.win.alert('Timeout must be digits only')
                self.inline_confirm.input_buffer = ''

        elif action == 'tag':
            if not value:
                self.inline_confirm.cancel()
                self._inline_action = None
                return
            if re.match(r'([\w\s])+$', value):
                entry = self._inline_context['entry']
                entry.label = value
                self.mods.tags[entry.ident] = value
                self.inline_confirm.cancel()
                self._inline_action = None
            else:
                self.win.alert('Label must contain only letters, numbers, or spaces')
                self.inline_confirm.input_buffer = ''

        elif action == 'copy':
            if not value:
                self.inline_confirm.cancel()
                self._inline_action = None
                return
            entry = self._inline_context['entry']
            original_label = self._inline_context['original_label']
            if value == original_label:
                # Must be different from original - alert user and stay in input mode
                self.win.alert('Label must be different from original')
                self.inline_confirm.input_buffer = ''
                return
            if re.match(r'([\w\s])+$', value):
                self.mods.copies.append((value, entry.raw_device))
                self.inline_confirm.cancel()
                self._inline_action = None
            else:
                self.win.alert('Label must contain only letters, numbers, or spaces')
                self.inline_confirm.input_buffer = ''


class EfiScreen(Screen):
    """Base for every screen in this app's stack."""

    def quit_ACTION(self):
        """Leave for good -- after the usual confirmation if edits are pending."""
        if self.app.mods.dirty:
            answer = self.win.answer(
                prompt='Enter "y" to abandon edits and exit')
            if not (answer and answer.strip().lower().startswith('y')):
                return
        ConsoleWindow.stop_all()
        os.system('clear; stty sane')
        raise SystemExit(0)


class BootScreen(EfiScreen):
    """The boot-entry listing: the app's home screen."""
    name = 'BOOT'

    def draw_screen(self):
        app = self.app
        # Pick mode is a property of what is being drawn:
        # the inline editor is the only modal state, and it hides the pick highlight.
        self.set_pick_mode(not app.inline_confirm.active)
        app.display_entries = app.make_display_entries()
        app.actions = app.get_actions(self.pick_pos)
        # add_fancy_header turns the '[k]' markup into one consistent
        # key style (underlined+bold) instead of hand-picked attrs.
        self.add_fancy_header(app.get_keys_line(), mode='Underline',
                              line_attr=cs.A_DIM)

        for entry in app.display_entries:
            self.add_body(app.format_boot_entry(entry), attr=app.row_attr(entry))
            # Display inline confirmation prompt right after the relevant entry
            if app.inline_confirm.active and app.inline_entry is entry:
                # One color for the whole prompt: DANGER
                # red means "the app is waiting on you",
                # and it does not switch when the first character is typed.
                # The marker is bold so the prompt is easy
                # to spot in a screen full of entry rows.
                self.add_body(app.inline_prompt_line(),
                              attr=semantic_attr(Theme.DANGER) | cs.A_BOLD)

    # --- actions -----------------------------------------------------------
    # An action's scope is derived from which screen
    # implements its method: every one below exists only here,
    # so the help page neither lists it nor dispatches it.

    def help_screen_ACTION(self):
        """Open the help page on top of the listing, remembering the row."""
        self.stack.push(HELP_ST, self.pick_pos)

    def up_ACTION(self):
        self.pick_pos = self.app.move_picked(self.pick_pos, -1)

    def down_ACTION(self):
        self.pick_pos = self.app.move_picked(self.pick_pos, 1)

    def star_ACTION(self):
        self.app.toggle_active(self.pick_pos)

    def next_ACTION(self):
        self.app.cycle_next(self.pick_pos)

    def remove_ACTION(self):
        self.pick_pos = self.app.remove_picked(self.pick_pos)

    def copy_ACTION(self):
        self.app.begin_inline('copy', self.pick_pos)

    def tag_ACTION(self):
        self.app.begin_inline('tag', self.pick_pos)

    def modify_ACTION(self):
        self.app.begin_inline('timeout', self.pick_pos)

    def write_ACTION(self):
        self.app.write()

    def boot_ACTION(self):
        app = self.app
        if app.mods.dirty:
            self.win.alert('Pending changes (on return, use "w" to commit '
                           'or "ESC" to discard)')
            return
        answer = self.win.answer(prompt='Type "reboot" to reboot',
                                 seed='reboot', width=80)
        if answer and answer.strip().lower().startswith('reboot'):
            app.reboot()

    def escape_ACTION(self):
        """ESC on the listing discards edits and refreshes the boot data.

        On HELP the inherited ``Screen.escape_ACTION`` pops instead,
        so ESC is the back button there and the reset key here -- one key, two screens,
        no ``help_mode`` flag in the middle.
        """
        app = self.app
        if app.mods.dirty:
            answer = self.win.answer(
                prompt='Type "y" to clear edits and refresh')
            if not (answer and answer.strip().lower().startswith('y')):
                return
        app.reinit()


class HelpScreen(EfiScreen, BasicHelpScreen):
    """The built-in help page, documenting the screen it was pushed from."""
    name = 'HELP'


def main():
    """ The program """
    if '--version' in sys.argv[1:]:
        print(f'efibootdude {__version__}')
        return

    parser = argparse.ArgumentParser()
    parser.add_argument('testfile', nargs='?', default=None)
    opts = parser.parse_args()

    dude = EfiBootDude(testfile=opts.testfile, theme=initial_theme(os.environ))
    dude.main_loop()


if __name__ == '__main__':

    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exce:
        ConsoleWindow.stop_all()
        print("exception:", str(exce))
        print(traceback.format_exc())
