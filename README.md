# Stream Deck USB recovery for macOS

Recover one Stream Deck MK.2 (`0FD9:0080`) or Neo (`0FD9:009A`) by USB
re-enumeration. The utility selects exactly one device by vendor, product, and
serial. It does not reset hubs, cycle hub power, change profiles, or write firmware.
It does not quit or restart the Stream Deck app.

## Build and confirm the device

Use macOS, Xcode Command Line Tools, and a permanent Python 3.9+ installation.
No third-party Python packages are required. Keep this checkout in a permanent
location before you install the optional service.

```sh
python3 install.py --build-only
./neo-usb-reset --list
python3 -m unittest discover -s tests -v
```

`--list` is read-only. It prints one JSON object per supported USB device.
Confirm the product and serial before configuration. For example, for an MK.2:

```sh
python3 install.py --configure --product 0080 --serial YOUR_SERIAL
python3 watchdog.py --check
./neo-usb-reset --access-check 0080 YOUR_SERIAL
```

The optional `--access-check` creates a USB user interface, then releases it.
It does not open or reset the device. If it fails inside a sandbox, repeat this
read-only check from a normal Terminal before you attempt a reset.

For a Neo, use `--product 009A`. Configuration checks the connected device and
writes a git-ignored `config.json`. It does not install or start anything.
Old serial-only configurations must be replaced with an explicit product choice.
`--check` reports the USB devices, last app firmware report, pending error,
pause and firmware blocks, saved attempt time, and the detection limit.
The firmware value comes from the app log; it is not a live firmware query.

## Detection limit and optional reset after wake

USB presence does not prove that the buttons or display work. The app can log
`connected` while a device still has a fault. The documented HID firmware and
serial queries test only part of the device. There is no documented full-device
health check in the protocol used here. This utility does not use a successful
firmware query, or a missing key event, as proof of health or failure.

By default, automatic recovery requires an explicit HID error for the exact
serial in the last 30 minutes. A later app connection clears that error.
Disconnect/reconnect events alone do not cause a reset. Silent freezes will
usually be missed in this mode.

To request a reset after each wake, including when no error is logged:

```sh
python3 install.py --configure --product 0080 --serial YOUR_SERIAL --reset-after-wake
```

This mode can reset a working device. It is an optional workaround for silent
freezes, not reliable freeze detection. Omit `--reset-after-wake` when you run
`--configure` again to disable it. Restart an installed service after configuration
changes.

## Recovery controls

The optional daemon receives native macOS system power notifications. It waits
at least 30 seconds after wake, and after daemon start, before a reset. It checks
the app log every five seconds. Repeated wake notifications extend the delay;
a sleep notification cancels pending work until the next wake.

Each recovery has at most two reset attempts. All attempts, including manual
attempts and timeouts, have a minimum interval of 600 seconds. The attempt time
is saved before USB access. Each app error is consumed once. A daemon restart
cancels pending retries; it does not repeat an already consumed app error.
After the attempt limit, further app errors cannot start another recovery until
the app logs a fresh connection. This block survives daemon restarts. An explicit
manual command or a new wake with reset-after-wake enabled can start another
recovery, subject to the same minimum interval.
Clock changes within a running daemon cannot shorten the minimum interval.
Across restarts the saved interval uses wall time; keep the Mac clock correct.

Before each attempt, the utility checks the exact USB identity again. An absent
or ambiguous device cancels recovery. It never selects a hub or another model.
After an attempt, it waits 45 seconds and checks for both a new USB registry
entry and a fresh app connection for the serial. These signals confirm a USB/app
reconnection only. Test the buttons and display yourself. Missing app logs,
missing signals, or USB access failures are reported. No app restart is forced.
Manual recovery performs one attempt and returns a nonzero status if the
reconnection is not confirmed. Automatic recovery can retry once after ten minutes.

## Firmware updates and pause

**Pause recovery before every firmware update.** There is no authoritative
firmware-update status API in this utility. Log detection is an extra guard;
it cannot protect against an update that starts without a log entry or starts
between the final check and USB access.

```sh
touch PAUSE
```

For firmware work, also stop an installed service and wait for any current reset
to finish before you start the update:

```sh
launchctl bootout gui/$(id -u)/com.ruslan.streamdeck-watchdog
```

A `PAUSE` file blocks all resets through `watchdog.py`. An unreadable app log also
blocks resets. Any firmware update/upgrade/flash mention in the last 1 MiB of the
app log sets a saved block, even if the mention is old or belongs to another
device. The block has no automatic expiry. This can cause a conservative false
alarm. It does not rely on a log completion message or log rotation to unblock.

After the update is complete, clear a saved firmware block explicitly:

```sh
python3 watchdog.py --clear-firmware-block --confirm-no-firmware-update
rm -f PAUSE
```

Clear the block only when no firmware update is in progress. This acknowledges
older log entries; new update entries will block recovery again. The service must
be stopped for manual reset or block-clear commands. A process lock prevents
concurrent reset commands. A pause cannot cancel a USB call that already started.

## Manual reset

First confirm that firmware work is complete. Stop an installed service, remove
`PAUSE` if appropriate, then use:

```sh
python3 watchdog.py --check
python3 watchdog.py --reset --confirm-no-firmware-update
```

The command uses the configured product and serial and all recovery guards.
If USB access is busy, quit the Stream Deck app yourself, wait for the minimum
interval, and try again. With the app closed, its reconnection check cannot pass
until you reopen it. Do not run as root to bypass an access error.

`neo-usb-reset --reset PRODUCT SERIAL --confirm-no-firmware-update` is the low-level
backend. It checks VID/PID/serial, but does not apply Python pause, log, lock, or
interval guards. Use the Python command for normal operation.

## Optional background service

Only the following command installs and starts a service:

```sh
python3 install.py --install
```

This installs user LaunchAgent `com.ruslan.streamdeck-watchdog`. It runs at login
and stays active to receive wake events. No root access is required. The installer
uses the Python executable that runs it, so use a persistent interpreter.
The configuration must exist and the exact device must be connected.

Activity goes to `watchdog.log` in this folder. macOS does not rotate this file;
archive it periodically with the service stopped. The app log read is bounded
to the last 1 MiB of `~/Library/Logs/ElgatoStreamDeck/StreamDeck.log`.
Changes to the app log format can affect error and connection detection.

For a temporary pause, create `PAUSE`; remove it to resume. To restart the service
after stopping it or changing the configuration, run `python3 install.py --install`
again. Inspect `python3 watchdog.py --check` before you resume.

Uninstall the service:

```sh
python3 install.py --uninstall
```

This unloads the service and removes its LaunchAgent file. Source, binary,
configuration, pause file, logs, and saved recovery state remain. Keep saved state
to preserve the minimum interval. No uninstall command resets the device.

## Validation

The MK.2 USB identity and app firmware report were checked read-only. Native
power notification registration also succeeded. A sandboxed manual attempt failed
before USB reset with `0xe00002be` (resource shortage); the read-only USB user
interface check succeeded outside the sandbox. A single operator-approved reset
outside the sandbox then succeeded while the official app was running. The exact
MK.2 returned with a new USB registry entry, and the app logged a fresh connection
with firmware `1.02.000`. The saved interval was bypassed once for that test;
the normal ten-minute limit in the code was not changed. The user confirmed
that the device worked and kept its selected page after the reset. Recovery
from an actual freeze remains unverified. The native
helper builds with `-Wall -Wextra -Werror`. Unit tests use simulated wake events,
USB responses, failures, timeouts, pauses, firmware blocks, and app log entries;
they do not reset hardware or put the Mac to sleep.

The following checks still need an approved physical test:

- Delivery of sleep/wake notifications during a real Mac sleep cycle.
- USB readiness and the 30-second delay with this dock/cable setup.
- Recovery of a real MK.2 freeze, including buttons, display, and profile actions.
- The app reconnection signals and retry behavior during a real failed recovery.
- LaunchAgent installation, login behavior, pause, restart, and uninstall.
- Neo hardware behavior after these changes; Neo support is retained but not retested.

References: [Elgato MK.2 device protocol](https://docs.elgato.com/streamdeck/hid/stream-deck-classic/),
[Elgato shared HID commands](https://docs.elgato.com/streamdeck/hid/general/),
and [Apple USBDeviceReEnumerate](https://developer.apple.com/documentation/iokit/iousbdeviceinterface187/1559027-usbdevicereenumerate).
