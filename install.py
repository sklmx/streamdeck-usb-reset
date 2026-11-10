#!/usr/bin/env python3
"""Build, configure, or explicitly install the current user's watchdog."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

BASE = Path(__file__).resolve().parent
LABEL = 'com.ruslan.streamdeck-watchdog'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')


def build():
    subprocess.run(['clang', '-Wall', '-Wextra', '-Werror', '-Wno-deprecated-declarations',
                    str(BASE / 'neo-usb-reset.c'), '-framework', 'IOKit', '-framework',
                    'CoreFoundation', '-o', str(BASE / 'neo-usb-reset')], check=True)


def definition():
    return dict(Label=LABEL, ProgramArguments=[sys.executable, str(BASE / 'watchdog.py'), '--daemon'],
                RunAtLoad=True, KeepAlive=True, ThrottleInterval=30, ProcessType='Background',
                StandardOutPath=str(BASE / 'watchdog.log'), StandardErrorPath=str(BASE / 'watchdog.log'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--build-only', action='store_true')
    action.add_argument('--configure', action='store_true')
    action.add_argument('--install', action='store_true')
    action.add_argument('--uninstall', action='store_true')
    parser.add_argument('--product', choices=['0080', '009A'])
    parser.add_argument('--serial')
    parser.add_argument('--reset-after-wake', action='store_true')
    args = parser.parse_args()
    if not args.configure and (args.product or args.serial or args.reset_after_wake):
        parser.error('Device and wake options require --configure')
    if args.uninstall:
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], check=False)
        PLIST.unlink(missing_ok=True)
        print('LaunchAgent removed. Source and configuration preserved.')
        return
    if args.build_only:
        build()
        return
    if args.configure:
        import re
        if not args.product or not args.serial or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.serial):
            parser.error('--product and an exact --serial are required')
        from watchdog import target
        cfg = dict(product=args.product, serial=args.serial, reset_after_wake=args.reset_after_wake)
        target(cfg)  # Read-only check; refuse absent or ambiguous devices.
        (BASE / 'config.json').write_text(json.dumps(cfg, indent=2) + '\n')
        print('Configuration saved. No service installed. No reset performed.')
        return
    from watchdog import config, target
    target(config())
    if not sys.executable or not os.access(sys.executable, os.X_OK):
        parser.error('A persistent Python 3 interpreter is required')
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    with PLIST.open('wb') as stream:
        plistlib.dump(definition(), stream)
    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(PLIST)], check=True)
    print('Installed: native wake notifications and app error checks every 5 seconds.')


if __name__ == '__main__':
    main()
