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
from types import SimpleNamespace
import subprocess
import traceback
import curses as cs
import xml.etree.ElementTree as ET
from efibootdude.PowerWindow import Window, OptionSpinner


class EfiBootDude:
    """ Main class for curses atop efibootmgr"""
    singleton = None

    def __init__(self, testfile=None):
        # self.cmd_loop = CmdLoop(db=False) # just running as command
        assert not EfiBootDude.singleton
        EfiBootDude.singleton = self
        self.testfile = testfile

        spin = self.spin = OptionSpinner()
        spin.add_key('help_mode', '? - toggle help screen', vals=[False, True])
        spin.add_key('verbose', 'v - toggle verbose', vals=[False, True])

        # FIXME: keys
        other = 'tudrnmwf*zqx'
        other_keys = set(ord(x) for x in other)
        other_keys.add(cs.KEY_ENTER)
        # other_keys.add(27) # ESCAPE
        other_keys.add(10) # another form of ENTER
        self.opts = spin.default_obj

        self.actions = {} # currently available actions
        self.check_preqreqs()
        self.mounts, self.uuids = {}, {}
        self.mods = SimpleNamespace()
        self.digests, self.width1, self.label_wid, self.boot_idx = [], 0, 0, 0
        self.win = None
        self.reinit()
        self.win = Window(head_line=True, body_rows=len(self.digests)+20, head_rows=10,
                          keys=spin.keys ^ other_keys, mod_pick=self.mod_pick)
        self.win.pick_pos = self.boot_idx

    def reinit(self):
        """ RESET EVERYTHING"""
        self.mounts = self.get_mounts()
        self.uuids = self.get_part_uuids() # uuid in lower case
        self.mods = SimpleNamespace(
                    dirty=False, # if anything changed
                    order=False,
                    timeout=None,
                    removes=set(),
                    tags={},
#                   adds=set(),
                    next=None,
                    actives=set(),
                    inactives=set(),
                    )
        self.digests, self.width1, self.label_wid, self.boot_idx = [], 0, 0, 0
        self.digest_boots()
        if self.win:
            self.win.pick_pos = self.boot_idx

    @staticmethod
    def get_mounts():
        """ Get a dictionary of device-to-mount-point """
        mounts = {}
        with open('/proc/mounts', 'r', encoding='utf-8') as mounts_file:
            for line in mounts_file:
                parts = line.split()
                dev = parts[0]
                mount_point = parts[1]
                mounts[dev] = mount_point
        return mounts

    def get_part_uuids(self):
        """ Get all the Partition UUIDS"""
        uuids = {}
        with open('/run/blkid/blkid.tab', encoding='utf8') as fh:
            # sample: <device ... TYPE="vfat"
            #   PARTUUID="25d2dea1-9f68-1644-91dd-4836c0b3a30a">/dev/nvme0n1p1</device>
            for xml_line in fh:
                element = ET.fromstring(xml_line)
                if 'PARTUUID' in element.attrib:
                    device=element.text.strip()
                    name = self.mounts.get(device, device)
                    uuids[element.attrib['PARTUUID'].lower()] = name
        return uuids

    @staticmethod
    def extract_uuid(line):
        """ Find uuid string in a line """
        # Define the regex pattern for UUID (e.g., 25d2dea1-9f68-1644-91dd-4836c0b3a30a)
        pattern = r'\b[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\b'
        # Search for the pattern in the line
        match = re.search(pattern, line, re.IGNORECASE)
        return match.group(0).lower() if match else None


    def digest_boots(self):
        """ Digest the output of 'efibootmgr'."""
        # Define the command to run
        lines = []
        if self.testfile:
            with open(self.testfile, 'r', encoding='utf-8') as fh:
                lines = fh.readlines()
        else:
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

            ns = SimpleNamespace(
                ident=None,
                is_boot=False,
                active='',
                label='',
                info1='',
                info2='',
            )

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

            mat = re.search(r'/?File\(([^)]*)\)', other, re.IGNORECASE)
            device, subpath = '', '' # e.g., /boot/efi, \EFI\UBUNTU\SHIMX64.EFI
            if mat:
                subpath = mat.group(1) + ' '
                start, end = mat.span()
                other = other[:start] + other[end:]

            uuid = self.extract_uuid(other)
            if uuid and uuid in self.uuids:
                device = self.uuids[uuid]

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

        self.digests = rv
        return rv

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
            orders = [ns.ident for ns in self.digests if ns.is_boot]
            cmds.append(f'{prefix} --bootorder {','.join(orders)}')
        if self.mods.next:
            cmds.append(f'{prefix} --bootnext {self.mods.next}')
        if self.mods.timeout:
            cmds.append(f'{prefix} --timeout {self.mods.timeout}')
        Window.stop_curses()
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

        Window._start_curses()
        self.win.pick_pos = self.boot_idx



    def main_loop(self):
        """ TBD """

        self.opts.name = "[hit 'n' to enter name]"
        while True:
            if self.opts.help_mode:
                self.win.set_pick_mode(False)
                self.spin.show_help_nav_keys(self.win)
                self.spin.show_help_body(self.win)
                # FIXME: keys
                lines = [
                    '   q or x - quit program (CTL-C disabled)',
                    '   u - up - move boot entry up',
                    '   d - down - move boot entry down',
#                   '   c - copy - copy boot entry',
                    '   r - remove - remove boot',
#                   '   a - add - add boot entry',
                    '   n - next - set next boot default',
                    '   t - tag - set a new label for the boot entry',
                    '   * - toggle whether entry is active'
                    '   m - modify - modify the value'
                    '   w - write - write the changes',
                    '   f - freshen - clear changes and re-read boot state',
                ]
                for line in lines:
                    self.win.put_body(line)
            else:
                # self.win.set_pick_mode(self.opts.pick_mode, self.opts.pick_size)
                self.win.set_pick_mode(True)
                self.win.add_header(self.get_keys_line(), attr=cs.A_BOLD)
                for ns in self.digests:
                    info1 = ns.info1
                    if not self.opts.verbose:
                        mat = re.search(r'/?VenHw\(.*$', info1, re.IGNORECASE)
                        if mat:
                            start, end = mat.span()
                            info1 = info1[:start] + info1[end:]

                    line = f'{ns.active:>1} {ns.ident:>4} {ns.label:<{self.label_wid}}'
                    line += f' {info1:<{self.width1}} {ns.info2}'
                    self.win.add_body(line)
            self.win.render()

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
        line += ' ?:help quit'
        # for action in self.actions:
            # line += f' {action[0]}:{action}'
        return line[1:]

    def get_actions(self):
        """ Determine the type of the current line and available commands."""
        # FIXME: keys
        actions = {}
        digests = self.digests
        if 0 <= self.win.pick_pos < len(digests):
            ns = digests[self.win.pick_pos]
            if ns.is_boot:
                if self.win.pick_pos > self.boot_idx:
                    actions['u'] = 'up'
                if self.win.pick_pos < len(self.digests)-1:
                    actions['d'] = 'down'
#               actions['c'] = 'copy'
                actions['r'] = 'rmv'
#               actions['a'] = 'add'
                actions['n'] = 'next'
                actions['t'] = 'tag'
                actions['*'] = 'inact' if ns.active else 'act'
            elif ns.ident in ('Timeout:', ):
                actions['m'] = 'modify'
            if self.mods.dirty:
                actions['w'] = 'write'
                actions['f'] = 'fresh'

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
        if key == cs.KEY_ENTER or key == 10: # Handle ENTER
            if self.opts.help_mode:
                self.opts.help_mode = False
                return True
            return None

        if key in self.spin.keys:
            value = self.spin.do_key(key, self.win)
            return value

        if key in (ord('q'), ord('x')):
            
            answer = 'y'
            if self.mods.dirty:
                answer = self.win.answer(
                    prompt='Enter "y" to abandon edits and exit')
            if answer.strip().lower().startswith('y'):
                self.win.stop_curses()
                os.system('clear; stty sane')
                sys.exit(0)
            return None

        ns = self.digests[self.win.pick_pos]

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
                        self.mods.dirty = True
                        break
            return None

        if key == ord('u') and ns.is_boot:
            digests, pos = self.digests, self.win.pick_pos
            if pos > self.boot_idx:
                digests[pos-1], digests[pos] = digests[pos], digests[pos-1]
                self.win.pick_pos -= 1
                self.mods.order = True
                self.mods.dirty = True
            return None
        if key == ord('d') and ns.is_boot:
            digests, pos = self.digests, self.win.pick_pos
            if pos < len(self.digests)-1:
                digests[pos+1], digests[pos] = digests[pos], digests[pos+1]
                self.win.pick_pos += 1
                self.mods.order = True
                self.mods.dirty = True
            return None
        if key == ord('r') and ns.is_boot:
            ident = self.digests[self.win.pick_pos].ident
            del self.digests[self.win.pick_pos]
            self.mods.removes.add(ident)
            self.mods.actives.discard(ident)
            self.mods.inactives.discard(ident)
            self.mods.dirty = True
            return None
        if key == ord('n') and ns.is_boot:
            ident = ns.ident
            self.digests[0].label = ident
            self.mods.next = ident
            self.mods.dirty = True
            return None

        if key == ord('*') and ns.is_boot:
            ident = ns.ident
            if ns.active:
                ns.active = ''
                self.mods.actives.discard(ident)
                self.mods.inactives.add(ident)
            else:
                ns.active = '*'
                self.mods.actives.add(ident)
                self.mods.inactives.discard(ident)
            self.mods.dirty = True

        if key == ord('t') and ns.is_boot:
            seed = ns.label
            while True:
                answer = self.win.answer(prompt='Enter new label or clear to abort',
                    seed=seed, width=80)
                seed = answer = answer.strip()
                if not answer:
                    break
                if re.match(r'([\w\s])+$', answer):
                    ns.label = f'{answer}'
                    self.mods.tags[ns.ident] = answer
                    self.mods.dirty = True
                    break

        if key == ord('f') and self.mods.dirty:
            answer = self.win.answer(
                prompt='Enter "y" to clear edits and refresh')
            if answer.strip().lower().startswith('y'):
                self.reinit()
            return None

        if key == ord('w') and self.mods.dirty:
            self.write()
            return None

        # FIXME: handle more keys 
        return None


def main():
    """ The program """
    import argparse
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
        Window.stop_curses()
        print("exception:", str(exce))
        print(traceback.format_exc())
#       if dump_str:
#           print(dump_str)
