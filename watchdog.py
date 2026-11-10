#!/usr/bin/env python3
"""Bounded, serial-specific Stream Deck recovery. No action on import."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time

BASE = Path(__file__).resolve().parent
LOG = Path.home() / 'Library/Logs/ElgatoStreamDeck/StreamDeck.log'
STATE = BASE / 'watchdog-state.json'
COOLDOWN = 600
WAKE_DELAY = 30
VERIFY_DELAY = 45
MAX_ATTEMPTS = 2


def log(message):
    print(dt.datetime.now().astimezone().isoformat(), message, flush=True)


def read_log():
    with LOG.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - 1024 * 1024))
        return stream.read().decode('utf-8', errors='replace').splitlines()


def stamp(line):
    try:
        value = dt.datetime.fromisoformat(line.split()[0])
        return value.timestamp() if value.tzinfo else 0
    except (ValueError, IndexError):
        return 0


def mentions(line, serial):
    return bool(serial and re.search(r'(?<![A-Za-z0-9_-])' + re.escape(serial) + r'(?![A-Za-z0-9_-])', line))


def firmware_update(lines, acknowledged=0):
    # No automatic expiry: a long update must not lose its protection.
    return any(re.search(r'firmware.*(updat|upgrad|flash)|(?:updat|upgrad|flash).*firmware', line, re.I)
               and (not stamp(line) or stamp(line) > acknowledged) for line in lines)


def connection_time(lines, now, serial):
    return max((stamp(line) for line in lines if mentions(line, serial)
                and 'device status changed' in line and re.search(r': connected\s*$', line)
                and 0 < stamp(line) <= now + 5), default=0)


def failure_time(lines, now, serial, acknowledged=0):
    if firmware_update(lines, acknowledged):
        return 0
    failure = max((stamp(line) for line in lines if mentions(line, serial)
                   and 0 <= now - stamp(line) <= 1800
                   and (any(message in line for message in (
                       'Read FW version: FAILED', 'Problem with connecting HID device',
                       'SetRGB                           Ignore error: FAILED',
                       'Upload Image                     Ignore error: FAILED'))
                        or ('DeviceComm' in line and 'Ignore error: FAILED' in line))), default=0)
    return failure if failure > connection_time(lines, now, serial) else 0


def config():
    value = json.loads((BASE / 'config.json').read_text())
    if value.get('product') not in ('0080', '009A'):
        raise ValueError('Configure product 0080 (MK.2) or 009A (Neo); no default device is selected')
    if not isinstance(value.get('serial'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value['serial']):
        raise ValueError('Configure an exact, nonempty serial')
    if type(value.get('reset_after_wake', False)) is not bool:
        raise ValueError('reset_after_wake must be true or false')
    return value


def devices():
    result = subprocess.run([str(BASE / 'neo-usb-reset'), '--list'],
                            capture_output=True, text=True, timeout=10, check=True)
    return [json.loads(line) for line in result.stdout.splitlines()]


def target(cfg):
    matches = [d for d in devices() if d.get('vendor') == '0FD9'
               and d.get('product') == cfg['product'] and d.get('serial') == cfg['serial']]
    if len(matches) != 1:
        raise ValueError(f'Expected one exact VID/PID/serial match; found {len(matches)}')
    return matches[0]


def save(state):
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2) + '\n')
    temporary.replace(STATE)


class Recovery:
    def __init__(self, cfg, state):
        self.cfg, self.state = cfg, state
        self.job = None
        self.last_mono = None
        self.confirmed = False

    def wake(self, now, mono=None):
        # Coalesce notifications. A new wake must not replenish an active retry budget.
        if self.job:
            self.job['due'] = max(self.job['due'], now + WAKE_DELAY)
        else:
            self.job = dict(kind='wake', due=now + WAKE_DELAY, attempts=0, phase='ready')
        if mono is not None:
            self.job['due_mono'] = max(self.job.get('due_mono', 0), mono + WAKE_DELAY)
        log('Wake received; wait 30 seconds for USB devices')

    def sleep(self):
        # Cancel work until a real wake arrives. Do not reset while sleep is pending.
        self.job = None
        log('Sleep notification; pending recovery cancelled')

    def guarded(self, lines):
        if not self.state.get('firmware_block') and firmware_update(lines, self.state.get('firmware_ack', 0)):
            self.state['firmware_block'] = True
            save(self.state)
        if (BASE / 'PAUSE').exists():
            return 'PAUSE file is present'
        if self.state.get('firmware_block'):
            return 'firmware update block; clear only after the update is complete'
        return None

    def tick(self, now, mono):
        try:
            lines = read_log()
        except OSError as error:
            log(f'Recovery blocked: app log is not readable: {error}')
            self.job = None
            return
        block = self.guarded(lines)
        if block:
            if self.job:
                log('Recovery cancelled: ' + block)
            self.job = None
            return
        failure = failure_time(lines, now, self.cfg['serial'], self.state.get('firmware_ack', 0))
        if (self.state.get('failed_recovery') and
                connection_time(lines, now, self.cfg['serial']) > self.state['failed_recovery']):
            self.state.pop('failed_recovery')
            save(self.state)
        if (not self.job and not self.state.get('failed_recovery') and
                failure > self.state.get('handled_failure', 0)):
            self.state['handled_failure'] = failure
            save(self.state)
            self.job = dict(kind='error', due=now, attempts=0, phase='ready', failure=failure)
        job = self.job
        if not job or now < job['due'] or mono < job.get('due_mono', 0):
            return
        if job['phase'] == 'verify':
            try:
                current = target(self.cfg)
                connected = connection_time(lines, now, self.cfg['serial'])
                recovered = (current['entry'] != job['entry'] and connected > job['started']
                             and failure <= job['started'])
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                log(f'Recovery check failed: {error}')
                recovered = False
            if recovered:
                self.confirmed = True
                self.state.pop('failed_recovery', None)
                save(self.state)
                log('USB device returned and app logged a fresh connection. Check buttons and display manually.')
                self.job = None
            elif job['attempts'] >= job.get('limit', MAX_ATTEMPTS):
                self.state['failed_recovery'] = now
                save(self.state)
                log('Recovery not confirmed; attempt limit reached. App error recovery is blocked until a fresh connection. Check the cable and app manually.')
                self.job = None
            else:
                log('Recovery not confirmed; one retry remains after the minimum interval')
                job.update(phase='ready', due=job['started'] + COOLDOWN)
            return
        if job['kind'] == 'wake' and not self.cfg.get('reset_after_wake', False):
            log('Wake check complete; freeze state is unknown. Optional reset after wake is disabled.')
            self.job = None
            return
        if job['kind'] == 'error' and not failure:
            log('App error cleared before reset; recovery cancelled')
            self.job = None
            return
        # Wall time protects across process restarts; monotonic time also protects
        # against a forward clock correction within this process.
        if (now - self.state.get('last_attempt', 0) < COOLDOWN or
                (self.last_mono is not None and mono - self.last_mono < COOLDOWN)):
            return
        try:
            current = target(self.cfg)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            log(f'No reset: {error}')
            self.job = None  # An absent or ambiguous target is not a reason to reset anything.
            return
        # Recheck the guard immediately before every attempt, including retries.
        try:
            block = self.guarded(read_log())
        except OSError:
            block = 'app log is not readable'
        if block:
            log('Recovery cancelled: ' + block)
            self.job = None
            return
        self.state['last_attempt'] = now
        save(self.state)  # A crash or timeout must not bypass the minimum interval.
        self.last_mono = mono
        job.update(attempts=job['attempts'] + 1, phase='verify',
                   started=now, entry=current['entry'], due=now + VERIFY_DELAY, due_mono=mono + VERIFY_DELAY)
        log(f"Reset {self.cfg['product']}/{self.cfg['serial']}; reason={job['kind']}; attempt={job['attempts']}")
        try:
            result = subprocess.run([str(BASE / 'neo-usb-reset'), '--reset', self.cfg['product'],
                                     self.cfg['serial'], '--confirm-no-firmware-update'],
                                    capture_output=True, text=True, timeout=20)
            log(f'USB reset exit={result.returncode}: {(result.stdout + result.stderr).strip()}')
        except (OSError, subprocess.SubprocessError) as error:
            log(f'USB reset failed: {error}')
        # Even an error/timeout may have re-enumerated the device. Verify first.


def power_events(process, events):
    for line in process.stdout:
        events.put(line.strip())
    events.put('eof')


def run_daemon(engine):
    events = queue.Queue()
    process = subprocess.Popen([str(BASE / 'neo-usb-reset'), '--watch-wake'],
                               stdout=subprocess.PIPE, text=True)
    threading.Thread(target=power_events, args=(process, events), daemon=True).start()
    sleeping = False
    ready_at = time.monotonic() + WAKE_DELAY
    try:
        if events.get(timeout=10) != 'ready':
            raise RuntimeError('Power notification helper did not become ready')
        log('Watchdog ready; native macOS power notifications enabled')
        while True:
            try:
                event = events.get(timeout=5)
            except queue.Empty:
                event = None
            # Drain all queued notifications before deciding to reset.
            pending = [event]
            while not events.empty():
                pending.append(events.get_nowait())
            for event in pending:
                if event == 'eof':
                    raise RuntimeError('Power notification helper stopped')
                if event == 'sleep':
                    sleeping = True
                    engine.sleep()
                elif event == 'wake':
                    sleeping = False
                    engine.wake(time.time(), time.monotonic())
            if not sleeping and time.monotonic() >= ready_at:
                engine.tick(time.time(), time.monotonic())
    finally:
        process.terminate()
        process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true', help='Read-only diagnostics')
    action.add_argument('--reset', action='store_true', help='One manual reset and recovery check')
    action.add_argument('--daemon', action='store_true')
    action.add_argument('--clear-firmware-block', action='store_true')
    parser.add_argument('--confirm-no-firmware-update', action='store_true')
    args = parser.parse_args()
    cfg = config()
    if args.check:
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        lines = read_log()
        print(json.dumps(dict(config=cfg, devices=devices(), paused=(BASE / 'PAUSE').exists(),
                              firmware_block=bool(state.get('firmware_block') or firmware_update(lines, state.get('firmware_ack', 0))),
                              failure_timestamp=failure_time(lines, time.time(), cfg['serial'], state.get('firmware_ack', 0)),
                              last_app_device_report=next((line for line in reversed(lines) if mentions(line, cfg['serial']) and 'firmware version:' in line), None),
                              state=state, health='unknown: USB presence and app connection do not prove full operation'), indent=2))
        return
    with (BASE / 'watchdog.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Watchdog is running. Stop the service before manual commands.')
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        if args.clear_firmware_block:
            if not args.confirm_no_firmware_update:
                parser.error('--confirm-no-firmware-update is required')
            state.update(firmware_ack=time.time(), firmware_block=False)
            save(state)
            log('Firmware block cleared by operator')
            return
        engine = Recovery(cfg, state)
        if args.reset:
            if not args.confirm_no_firmware_update:
                parser.error('--confirm-no-firmware-update is required')
            if time.time() - state.get('last_attempt', 0) < COOLDOWN:
                parser.error('Minimum interval has not elapsed; no reset performed')
            engine.job = dict(kind='manual', due=0, attempts=0, phase='ready', limit=1)
            engine.tick(time.time(), time.monotonic())
            if not engine.job or not engine.job['attempts']:
                raise SystemExit(1)
            while engine.job:
                time.sleep(1)
                engine.tick(time.time(), time.monotonic())
            # Recovery remains a partial check, even when it passes.
            if not engine.confirmed:
                raise SystemExit(1)
        else:
            def stop(signum, frame):
                raise SystemExit(0)
            signal.signal(signal.SIGTERM, stop)
            run_daemon(engine)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, queue.Empty) as error:
        log(f'Stopped: {error}')
        raise SystemExit(1)
