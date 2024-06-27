# EfiBootDude
`efibootdude` presents a visual (curses) interface to `efibootmgr` which allows editing the bios
boot menu and parameters while running Linux.

* Install `efibootdude` using `pipx install efibootdude`, or however you do so.
* Prerequisites: install [rhboot/efibootmgr](https://github.com/rhboot/efibootmgr)
  * For example, on a Debian derived distro, use `sudo apt install efibootmgr`.


`efibootdude` covers only the most commonly used capabilities of `efibootmgr` including:
* reordering boot entries,
* removing boot entries,
* setting the boot entry for the next boot only,
* setting boot entries active or inactive, and
* setting the boot menu timeout value (until it boots the default entry).

To be sure, there are many other esoteric uses of `efibootmanager` including adding
a new boot entry; for such needs, just use `efibootmgr` directly.
  
## Usage
After running `efibootdude`, you'll see a screen like this:

![efibootdude-screenshot](https://github.com/joedefen/efibootdude/blob/main/images/efibootdude-screenshot.png?raw=true).

At this point
* The current line starts with `>` and is highlighted.
* The top line shows actions for the current line.
* Enter `?` for a more complete explanation of the keys, navigation keys, etc.
* With this current line, we can:
  * Use `u` or `d` to move it up or down in the boot order.
  * Use `t` to relabel the boot entry.
  * Use `r` to remove it.
  * And so forth.
* When ready to write the changes to the BIOS, enter 'w'.
* When writing the changes, `efibootdude` drops out of menu mode so you can
  verify the underlying commands, error codes, and error messages.
* Use the `ESC` key to abandon changes and refresh the boot information.

## Caveats
* Some operations may not work permanently even though there is no indication from `efibootmgr`
  (e.g., on my desktop, I cannot re-label boot entries).
* Some operations may only work (again) after re-booting (e.g., you might find activating
  an entry does not work, but it does so after a reboot).

## About this Project
This project was inspired by [Elinvention/efiboots](https://github.com/Elinvention/efiboots). Relative to that project, the aims of `efibootdude` are:
* to be easier to install especially when not in your distro's repos.
* to clearly present the partition of the boot entries (as a mount point if mounted and, otherwise, the device pathname).
* to show the underlying commands being run for education, for verification, and for help on investigating issues.
