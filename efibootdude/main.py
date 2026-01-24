#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
efibootdude - Interactive TUI wrapper for efibootmgr

A curses-based text user interface for managing UEFI boot entries.
Provides visual feedback and undo capabilities for common efibootmgr operations.

Architecture:
    - BootEntry: Dataclass representing a single boot entry or system info line
    - BootModifications: Dataclass tracking all pending changes (dirty state)
    - SystemInfo: Discovers system mounts and partition UUIDs
    - EfiBootDude: Main application class managing the TUI and state

Key Features:
    - Visual boot entry ordering with up/down movement
    - Toggle boot entry active/inactive state
    - Set next boot (BootNext)
    - Modify timeout and entry labels
    - Undoable remove (entries marked -RMV, restorable until committed)
    - Smart dirty detection (knows when changes are undone)
    - Verbose/terse mode for firmware path display

Workflow:
    1. Parse efibootmgr output into BootEntry objects
    2. Display in curses TUI with context-sensitive actions
    3. Track modifications in BootModifications
    4. Generate and execute efibootmgr commands on write
"""
# pylint: disable=broad-exception-caught,consider-using-with
# pylint: disable=too-many-instance-attributes,too-many-branches
# pylint: disable=too-many-return-statements,too-many-statements
# pylint: disable=consider-using-in,too-many-nested-blocks
# pylint: disable=wrong-import-position,disable=wrong-import-order
# pylint: disable=too-many-locals,line-too-long

import os
import sys
import re
import shutil
import copy
from dataclasses import dataclass, field
from typing import Optional
import subprocess
import traceback
import curses as cs
import argparse
# import xml.etree.ElementTree as ET
from console_window import ConsoleWindow, ConsoleWindowOpts, OptionSpinner, Theme, InlineConfirmation

# Use slots for memory efficiency and typo protection on Python 3.10+
_dataclass_kwargs = {'slots': True} if sys.version_info >= (3, 10) else {}

@dataclass(**_dataclass_kwargs)
class BootEntry:
    """Represents a boot entry or system info line from efibootmgr.

    Attributes:
        ident: Boot entry identifier (e.g., '0007') or system field name
               (e.g., 'BootNext:', 'Timeout:', 'BootCurrent:')
        is_boot: True if this is an actual boot entry (vs system info line)
        active: '*' if boot entry is active/enabled, '' otherwise
        label: Human-readable label (e.g., 'Ubuntu', '2 seconds', '0007')
        info1: Primary info - mount point/device (e.g., '/boot/efi', '/dev/nvme0n1p1')
               or firmware path for BIOS entries
        info2: Secondary info - EFI path (e.g., '\\EFI\\ubuntu\\shimx64.efi')
               or additional device information
        removed: True if this boot entry is marked for removal
        raw_device: Raw device string from efibootmgr (for copy operation)
                    e.g., 'HD(1,GPT,uuid,...)\\File(\\EFI\\ubuntu\\shimx64.efi)'

    Examples:
        Boot entry:    ident='0007', is_boot=True, active='*',
                      label='Ubuntu', info1='/boot/efi',
                      info2='\\EFI\\ubuntu\\shimx64.efi'

        System info:   ident='Timeout:', is_boot=False, active='',
                      label='2 seconds', info1='', info2=''

        Next boot:     ident='BootNext:', is_boot=False, active='',
                      label='0007' (or '---'), info1='', info2=''
    """
    ident: str
    is_boot: bool = False
    active: str = ''
    label: str = ''
    info1: str = ''
    info2: str = ''
    removed: bool = False
    raw_device: str = ''
    pending_copy: bool = False


@dataclass(**_dataclass_kwargs)
class BootModifications:
    """Tracks pending modifications to boot configuration.

    Attributes:
        dirty: True if any changes have been made
        order: True if boot order has been modified
        timeout: New timeout value in seconds, or None if unchanged
        removes: Set of boot entry identifiers to remove
        tags: Dict mapping boot entry identifiers to new labels
        next: Boot entry identifier for next boot, or None if unchanged
        actives: Set of boot entry identifiers to mark as active
        inactives: Set of boot entry identifiers to mark as inactive
        copies: List of (label, raw_device) tuples for new boot entries to create
    """
    dirty: bool = False
    order: bool = False
    timeout: Optional[str] = None
    removes: set = field(default_factory=set)
    tags: dict = field(default_factory=dict)
    next: Optional[str] = None
    actives: set = field(default_factory=set)
    inactives: set = field(default_factory=set)
    copies: list = field(default_factory=list)


class SystemInfo:
    """Gather system information about mounts and partitions.

    Discovers partition UUIDs and maps them to mount points, enabling
    the TUI to display human-readable paths instead of cryptic UUIDs.

    This mapping allows converting efibootmgr output like:
        HD(1,GPT,abc123-def4-5678-90ab-cdef12345678,0x800,0x100000)
    Into user-friendly display:
        /boot/efi or /dev/nvme0n1p1

    Attributes:
        mounts: Dict mapping device paths to mount points (e.g., '/dev/sda1' -> '/boot/efi')
        uuids: Dict mapping partition UUIDs to (device, mount_point) tuples
    """

    def __init__(self):
        self.mounts = self.get_mounts()
        self.uuids = self.get_part_uuids()

    @staticmethod
    def get_mounts():
        """Get a dictionary of device-to-mount-point"""
        mounts = {}
        with open('/proc/mounts', 'r', encoding='utf-8') as mounts_file:
            for line in mounts_file:
                parts = line.split()
                dev = parts[0]
                mount_point = parts[1]
                mounts[dev] = mount_point
        return mounts

    def get_part_uuids(self):
        """Get all partition UUIDs and map them to device paths or mount points.

        This is the key translation layer that makes boot entries readable:
        1. Read symlinks from /dev/disk/by-partuuid/
        2. Resolve each symlink to actual device (e.g., /dev/nvme0n1p1)
        3. If device is mounted, use mount point instead (e.g., /boot/efi)

        Returns:
            Dict mapping UUID strings to either mount points (preferred) or device paths
        """
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
    """Main TUI application for managing UEFI boot entries.

    This class orchestrates the entire application:
    - Parses efibootmgr output into BootEntry objects
    - Manages the curses-based UI with ConsoleWindow
    - Tracks modifications with BootModifications
    - Handles user input and updates state
    - Generates and executes efibootmgr commands

    Key Instance Variables:
        boot_entries: List of BootEntry objects (current state)
        original_entries: Deep copy of boot_entries at load time (for undo detection)
        mods: BootModifications tracking all pending changes
        sysinfo: SystemInfo instance for UUID/mount mapping
        win: ConsoleWindow instance managing the curses UI
        spin: OptionSpinner managing toggle and action keys
        boot_idx: Index of first boot entry in boot_entries list

    Singleton pattern ensures only one instance manages the TUI.
    """
    singleton = None

    def __init__(self, testfile=None):
        # self.cmd_loop = CmdLoop(db=False) # just running as command
        assert not EfiBootDude.singleton
        EfiBootDude.singleton = self
        self.testfile = testfile
        self.redraw = False # force redraw

        spin = self.spin = OptionSpinner()
        spin.add_key('help_mode', '? - toggle help screen', vals=[False, True])
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
        spin.add_key('theme', 'T - cycle theme', vals=[
            'default', 'dark-mono', 'light-mono', 'nord'])
        spin.add_key('reset', 'ESC - reset edits and refresh',
                     genre='action', keys=[27]) # 27=ESC
        spin.add_key('quit', 'q,x - quit program',
                     genre='action', keys=[ord('q'), ord('x')])

        # other = ''
        # other_keys = set(ord(x) for x in other)
        # other_keys.add(cs.KEY_ENTER)
        # other_keys.add(10) # another form of ENTER
        self.opts = spin.default_obj

        self.actions = {} # currently available actions
        self.check_preqreqs()
        self.sysinfo = SystemInfo()
        self.mods = BootModifications()
        self.boot_entries, self.width1, self.label_wid, self.boot_idx = [], 0, 0, 0
        self.saved_pick_pos = None  # Save cursor position when entering help mode

        self.win = None
        self.reinit()
        win_opts = ConsoleWindowOpts()
        win_opts.head_line = True
        win_opts.body_rows = len(self.boot_entries)+20
        win_opts.head_rows = 10
        win_opts.min_cols_rows = (70,10)
        win_opts.mod_pick = self.mod_pick
        win_opts.ctrl_c_terminates = False

        self.win = ConsoleWindow(win_opts)
        self.win.pick_pos = self.boot_idx  # Start at first boot entry
        self.win.set_pick_mode(True)  # Start in pick mode

        # Initialize theme to first in the list (default)
        self._current_theme = self.opts.theme
        Theme.set(self._current_theme)

        # Initialize inline confirmation for text input (Timeout, Tag, Copy)
        self.inline_confirm = InlineConfirmation()
        self._inline_action = None  # Track which action is in progress (e.g., 'timeout', 'tag', 'copy')
        self._inline_context = {}   # Store context data (target entry, etc.)

    def reinit(self):
        """ RESET EVERYTHING"""
        self.sysinfo.refresh()
        self.mods = BootModifications()
        self.boot_entries, self.width1, self.label_wid, self.boot_idx = [], 0, 0, 0
        self.digest_boots()

        # Save original state for detecting actual changes
        self.original_entries = copy.deepcopy(self.boot_entries)

        if self.win:
            self.win.pick_pos = self.boot_idx

    def digest_boots(self):
        """ Digest the output of 'efibootmgr'."""
        # Define the command to run
        lines = []
        if self.testfile:
            # if given a "testfile" (which should be just the
            # raw output of 'efibootmgr'), then parse it
            with open(self.testfile, 'r', encoding='utf-8') as fh:
                lines = fh.readlines()
        else: # run efibootmgr
            command = 'efibootmgr'.split()
            result = subprocess.run(command, stdout=subprocess.PIPE, text=True, check=True)
            lines = result.stdout.splitlines()
        rv = []
        width1 = 0  # width of info1
        label_wid = 0
        boots = {}
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

            # Store device and EFI path for copy operation (if both available)
            # Device could be /boot/efi or /dev/nvme0n1p1
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
        """Recalculate dirty flag based on actual changes from original state.

        Smart dirty detection: Detects when user undoes changes (e.g., moves entry
        up then down, or toggles active twice). Uses deep copy of original_entries
        to compare current state against initial state, not just tracking that
        changes were made.
        """
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
        """Format a boot entry for display, applying verbose/terse filtering.

        Args:
            entry: The BootEntry to format

        Returns:
            Formatted line for display
        """
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

            # Strip PCI device path prefix for USB/network entries
            # Keep just USB(...) or similar instead of PciRoot(...)/Pci(...)/USB(...)
            info1 = re.sub(r'PciRoot\([^)]*\)(?:/Pci\([^)]*\))*/', '', info1)
            info2 = re.sub(r'PciRoot\([^)]*\)(?:/Pci\([^)]*\))*/', '', info2)

        # Display removed entries with -RMV instead of ident
        display_ident = '-RMV' if entry.removed else entry.ident
        line = f'{entry.active:>1} {display_ident:>4} {entry.label:<{self.label_wid}}'
        line += f' {info1:<{self.width1}} {info2}'
        return line

    @staticmethod
    def check_preqreqs():
        """ Check that needed programs are installed. """
        ok = True
        for prog in 'efibootmgr'.split():
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
        ConsoleWindow.stop_curses()
        os.system('clear; stty sane; (set -x; sudo reboot now)')

        # NOTE: probably will not get here...
        os.system(r'/bin/echo -e "\n\n===== Press ENTER for menu ====> \c"; read FOO')
        self.reinit()
        ConsoleWindow.start_curses()
        self.win.pick_pos = self.boot_idx
        return None

    def write(self):
        """ Commit the changes. """
        if not self.mods.dirty:
            return

        cmds = []
        prefix = 'sudo efibootmgr --quiet'
        for ident in self.mods.removes:
            cmds.append(f'{prefix} --delete-bootnum --bootnum {ident}')
        for ident in self.mods.actives:
            cmds.append(f'{prefix} --active --bootnum {ident}')
        for ident in self.mods.inactives:
            cmds.append(f'{prefix} --inactive --bootnum {ident}')
        for ident, tag in self.mods.tags.items():
            cmds.append(f'{prefix} --bootnum {ident} --label "{tag}"')
        for label, raw_device in self.mods.copies:
            # Parse raw_device: "device|efi_path" e.g., "/boot/efi|\EFI\ubuntu\shimx64.efi"
            if '|' in raw_device:
                device_path, efi_path = raw_device.split('|', 1)
                # Extract disk and partition from device path
                # e.g., /dev/nvme0n1p1 -> disk=/dev/nvme0n1, part=1
                # or /boot/efi -> need to find actual device from mounts
                disk, part = None, None
                if device_path.startswith('/dev/'):
                    # Direct device path
                    # Handle both NVMe (/dev/nvme0n1p3) and SATA/SCSI (/dev/sda3)
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
                    cmds.append(f'{prefix} --create --disk {disk} --part {part} --label "{label}" --loader "{efi_path}"')
        if self.mods.order:
            orders = [ns.ident for ns in self.boot_entries if ns.is_boot and not ns.removed]
            orders = ','.join(orders)
            cmds.append(f'{prefix} --bootorder {orders}')
        if self.mods.next:
            if self.mods.next == '---':
                cmds.append(f'{prefix} --delete-bootnext')
            else:
                cmds.append(f'{prefix} --bootnext {self.mods.next}')
        if self.mods.timeout:
            cmds.append(f'{prefix} --timeout {self.mods.timeout}')
        ConsoleWindow.stop_curses()
        os.system('clear; stty sane')
        print('Commands:')
        for cmd in cmds:
            print(f' + {cmd}')
        yes = input("Run the above commands? (yes/No) ")

        if yes.lower().startswith('y'):
            os.system('/bin/echo; /bin/echo')

            for cmd in cmds:
                os.system(f'(set -x; {cmd}); /bin/echo "    <<<ExitCode=$?>>>"')

            os.system(r'/bin/echo -e "\n\n===== Press ENTER for menu ====> \c"; read FOO')
            self.reinit()

        ConsoleWindow.start_curses()
        self.win.pick_pos = self.boot_idx

    def main_loop(self):
        """ TBD """

        while True:
            # Handle transitions into/out of help mode
            if self.opts.help_mode and self.saved_pick_pos is None:
                # Entering help mode - save cursor position and disable pick mode
                self.saved_pick_pos = self.win.pick_pos
                self.win.set_pick_mode(False)
            elif not self.opts.help_mode and self.saved_pick_pos is not None:
                # Exiting help mode - restore cursor position and enable pick mode
                self.win.pick_pos = self.saved_pick_pos
                self.saved_pick_pos = None
                self.win.set_pick_mode(True)

            if self.opts.help_mode:
                self.spin.show_help_nav_keys(self.win)
                self.spin.show_help_body(self.win)
            else:
                # self.win.set_pick_mode(self.opts.pick_mode, self.opts.pick_size)
                self.win.add_header(self.get_keys_line(), attr=cs.A_BOLD)

                # Build display list with pending copies injected before removed entries
                self.display_entries = []
                pending_copies_added = False
                for i, entry in enumerate(self.boot_entries):
                    # Insert pending copies just before first removed entry (or at end if none removed)
                    if not pending_copies_added and self.mods.copies:
                        if (entry.is_boot and entry.removed) or i == len(self.boot_entries) - 1:
                            # Found first removed entry or last entry - insert pending copies before it
                            for label, raw_device in self.mods.copies:
                                copy_entry = BootEntry(
                                    ident='+ADD',
                                    label=label,
                                    info1='[pending copy]',
                                    pending_copy=True,
                                    raw_device=raw_device
                                )
                                self.display_entries.append(copy_entry)
                            pending_copies_added = True

                    self.display_entries.append(entry)

                # Render all entries
                for entry in self.display_entries:
                    line = self.format_boot_entry(entry)
                    attr = None
                    if '+ADD' in line:
                        attr=cs.color_pair(Theme.SUCCESS)
                    elif entry.is_boot and entry.removed:
                        attr=cs.color_pair(Theme.DANGER)
                    elif entry.is_boot and not entry.active:
                        # Dim inactive boot entries
                        attr=cs.A_DIM
                    self.win.add_body(line, attr=attr)

                    # Display inline confirmation prompt right after the relevant entry
                    if self.inline_confirm.active and self._inline_context.get('entry') is entry:
                        hint = self.inline_confirm.get_hint()
                        current_value = self.inline_confirm.input_buffer

                        # Create action-specific prompt text
                        if self._inline_action == 'tag':
                            action_prompt = 'Enter new label for entry'
                        elif self._inline_action == 'copy':
                            action_prompt = 'Enter label for new copy'
                        elif self._inline_action == 'timeout':
                            action_prompt = 'Enter timeout (seconds)'
                        else:
                            action_prompt = self._inline_action.upper() if self._inline_action else 'INPUT'

                        if not current_value and hint:
                            prompt_line = f"  → {hint}"
                            self.win.add_body(prompt_line, attr=cs.color_pair(Theme.WARNING))
                        else:
                            cursor = '_'
                            prompt_line = f"  → {action_prompt}: {current_value}{cursor}"
                            self.win.add_body(prompt_line, attr=cs.color_pair(Theme.DANGER))

            # Update dirty state before rendering to reflect actual changes
            self.update_dirty_state()

            self.win.render(redraw=self.redraw)
            self.redraw = False

            _ = self.do_key(self.win.prompt(seconds=300))
            self.win.clear()

    def get_keys_line(self):
        """ TBD """
        # EXPAND
        line = ''
        for key, verb in self.actions.items():
            if key[0] == verb[0]:
                line += f' {verb}'
            else:
                line += f' {key}:{verb}'
        # or EXPAND
        line += ' v:terse' if self.opts.verbose else ' [v]erbose'
        line += f' [T]heme={self.opts.theme}'
        line += ' ?:help quit'
        # for action in self.actions:
            # line += f' {action[0]}:{action}'
        return line[1:]

    def get_actions(self):
        """ Determine the type of the current line and available commands."""
        actions = {}
        # Use display_entries if available (includes pending copies), otherwise boot_entries
        digests = getattr(self, 'display_entries', self.boot_entries)
        if 0 <= self.win.pick_pos < len(digests):
            boot_entry = digests[self.win.pick_pos]
            if self.mods.dirty:
                actions['w'] = 'wRITE' # unusual case to indicate dirty
            # Pending copy entries: only allow removal
            if boot_entry.pending_copy:
                actions['r'] = 'cancel'
            elif boot_entry.is_boot:
                if not boot_entry.removed:
                    # Non-removed entry: can move up/down but not into removed section
                    if self.win.pick_pos > self.boot_idx:
                        actions['u'] = 'up'
                    # Check if next entry exists and is not removed
                    if (self.win.pick_pos < len(self.boot_entries)-1 and
                        not self.boot_entries[self.win.pick_pos + 1].removed):
                        actions['d'] = 'down'
                    actions['n'] = 'next'
                    actions['t'] = 'tag'
                    actions['*'] = 'inact' if boot_entry.active else 'act'
                    # Copy only available if we have raw device info
                    if boot_entry.raw_device:
                        actions['c'] = 'copy'
                # Removed entries: no up/down, no next, no tag, no toggle active
                actions['r'] = 'unrmv' if boot_entry.removed else 'rmv'
#               actions['a'] = 'add'
            elif boot_entry.ident == 'BootNext:':
                actions['n'] = 'cycle'
            elif boot_entry.ident in ('Timeout:', ):
                actions['m'] = 'modify'
            if not self.mods.dirty:
                actions['b'] = 'boot'

        return actions

    @staticmethod
    def mod_pick(line):
        """ Callback to modify the "pick line" being highlighted;
            We use it to alter the state
        """
        this = EfiBootDude.singleton
        this.actions = this.get_actions()
        header = this.get_keys_line()
        wds = header.split()
        this.win.head.pad.move(0, 0)
        for wd in wds:
            if wd:
                this.win.add_header(wd[0], attr=cs.A_BOLD|cs.A_UNDERLINE, resume=True)
            if wd[1:]:
                this.win.add_header(wd[1:] + ' ', resume=True)

        _, col = this.win.head.pad.getyx()
        pad = ' ' * (this.win.get_pad_width()-col)
        this.win.add_header(pad, resume=True)
        return line

    def do_key(self, key):
        """ TBD """
        if not key:
            return True
        self.redraw = True # any key redraws/fixes screen

        # Check if inline confirmation is active
        if self.inline_confirm.active:
            result = self.inline_confirm.handle_key(key)
            if result == 'confirmed':
                self._process_inline_confirmation()
            elif result == 'cancelled':
                self._inline_action = None
                self._inline_context = {}
                self.inline_confirm.cancel()

            # Keep cursor on the entry that was being edited
            if result in ('confirmed', 'cancelled'):
                entry_to_stay_on = self._inline_context.get('entry')
                if entry_to_stay_on and hasattr(self, 'display_entries'):
                    for i, e in enumerate(self.display_entries):
                        if e is entry_to_stay_on:
                            self.win.pick_pos = i
                            break
                self.win.set_pick_mode(True)  # Re-enable pick mode
            return None

        if key == cs.KEY_ENTER or key == 10: # Handle ENTER
            if self.opts.help_mode:
                self.opts.help_mode = False
                return True
            return None

        if key in self.spin.keys:
            value = self.spin.do_key(key, self.win)
            self.do_actions()
            return value

        return None

    def do_actions(self):
        """ Handle keys that are genre='action' """

        do_quit, self.opts.quit = self.opts.quit, False
        if do_quit:
            answer = 'y'
            if self.mods.dirty:
                answer = self.win.answer(
                    prompt='Enter "y" to abandon edits and exit')
            if answer and answer.strip().lower().startswith('y'):
                self.win.stop_curses()
                os.system('clear; stty sane')
                sys.exit(0)

        reset, self.opts.reset = self.opts.reset, False
        if reset:  # ESC
            if self.mods.dirty:
                answer = self.win.answer(
                    prompt='Type "y" to clear edits and refresh')
                if answer and answer.strip().lower().startswith('y'):
                    self.reinit()
            else:
                self.reinit()
            return None

        write, self.opts.write = self.opts.write, False
        if write and self.mods.dirty:
            self.write()

        boot, self.opts.boot = self.opts.boot, False
        if boot:
            if self.mods.dirty:
                self.win.alert('Pending changes (on return, use "w" to commit or "ESC" to discard)')
                return None

            answer = self.win.answer(prompt='Type "reboot" to reboot',
                    seed='reboot', width=80)
            if answer and answer.strip().lower().startswith('reboot'):
                return self.reboot()

        # Check if theme was changed (cycled) by OptionSpinner
        new_theme = self.opts.theme if self.opts.theme else 'default'
        if new_theme != self._current_theme:
            Theme.set(new_theme)
            self._current_theme = new_theme
            self.redraw = True

        # Use display_entries if available (includes pending copies)
        digests = getattr(self, 'display_entries', self.boot_entries)
        if self.win.pick_pos >= len(digests):
            return None
        boot_entry = digests[self.win.pick_pos]

        up, self.opts.up = self.opts.up, False
        if up and boot_entry.is_boot and not boot_entry.removed:
            digests, pos = self.boot_entries, self.win.pick_pos
            # Don't move past the first boot entry or into a removed entry
            if pos > self.boot_idx and not digests[pos-1].removed:
                digests[pos-1], digests[pos] = digests[pos], digests[pos-1]
                self.win.pick_pos -= 1
                self.mods.order = True

        down, self.opts.down = self.opts.down, False
        if down and boot_entry.is_boot and not boot_entry.removed:
            digests, pos = self.boot_entries, self.win.pick_pos
            # Don't move past end or into a removed entry
            if pos < len(self.boot_entries)-1 and not digests[pos+1].removed:
                digests[pos+1], digests[pos] = digests[pos], digests[pos+1]
                self.win.pick_pos += 1
                self.mods.order = True

        boot_next, self.opts.next = self.opts.next, False
        if boot_next:
            if boot_entry.ident == 'BootNext:':
                # Cycle through non-removed boot entries + '---' (default) when on BootNext line
                boot_entries = [b.ident for b in self.boot_entries if b.is_boot and not b.removed]
                if not boot_entries:
                    return None

                # Create full cycle: all boot entries + '---' (default)
                cycle = boot_entries + ['---']
                current = boot_entry.label

                if current in cycle:
                    # Find current and move to next (wrapping at end)
                    idx = cycle.index(current)
                    next_ident = cycle[(idx + 1) % len(cycle)]
                else:
                    # Unknown state, start with first boot entry
                    next_ident = cycle[0]

                boot_entry.label = next_ident
                # Get original value to detect if we're cycling back to it
                original_next = next((ns.label for ns in self.original_entries if ns.ident == 'BootNext:'), None)
                # Only record change if different from original
                self.mods.next = next_ident if next_ident != original_next else None
                return None

            elif boot_entry.is_boot:
                # Set this boot entry as next
                ident = boot_entry.ident
                self.boot_entries[0].label = ident
                self.mods.next = ident
                return None

        star, self.opts.star = self.opts.star, False
        if star and boot_entry.is_boot:
            ident = boot_entry.ident
            if boot_entry.active:
                boot_entry.active = ''
                self.mods.actives.discard(ident)
                self.mods.inactives.add(ident)
            else:
                boot_entry.active = '*'
                self.mods.actives.add(ident)
                self.mods.inactives.discard(ident)

        remove, self.opts.remove = self.opts.remove, False
        # Handle removal of pending copy entries
        if remove and boot_entry.pending_copy:
            # Remove from copies list by matching label and raw_device
            for i, (label, raw_device) in enumerate(self.mods.copies):
                if label == boot_entry.label and raw_device == boot_entry.raw_device:
                    self.mods.copies.pop(i)
                    break
            return None

        if remove and boot_entry.is_boot:
            # Undoable remove feature: Instead of deleting immediately, mark as removed
            # and move to bottom. Entry shows as "-RMV" and can be restored with 'r'.
            # This prevents accidental deletion without needing to ESC (losing all changes).

            # Find the actual position in boot_entries (pick_pos is for display_entries)
            boot_pos = None
            for i, entry in enumerate(self.boot_entries):
                if entry is boot_entry:
                    boot_pos = i
                    break

            if boot_pos is None:
                return None  # Entry not found in boot_entries

            if boot_entry.removed:
                # Un-remove: restore entry just before first removed entry (or at end)
                boot_entry.removed = False
                self.mods.removes.discard(boot_entry.ident)

                # Find insertion point: first removed boot entry, or end of list
                insert_pos = len(self.boot_entries)
                for i in range(self.boot_idx, len(self.boot_entries)):
                    if self.boot_entries[i].is_boot and self.boot_entries[i].removed and i != boot_pos:
                        insert_pos = i
                        break

                # Move entry to insertion point
                entry = self.boot_entries.pop(boot_pos)
                # Adjust insert_pos if we removed an item before it
                if boot_pos < insert_pos:
                    insert_pos -= 1
                self.boot_entries.insert(insert_pos, entry)
                self.win.pick_pos = insert_pos
                self.mods.order = True
            else:
                # Mark for removal and move to bottom
                boot_entry.removed = True
                self.mods.removes.add(boot_entry.ident)
                self.mods.actives.discard(boot_entry.ident)
                self.mods.inactives.discard(boot_entry.ident)

                # Auto-fix BootNext if it points to the removed entry
                bootnext_entry = self.boot_entries[0]
                if bootnext_entry.ident == 'BootNext:' and bootnext_entry.label == boot_entry.ident:
                    bootnext_entry.label = '---'
                    self.mods.next = None

                # Move to bottom of list
                self.boot_entries.append(self.boot_entries.pop(boot_pos))
                # Keep cursor at same position (next entry moves up)
                self.mods.order = True

            return None

        copy_entry, self.opts.copy = self.opts.copy, False
        if copy_entry and boot_entry.is_boot and boot_entry.raw_device:
            seed = boot_entry.label
            self.inline_confirm.start(action_type='copy', mode='text')
            self.inline_confirm.input_buffer = seed
            self._inline_action = 'copy'
            self._inline_context = {'entry': boot_entry, 'original_label': boot_entry.label}
            self.win.set_pick_mode(False)  # Disable pick mode during input
        elif copy_entry:
            # Debug why copy failed
            msg = f"Copy blocked: is_boot={boot_entry.is_boot}, raw_device={'empty' if not boot_entry.raw_device else 'set'}"
            self.win.alert(msg)

        tag, self.opts.tag = self.opts.tag, False
        if tag and boot_entry.is_boot:
            seed = boot_entry.label
            self.inline_confirm.start(action_type='tag', mode='text')
            self.inline_confirm.input_buffer = seed
            self._inline_action = 'tag'
            self._inline_context = {'entry': boot_entry}
            self.win.set_pick_mode(False)  # Disable pick mode during input

        modify, self.opts.modify = self.opts.modify, False
        if modify and boot_entry.ident == 'Timeout:':
            # Only allow modify on the Timeout line
            seed = boot_entry.label.split()[0]
            self.inline_confirm.start(action_type='timeout', mode='text')
            self.inline_confirm.input_buffer = seed
            self._inline_action = 'timeout'
            self._inline_context = {'entry': boot_entry}
            self.win.set_pick_mode(False)  # Disable pick mode during input

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


def main():
    """ The program """
    parser = argparse.ArgumentParser()
    parser.add_argument('testfile', nargs='?', default=None)
    opts = parser.parse_args()

    dude = EfiBootDude(testfile=opts.testfile)
    dude.main_loop()

if __name__ == '__main__':

    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exce:
        ConsoleWindow.stop_curses()
        print("exception:", str(exce))
        print(traceback.format_exc())
#       if dump_str:
#           print(dump_str)
