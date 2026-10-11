# EfiBootDude
`efibootdude` presents a visual (curses) interface to `efibootmgr` which allows editing the bios boot menu and parameters while running Linux.

* **Installation**: `pipx install efibootdude`
* **Prerequisites**:
  * Python **3.11 or newer**.
  * Install [rhboot/efibootmgr](https://github.com/rhboot/efibootmgr)
    * For example, on a Debian derived distro, use `sudo apt install efibootmgr`.

![Demo of efibootdude](https://raw.githubusercontent.com/joedefen/efibootdude/main/images/efibootdude-2026-01-24=16-37.gif)

`efibootdude` covers the most commonly used capabilities of `efibootmgr`; you can:

* reorder, enable/disable, copy, and remove boot entries
* choose what to boot on the next reboot (BootNext)
* set the timeout before auto-booting
* save your changes and reboot

Note: Copying a boot entry effectively adds a new entry with a different label. For more esoteric uses of `efibootmgr` (like setting custom boot variables), use `efibootmgr` directly.

## Usage

> **Essentials before you start**
> * **Do not run `efibootdude` as root** (i.e. do not use `sudo efibootdude`); run it as your normal user.
> * `efibootdude` elevates itself: the first time it needs to write (`w`) or reboot (`b`) it **prompts for your `sudo` password**.
> * Nothing is written until you press `w` and confirm at the prompt; press `?` at any time for help.
>
> See [Privileges: `efibootdude` elevates to root automatically](#privileges-efibootdude-elevates-to-root-automatically) below for the full story and safety notes.

After running `efibootdude` and making some changes, you'll see a screen comparable to this:
```
 wRITE down next tag *:inact copy rmv [v]erbose [T]heme ?:help quit
──────────────────────────────────────────────────────────────────────────────
   BootNext: ---
   BootCurrent: 0007
   Timeout: 2 seconds
>* 0006 Linux Boot Manager               /dev/nvme0n1p1 \EFI\systemd\systemd-b
 * 0000 Enter Setup                      [Firmware]
 * 0001 NVMe: SAMSUNG MZALQ128HBHQ-000L2 [Auto]
 * 0002 UEFI Shell                       [Firmware]
   +ADD Linux Boot Manager Copy          [pending copy]
 * -RMV Ubuntu                           /boot/efi      \EFI\ubuntu\shimx64.ef

```
At this point
* The "current" line starts with `>` and is highlighted.
* The top line shows actions for the current line; type the underscored letter to effect its action.
* Type `?` for a more complete explanation of the keys, navigation keys, etc.
  * **ALWAYS** view the help at least once if unfamiliar with this tool,
    its navigation, and/or uncertain of keys not shown on top line.
* With this current line, we can:
  * Type `u` or `d` to move it up or down in the boot order.
  * Type `t` to relabel the boot entry (prompts for new name).
  * Type `c` to copy the boot entry with a new label (prompts for new name).
  * Type `r` to remove the boot entry.
  * Type `m` on the Timeout line to change the boot timeout in seconds.
  * Type `T` to cycle through available themes.
  * And so forth.
* The entries with `*` on the left are active boot entries; toggle whether
  active by typing `*` for the corresponding entries.
* Special status lines at the top:
  * `BootNext:` shows which entry will boot next (use `n` to set it, or leave as `---`)
  * `BootCurrent:` shows which entry is currently booted (read-only, set by firmware)
  * `Timeout:` shows boot timeout in seconds (use `m` to modify)
* Special indicators:
  * `+ADD` shows pending copy operations (press `r` to cancel before writing)
  * `-RMV` shows entries marked for removal (press `r` to undo before writing)
* Press `ESC` key to abandon any changes and reload the boot information.
* When ready to write the changes to the BIOS, enter `w`.
* When writing the changes, `efibootdude` drops out of menu mode so you can verify the underlying commands, error codes, and error messages.
* After you write changes, type `b` to reboot, if you wish and the boot menu looks OK.
* BTW, the top-line keys vary per context; e.g.:
  * `w` is only shown with pending changes, and
  * `b` is only shown w/o pending changes.

## Caveats - Firmware Bugs and efibootmgr Quirks
* Some operations may not work permanently even though there is no indication from `efibootmgr` (e.g., on my desktop, I cannot re-label boot entries); copy an entry and later remove the old one as a safe workaround.
* Some operations may only work (again) after re-booting (e.g., you might find activating an entry does not work, but it does so after a reboot).
* `efibootmgr` v18 cannot address `Boot0000` by number: `--bootnum 0000` fails with `Boot entry 0 not found` (other numbers work), even though the entry exists and lists fine. Work around it by changing the boot order (`--bootorder`) instead of the entry's active flag, or delete it from your firmware setup screen.

## Privileges: `efibootdude` elevates to root automatically

`efibootdude` is **designed to be launched as your normal, unprivileged user**.
Reading and writing the UEFI boot variables, however, requires root. Rather than
making you launch the whole application with `sudo`, `efibootdude` **elevates
automatically**: each privileged action it performs — writing changes with
`efibootmgr` and rebooting — is run through `sudo` on your behalf. In practice,
when you press `w` (write) or `b` (reboot), `efibootdude` **becomes root for you**
without you typing `sudo` yourself. Only the individual `efibootmgr`/`reboot`
commands are elevated; the TUI itself keeps running as your normal user.

* The first time a privileged command runs you may be prompted for your password
  (unless your `sudo` credentials are already cached). Because `efibootdude`
  briefly leaves curses mode before running the commands, the `sudo` prompt and
  the resulting command output and exit codes are shown plainly on the terminal.
* Tip: run `sudo -v` in another terminal first if you want to pre-authorize
  before starting `efibootdude` and avoid an in-app prompt.
* **Do not** launch the whole program as root (`sudo efibootdude`). It works, but
  it is unnecessary and needlessly widens the blast radius — let `efibootdude`
  elevate only what it needs.

### Doing it safely

Elevating only the individual commands (rather than the whole TUI) is the safer
design, but you should still understand what you are handing over:

* **Root is blanket authority.** Every generated command runs as root. Before
  anything is executed, `efibootdude` prints the exact command list and asks
  `Run the above commands? (yes/No)`. **Read that list before answering `yes`** —
  it is your last chance to catch a mistyped label or the wrong partition.
* **Values are passed to `sudo efibootmgr` as separated arguments, never through
  a shell.** Labels and device paths cannot break out into additional commands,
  so a crafted label cannot run arbitrary root commands.
* **Prefer scoping `sudo` to the two binaries.** If you want to skip password
  prompts, grant passwordless `sudo` for just `efibootmgr` and `reboot` instead
  of for `efibootdude` as a whole. With `sudo visudo`, add a line such as:
  ```
  youruser ALL=(root) NOPASSWD: /usr/sbin/efibootmgr, /sbin/reboot
  ```
  (Paths vary by distro; confirm them with `command -v efibootmgr` and
  `command -v reboot`.) This is safer than `NOPASSWD: ALL` and safer than
  whitelisting the TUI wrapper.
* **Boot-menu changes are firmware-level and persistent.** A bad write can leave
  a machine unbootable. Keep a recovery USB and know how to reach your firmware
  setup screen. The undoable-remove (`-RMV`) feature and the confirmation prompt
  reduce mistakes but are not a substitute for a recovery plan.
* **Reboot is immediate.** The `b` key ultimately runs `sudo reboot now` after a
  typed confirmation.

## About this Project
This project was inspired by [Elinvention/efiboots](https://github.com/Elinvention/efiboots). Relative to that project, the aims of `efibootdude` are:
* to be easier to install especially when not in your distro's repos.
* to clearly present the partition of the boot entries (as a mount point if mounted and, otherwise, the device pathname).
* to show the underlying commands being run for education, for verification, and for help on investigating issues.
