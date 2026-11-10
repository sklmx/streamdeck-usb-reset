import datetime as dt
import subprocess
import unittest
from unittest.mock import patch

import watchdog as w
import install


class RecoveryTests(unittest.TestCase):
    now = 1804289760

    def setUp(self):
        self.patches = [patch.object(w, 'read_log', return_value=[]),
                        patch.object(w, 'save'), patch.object(w, 'log'),
                        patch.object(w, 'target', return_value={'entry': 10}),
                        patch.object(w.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')),
                        patch.object(w.Path, 'exists', return_value=False)]
        self.read, self.save, self.log, self.target, self.run, self.exists = [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.cfg = dict(product='0080', serial='TEST_MK2', reset_after_wake=True)
        self.engine = w.Recovery(self.cfg, {})

    def tick(self, offset):
        self.engine.tick(self.now + offset, offset)

    def line(self, offset, text):
        return dt.datetime.fromtimestamp(self.now + offset, dt.timezone.utc).isoformat() + ' ' + text

    def wake_reset(self):
        self.engine.wake(self.now)
        self.tick(30)
        self.assertEqual(self.run.call_count, 1)

    def test_wake_delay_and_duplicate_notifications(self):
        self.engine.wake(self.now)
        self.tick(29)
        self.run.assert_not_called()
        self.engine.wake(self.now + 10)
        self.tick(39)
        self.run.assert_not_called()
        self.tick(40)
        self.assertEqual(self.run.call_args.args[0][1:4], ['--reset', '0080', 'TEST_MK2'])
        self.assertEqual(self.engine.job['attempts'], 1)

    def test_default_does_not_reset_silent_freeze(self):
        self.cfg['reset_after_wake'] = False
        self.engine.wake(self.now)
        self.tick(30)
        self.run.assert_not_called()
        self.assertIsNone(self.engine.job)

    def test_sleep_cancels_pending_work(self):
        self.engine.wake(self.now)
        self.engine.sleep()
        self.tick(31)
        self.run.assert_not_called()

    def test_presence_alone_does_not_confirm_recovery(self):
        self.wake_reset()
        self.tick(75)
        self.assertFalse(self.engine.confirmed)
        self.assertEqual(self.engine.job['phase'], 'ready')

    def test_retries_stop_after_two_attempts(self):
        self.run.side_effect = subprocess.TimeoutExpired('reset', 20)
        self.wake_reset()
        self.assertEqual(self.engine.state['last_attempt'], self.now + 30)
        self.tick(75)
        self.tick(629)
        self.assertEqual(self.run.call_count, 1)
        self.tick(630)
        self.tick(675)
        self.tick(2000)
        self.assertEqual(self.run.call_count, 2)
        self.assertIsNone(self.engine.job)
        self.assertFalse(self.engine.confirmed)

    def test_recovery_needs_new_entry_and_fresh_app_connection(self):
        self.wake_reset()
        self.target.return_value = {'entry': 11}
        self.read.return_value = [self.line(40, 'DeviceManager device status changed @(1)[4057/128/TEST_MK2]: connected')]
        self.tick(75)
        self.assertTrue(self.engine.confirmed)
        self.assertIsNone(self.engine.job)

    def test_old_connection_cannot_confirm_recovery(self):
        self.wake_reset()
        self.target.return_value = {'entry': 11}
        self.read.return_value = [self.line(0, 'device status changed [TEST_MK2]: connected')]
        self.tick(75)
        self.assertFalse(self.engine.confirmed)

    def test_failed_reset_is_checked_before_retry(self):
        self.run.return_value.returncode = 1
        self.wake_reset()
        self.tick(74)
        self.assertEqual(self.engine.job['phase'], 'verify')
        self.tick(75)
        self.assertEqual(self.engine.job['phase'], 'ready')

    def test_pause_cancels_retry(self):
        self.wake_reset()
        self.tick(75)
        self.exists.return_value = True
        self.tick(630)
        self.assertEqual(self.run.call_count, 1)
        self.assertIsNone(self.engine.job)

    def test_firmware_block_has_no_time_expiry(self):
        self.read.return_value = [self.line(-90000, 'Starting firmware update')]
        self.engine.wake(self.now)
        self.tick(30)
        self.run.assert_not_called()
        self.assertTrue(self.engine.state['firmware_block'])
        self.read.return_value = []
        self.engine.wake(self.now + 100000)
        self.tick(100030)
        self.run.assert_not_called()

    def test_unreadable_log_blocks_reset(self):
        self.read.side_effect = PermissionError('denied')
        self.engine.wake(self.now)
        self.tick(30)
        self.run.assert_not_called()

    def test_absent_or_ambiguous_device_blocks_reset(self):
        self.target.side_effect = ValueError('found 2')
        self.engine.wake(self.now)
        self.tick(30)
        self.run.assert_not_called()

    def test_persistent_cooldown_and_clock_rollback(self):
        self.engine.state['last_attempt'] = self.now
        self.engine.wake(self.now)
        self.tick(30)
        self.tick(-100)
        self.run.assert_not_called()
        self.tick(600)
        self.assertEqual(self.run.call_count, 1)

    def test_forward_clock_jump_does_not_bypass_local_cooldown(self):
        self.wake_reset()
        self.tick(75)
        self.engine.tick(self.now + 90000, 100)
        self.assertEqual(self.run.call_count, 1)

    def test_serial_prefix_does_not_match(self):
        self.assertFalse(w.mentions('[TEST_MK2_EXTRA]', 'TEST_MK2'))
        self.assertFalse(w.mentions('[XTEST_MK2]', 'TEST_MK2'))

    def test_fresh_error_uses_exact_serial_and_is_handled_once(self):
        self.read.return_value = [self.line(-5, 'DeviceComm [TEST_MK2] Read FW version: FAILED')]
        self.tick(0)
        self.tick(45)
        self.tick(600)
        self.tick(645)
        self.tick(1200)
        self.assertEqual(self.run.call_count, 2)

    def test_new_firmware_entry_blocks_immediately_before_usb_call(self):
        self.read.side_effect = [[], [self.line(29, 'Starting firmware update')]]
        self.engine.wake(self.now)
        self.tick(30)
        self.run.assert_not_called()
        self.assertTrue(self.engine.state['firmware_block'])

    def test_wake_delay_survives_forward_clock_jump(self):
        self.engine.wake(self.now, 0)
        self.engine.tick(self.now + 10000, 29)
        self.run.assert_not_called()
        self.engine.tick(self.now + 10001, 30)
        self.assertEqual(self.run.call_count, 1)

    def test_neo_product_is_preserved(self):
        self.cfg['product'] = '009A'
        self.wake_reset()
        self.assertEqual(self.run.call_args.args[0][2], '009A')

    def exhaust_with_repeated_errors(self):
        for offset, error_offset in ((0, -5), (45, 10), (600, 590), (645, 610)):
            self.read.return_value = [self.line(error_offset, 'DeviceComm [TEST_MK2] Read FW version: FAILED')]
            self.tick(offset)
        self.assertEqual(self.run.call_count, 2)
        self.assertIsNone(self.engine.job)

    def test_repeated_errors_cannot_replenish_exhausted_budget(self):
        self.exhaust_with_repeated_errors()
        self.read.return_value = [self.line(1190, 'DeviceComm [TEST_MK2] Read FW version: FAILED')]
        self.tick(1200)
        # A restart must also retain the failed recovery block.
        self.engine = w.Recovery(self.cfg, self.engine.state)
        self.tick(1800)
        self.assertEqual(self.run.call_count, 2)

    def test_new_connection_allows_a_later_error_recovery(self):
        self.exhaust_with_repeated_errors()
        self.read.return_value = [self.line(700, 'device status changed [TEST_MK2]: connected')]
        self.tick(710)
        self.read.return_value.append(self.line(1190, 'DeviceComm [TEST_MK2] Read FW version: FAILED'))
        self.tick(1200)
        self.assertEqual(self.run.call_count, 3)

    def test_installer_runs_continuous_listener(self):
        definition = install.definition()
        self.assertIn('--daemon', definition['ProgramArguments'])
        self.assertTrue(definition['KeepAlive'])
        self.assertNotIn('StartInterval', definition)


class TargetTests(unittest.TestCase):
    @patch.object(w, 'devices')
    def test_exact_product_vendor_and_serial(self, devices):
        cfg = dict(product='0080', serial='EXACT')
        wanted = dict(vendor='0FD9', product='0080', serial='EXACT', entry=1)
        devices.return_value = [dict(wanted, product='009A'), dict(wanted, serial='EXACT2'), wanted]
        self.assertEqual(w.target(cfg), wanted)
        devices.return_value.append(wanted)
        with self.assertRaises(ValueError):
            w.target(cfg)
        devices.return_value = [dict(wanted, vendor='OTHER')]
        with self.assertRaises(ValueError):
            w.target(cfg)
