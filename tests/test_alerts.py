from __future__ import annotations

import contextlib
import io
import json
from unittest.mock import patch

from fixtures import ProjectCase
from newspaper.alerts import process_failure_alerts, record_command
from newspaper.schedule import jobs
from newspaper.storage import Store, utcnow
from newspaper.notify import UncertainDelivery


class FailureAlertTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.settings = self.make_settings()
        self.settings.values['notifications'].update(
            mode='accounts', failure_alerts={'account_id': 'Owner'})
        self.settings.values['_accounts'] = [
            {'id': 'Owner', 'emails': ['owner@example.test'], 'telegram_chat_id': '123', 'subscriptions': []},
            {'id': 'Other', 'emails': ['other@example.test'], 'telegram_chat_id': '456'}]
        self.job, self.window = jobs(self.settings, self.now)[0]
        self.key = self.window['date'] + '/' + self.job['id']

    def failures(self, count=3):
        metrics = {'stage': 'translate-review', 'translation_failures': [
            {'event_key': 'fixture|event', 'reason': 'Review rejected translation',
             'review': {'problems': [], 'sentence_verdicts': [
                 {'id': 0, 'ok': False, 'problems': ['Wrong tense in sentence 0']}]}}]}
        for _ in range(count):
            self.store.db.execute('''insert into runs(day,started_at,finished_at,status,error,metrics)
                values(?,?,?,'failed','ValueError',?)''',
                (self.key, utcnow().timestamp(), utcnow().timestamp(), json.dumps(metrics)))
        self.store.db.commit()

    def test_only_configured_owner_gets_alert_with_reason_once_across_restarts(self):
        self.failures()
        with patch('newspaper.alerts.readiness', return_value=True), patch('newspaper.alerts.send_text') as send:
            self.assertEqual(process_failure_alerts(self.settings, self.store)['sent'], 2)
            self.assertEqual({c.kwargs['recipient'] for c in send.call_args_list}, {'owner@example.test', '123'})
            self.assertTrue(all('Wrong tense in sentence 0' in c.args[3] for c in send.call_args_list))
            self.store.close();self.store = Store(self.settings)
            self.assertEqual(process_failure_alerts(self.settings, self.store)['sent'], 0)
            self.assertEqual(send.call_count, 2)
        self.assertEqual(self.store.db.execute('select count(*) from account_outbox').fetchone()[0], 0)

    def test_first_two_failures_are_silent_and_resolved_incidents_are_not_backfilled(self):
        self.failures(2)
        with patch('newspaper.alerts.send_text') as send:
            self.assertEqual(process_failure_alerts(self.settings, self.store)['sent'], 0)
            self.failures(1)
            self.save(self.job, self.window)
            self.assertEqual(process_failure_alerts(self.settings, self.store)['sent'], 0)
            send.assert_not_called()

    def test_delivery_retry_exhaustion_alert_goes_to_owner_not_failed_recipient(self):
        from newspaper.batches import prepare_batches
        batch_id = prepare_batches(self.settings, self.store, [(self.job, self.window)])[0]
        self.store.db.execute('''insert into account_outbox(batch_id,account_id,channel,recipient,options,state,attempts,last_error)
            values(?,'Other','email','other@example.test','{}','failed',4,'SMTPException')''', (batch_id,))
        self.store.db.commit()
        with patch('newspaper.alerts.readiness', return_value=True), patch('newspaper.alerts.send_text') as send:
            self.assertEqual(process_failure_alerts(self.settings, self.store)['sent'], 0)
            self.store.db.execute('update account_outbox set attempts=5');self.store.db.commit()
            self.assertEqual(process_failure_alerts(self.settings, self.store)['sent'], 2)
            self.assertEqual({c.kwargs['recipient'] for c in send.call_args_list}, {'owner@example.test', '123'})
            self.assertTrue(all('other@example.test' not in c.args[3] for c in send.call_args_list))

    def test_alert_failures_retry_with_backoff_and_ambiguous_delivery_is_not_resent(self):
        self.failures()
        with patch('newspaper.alerts.readiness', return_value=True), \
             patch('newspaper.alerts.send_text', side_effect=[RuntimeError('private provider URL'), UncertainDelivery()]) as send:
            result = process_failure_alerts(self.settings, self.store)
            self.assertEqual((result['failed'], result['uncertain']), (1, 1))
            process_failure_alerts(self.settings, self.store)
            self.assertEqual(send.call_count, 2)
        self.store.db.execute('update failure_alert_outbox set next_attempt=0');self.store.db.commit()
        with patch('newspaper.alerts.readiness', return_value=True), patch('newspaper.alerts.send_text') as send:
            self.assertEqual(process_failure_alerts(self.settings, self.store)['sent'], 1)
            self.assertEqual(send.call_args.kwargs['recipient'], 'owner@example.test')

    def test_recovery_or_owner_address_change_cancels_pending_alerts(self):
        self.failures()
        with patch('newspaper.alerts.readiness', return_value=False):
            self.assertEqual(process_failure_alerts(self.settings, self.store)['unconfigured'], 2)
        self.settings.values['_accounts'][0]['emails'] = []
        self.save(self.job, self.window)
        with patch('newspaper.alerts.send_text') as send:
            process_failure_alerts(self.settings, self.store)
            send.assert_not_called()
        self.assertEqual(self.store.db.execute("select count(*) from failure_alert_outbox where state='cancelled'").fetchone()[0], 2)

    def test_queued_alert_survives_the_rolling_retry_window_until_recovery(self):
        self.failures()
        with patch('newspaper.alerts.readiness', return_value=False):
            process_failure_alerts(self.settings, self.store)
        self.store.db.execute('update runs set started_at=started_at-7*3600');self.store.db.commit()
        with patch('newspaper.alerts.readiness', return_value=True), patch('newspaper.alerts.send_text') as send:
            self.assertEqual(process_failure_alerts(self.settings, self.store)['sent'], 2)
            self.assertEqual(send.call_count, 2)

    def test_cli_errors_alert_after_three_failures_and_success_resets_counter(self):
        from newspaper.cli import main
        with patch('newspaper.cli.Settings.load', return_value=self.settings), \
             patch('sys.argv', ['newspaper', 'run']), \
             patch('newspaper.cli.generate', side_effect=RuntimeError('secret provider response')), \
             patch('newspaper.alerts.readiness', return_value=True), patch('newspaper.alerts.send_text') as send, \
             contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3):self.assertEqual(main(), 1)
            self.assertEqual(send.call_count, 2)
            self.assertTrue(all('secret provider response' not in c.args[3] for c in send.call_args_list))
        record_command(self.settings, self.store, 'run')
        self.assertEqual(self.store.db.execute('select count(*) from workflow_health').fetchone()[0], 0)

    def test_alert_target_must_be_one_known_account_and_disabling_it_stops_alerts(self):
        self.settings.validate()
        self.settings.values['_accounts'][0]['enabled'] = False
        self.settings.validate()
        self.failures()
        with patch('newspaper.alerts.send_text') as send:
            process_failure_alerts(self.settings, self.store)
            send.assert_not_called()
        for value in [{'account_id': '*'}, {'account_ids': ['Owner', 'Other']}, {'account_id': ['Owner']}]:
            self.settings.values['notifications']['failure_alerts'] = value
            with self.assertRaises(ValueError):self.settings.validate()

    def test_no_notify_run_does_not_send_failure_alerts(self):
        from newspaper.cli import main
        self.failures()
        with patch('newspaper.cli.Settings.load', return_value=self.settings), \
             patch('sys.argv', ['newspaper', 'run', '--no-notify']), \
             patch('newspaper.cli.generate', return_value={'state': 'partial_failure'}), \
             patch('newspaper.alerts.send_text') as send, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 1)
            send.assert_not_called()
