#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interactive, visual thin layer atop efibootmgr
"""
# pylint: disable=broad-exception-caught,consider-using-with
# pylint: disable=too-many-instance-attributes,too-many-branches
# pylint: disable=too-many-return-statements,too-many-statements
# pylint: disable=consider-using-in,too-many-nested-blocks
# pylint: disable=wrong-import-position,disable=wrong-import-order
# pylint: disable=too-many-locals

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
from console_window import ConsoleWindow, OptionSpinner

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
    """
    dirty: bool = False
    order: bool = False
    timeout: Optional[str] = None
    removes: set = field(default_factory=set)
    tags: dict = field(default_factory=dict)
    next: Optional[str] = None
    actives: set = field(default_factory=set)
    inactives: set = field(default_factory=set)


class SystemInfo:
    """Gather system information about mounts and partitions"""

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
        """Get all the Partition UUIDs"""
        uuids = {}
        partuuid_path = '/dev/disk/by-partuuid/'

        if not os.path.exists(partuuid_path):
            return uuids
        for entry in os.listdir(partuuid_path):
            full_path = os.path.join(partuuid_path, entry)
            if os.path.islink(full_path):
                device_path = os.path.realpath(full_path)
                uuids[entry] = device_path
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
    """ Main class for curses atop efibootmgr"""
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
        spin.add_key('up', 'u - move boot entry up', category='action')
        spin.add_key('down', 'd - move boot entry down', category='action')
        spin.add_key('remove', 'r - remove boot entry', category='action')
        spin.add_key('next', 'n - set next boot to boot entry OR cycle its values', category='action')
        spin.add_key('star', '* - toggle whether entry is active', category='action')
        spin.add_key('tag', 't - set a new label for the boot entry', category='action')
        spin.add_key('modify', 'm - modify the value (on Timeout line)', category='action')
        spin.add_key('write', 'w - write changes', category='action')
        spin.add_key('boot', 'b - reboot the machine', category='action')
        spin.add_key('reset', 'ESC - reset edits and refresh',
                     category='action', keys=[27]) # 27=ESC
        spin.add_key('quit', 'q,x - quit program',
                     category='action', keys=[ord('q'), ord('x')])

        other = ''
        other_keys = set(ord(x) for x in other)
        other_keys.add(cs.KEY_ENTER)
        other_keys.add(10) # another form of ENTER
        self.opts = spin.default_obj

        self.actions = {} # currently available actions
        self.check_preqreqs()
        self.sysinfo = SystemInfo()
        self.mods = BootModifications()
        self.boot_entries, self.width1, self.label_wid, self.boot_idx = [], 0, 0, 0
        self.saved_pick_pos = None  # Save cursor position when entering help mode
        self.win = None
        self.reinit()
        self.win = ConsoleWindow(head_line=True, body_rows=len(self.boot_entries)+20, head_rows=10,
                          keys=spin.keys ^ other_keys, mod_pick=self.mod_pick)
        self.win.pick_pos = self.boot_idx  # Start at first boot entry
        self.win.set_pick_mode(True)  # Start in pick mode

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

            pat = r'(?:/?\b\w*\(|/)(\\[^/()]+)(?:$|[()/])'
            mat = re.search(pat, other, re.IGNORECASE)
            device, subpath = '', '' # e.g., /boot/efi, \EFI\UBUNTU\SHIMX64.EFI
            if mat:
                subpath = mat.group(1) + ' '
                start, end = mat.span()
                other = other[:start] + other[end:]

            uuids = SystemInfo.extract_uuids(other)
            for uuid in uuids:
                if uuid and uuid in self.sysinfo.uuids:
                    device = self.sysinfo.uuids[uuid]
                    break

            if device:
                ns.info1 = device
                ns.info2 = subpath if subpath else other
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
        """Recalculate dirty flag based on actual changes from original state"""
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

        # Check removals and tag changes
        removals_changed = bool(self.mods.removes)
        tags_changed = bool(self.mods.tags)

        # Update dirty flag
        self.mods.dirty = (
            order_changed or
            actives_changed or
            timeout_changed or
            next_changed or
            removals_changed or
            tags_changed
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
        ConsoleWindow._start_curses()
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
        if self.mods.order:
            orders = [ns.ident for ns in self.boot_entries if ns.is_boot and not ns.removed]
            orders = ','.join(orders)
            cmds.append(f'{prefix} --bootorder {orders}')
        if self.mods.next:
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

        ConsoleWindow._start_curses()
        self.win.pick_pos = self.boot_idx

    def main_loop(self):
        """ TBD """

        self.opts.name = "[hit 'n' to enter name]"
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
                lines = [
                    '   q or x - quit program (CTL-C disabled)',
#                   '   c - copy - copy boot entry',
#                   '   a - add - add boot entry',

                ]
                for line in lines:
                    self.win.put_body(line)
            else:
                # self.win.set_pick_mode(self.opts.pick_mode, self.opts.pick_size)
                pass  # pick mode already set in transition logic above
                self.win.add_header(self.get_keys_line(), attr=cs.A_BOLD)
                for entry in self.boot_entries:
                    line = self.format_boot_entry(entry)
                    self.win.add_body(line)

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
        line += ' v:' + ('terse' if self.opts.verbose else 'verbose')
        line += ' ?:help quit'
        # for action in self.actions:
            # line += f' {action[0]}:{action}'
        return line[1:]

    def get_actions(self):
        """ Determine the type of the current line and available commands."""
        # FIXME: keys
        actions = {}
        digests = self.boot_entries
        if 0 <= self.win.pick_pos < len(digests):
            boot_entry = digests[self.win.pick_pos]
            if self.mods.dirty:
                actions['w'] = 'wRITE' # unusual case to indicate dirty
            if boot_entry.is_boot:
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
                # Removed entries: no up/down, no next, no tag, no toggle active
#               actions['c'] = 'copy'
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
        if key == cs.KEY_ENTER or key == 10: # Handle ENTER
            if self.opts.help_mode:
                self.opts.help_mode = False
                return True
            return None

        if key in self.spin.keys:
            value = self.spin.do_key(key, self.win)
            self.do_actions()
            return value


        ns = self.boot_entries[self.win.pick_pos]

        if key == ord('m'):
            if ns.ident == 'Timeout:':
                seed = ns.label.split()[0]
                while True:
                    answer = self.win.answer(
                        prompt='Enter timeout seconds or clear to abort',
                        seed=seed, width=80)
                    seed = answer = answer.strip()
                    if not answer:
                        break
                    if re.match(r'\d+$', answer):
                        ns.label = f'{answer} seconds'
                        self.mods.timeout = answer
                        break
            return None


        return None

    def do_actions(self):
        """ Handle keys that are category='action' """

        quit, self.opts.quit = self.opts.quit, False
        if quit:
            answer = 'y'
            if self.mods.dirty:
                answer = self.win.answer(
                    prompt='Enter "y" to abandon edits and exit')
            if answer.strip().lower().startswith('y'):
                self.win.stop_curses()
                os.system('clear; stty sane')
                sys.exit(0)

        reset, self.opts.reset = self.opts.reset, False
        if reset:  # ESC
            if self.mods.dirty:
                answer = self.win.answer(
                    prompt='Type "y" to clear edits and refresh')
                if answer.strip().lower().startswith('y'):
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
            if answer.strip().lower().startswith('reboot'):
                return self.reboot()

        boot_entry = self.boot_entries[self.win.pick_pos]

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
                # Get original BootNext value
                original_next = next((ns.label for ns in self.original_entries if ns.ident == 'BootNext:'), None)

                # Cycle through boot entries when on BootNext line
                boot_entries = [b.ident for b in self.boot_entries if b.is_boot]
                if not boot_entries:
                    return None

                current = boot_entry.label
                # Determine next entry to cycle to
                if current == original_next or current == '---':
                    # Start with first boot entry
                    next_ident = boot_entries[0]
                elif current in boot_entries:
                    # Find current in list and move to next (or wrap to original)
                    idx = boot_entries.index(current)
                    if idx + 1 < len(boot_entries):
                        next_ident = boot_entries[idx + 1]
                    else:
                        # Wrap back to original
                        next_ident = original_next
                else:
                    # Unknown state, start over
                    next_ident = boot_entries[0]

                boot_entry.label = next_ident
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
        if remove and boot_entry.is_boot:
            if boot_entry.removed:
                # Un-remove: restore entry just before first removed entry (or at end)
                boot_entry.removed = False
                self.mods.removes.discard(boot_entry.ident)

                # Find insertion point: first removed boot entry, or end of list
                insert_pos = len(self.boot_entries)
                for i in range(self.boot_idx, len(self.boot_entries)):
                    if self.boot_entries[i].is_boot and self.boot_entries[i].removed and i != self.win.pick_pos:
                        insert_pos = i
                        break

                # Move entry to insertion point
                entry = self.boot_entries.pop(self.win.pick_pos)
                # Adjust insert_pos if we removed an item before it
                if self.win.pick_pos < insert_pos:
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

                # Move to bottom of list
                self.boot_entries.append(self.boot_entries.pop(self.win.pick_pos))
                # Keep cursor at same position (next entry moves up)
                self.mods.order = True

            return None

        tag, self.opts.tag = self.opts.tag, False
        if tag and boot_entry.is_boot:
            seed = boot_entry.label
            while True:
                answer = self.win.answer(prompt='Type new label or clear to abort',
                    seed=seed, width=80)
                seed = answer = answer.strip()
                if not answer:
                    break
                if re.match(r'([\w\s])+$', answer):
                    boot_entry.label = f'{answer}'
                    self.mods.tags[boot_entry.ident] = answer
                    break


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
