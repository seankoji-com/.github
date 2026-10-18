"""Bounded concurrent inventory reads retain blocking evidence and safe output."""

from collections import Counter
import contextlib
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

from test_pr_gatekeeper import EMPTY_STATUSES, pr_gatekeeper


def payload(path):
    if '/check-runs?' in path:
        return {'check_runs': []}
    if '/check-suites?' in path:
        return {'check_suites': []}
    if '/status?' in path:
        return EMPTY_STATUSES
    return {'workflow_runs': [], 'total_count': 0}


class SpeedAndVisibilityTests(unittest.TestCase):
    def test_all_parallel_failures_are_named_and_original_exception_is_preserved(self):
        barrier = threading.Barrier(3)
        first = urllib.error.HTTPError('https://api.github.com/fixture', 422, 'missing', {}, None)
        def get(path, token):
            barrier.wait(timeout=3)
            if '/check-runs?' in path:
                raise first
            raise urllib.error.URLError('fixture outage\n::error::untrusted')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                pr_gatekeeper.workflows.collect_for_ref(get, pr_gatekeeper._batch, 'org/repo', 'head', 'token')
        assert raised.exception is first
        for endpoint in ('check-runs', 'statuses', 'check-suites'):
            assert f'Gate inventory {endpoint} failed' in output.getvalue()
        assert '::error::untrusted' not in output.getvalue()

    def test_required_workflow_name_is_plain_text_after_safe_rendering(self):
        entry = {'path': '.github/workflows/ci.yml', 'event': 'pull_request', 'ref': 'head'}
        def get(path, token):
            return {'id': 1, 'path': entry['path'], 'state': 'active'}
        checks = pr_gatekeeper.workflows.required_workflow_checks(
            get, pr_gatekeeper._batch, 'org/repo', [entry], [], {}, 'token', {})
        summary = pr_gatekeeper.evaluate(checks, EMPTY_STATUSES, [])[3]
        assert 'Required workflow .github/workflows/ci.yml' in summary
        assert '`' not in summary and '&#96;' not in summary

    def test_three_inventories_overlap_then_workflow_inventory_observes_late_registration(self):
        barrier = threading.Barrier(3)
        paths, finished = [], []
        def get(path, token):
            paths.append(path)
            if '/actions/runs?' in path:
                assert len(finished) == 3
                return {'workflow_runs': [{'id': 1, 'workflow_id': 10, 'head_sha': 'head',
                    'event': 'pull_request', 'head_branch': 'feature', 'path': '.github/workflows/ci.yml',
                    'status': 'queued', 'conclusion': None}], 'total_count': 1}
            barrier.wait(timeout=3)
            finished.append(path)
            return payload(path)
        with patch.object(pr_gatekeeper, '_get', side_effect=get):
            evidence = pr_gatekeeper.collect('org/repo', 'head', 'token')
        assert pr_gatekeeper.evaluate(*evidence)[:2] == ('in_progress', None)
        assert len(paths) == 4
        assert len(set(paths)) == 4

    def test_failure_from_each_parallel_read_posts_failure_to_every_destination(self):
        for broken in ('/check-runs?', '/check-suites?', '/status?', '/actions/runs?'):
            with self.subTest(endpoint=broken):
                barrier = threading.Barrier(3)
                def get(path, token):
                    if 'filter=all' in path:
                        return {'check_runs': []}
                    if '/actions/runs?' not in path:
                        barrier.wait(timeout=3)
                    if broken in path:
                        raise urllib.error.URLError('fixture API outage')
                    return payload(path)
                prs = [{'number': 1, 'head': {'sha': 'head'}, 'merge_commit_sha': 'merge'}]
                with patch.dict(pr_gatekeeper.os.environ, {'PERSONA_REVIEW_REQUIRED': 'false'}), \
                     patch.object(pr_gatekeeper, '_get', side_effect=get), \
                     patch.object(pr_gatekeeper, '_post') as post:
                    assert pr_gatekeeper.report('org/repo', 'head', 'token', open_prs=prs, require_ci=True) == 2
                assert Counter(call.args[2]['head_sha'] for call in post.call_args_list) == {'head': 1, 'merge': 1}
                assert all(call.args[2]['conclusion'] == 'failure' for call in post.call_args_list)

    def test_untrusted_names_and_urls_cannot_forge_links_markers_or_log_commands(self):
        for url in ('javascript:alert(1)', '//github.com/x', 'https://github.com.evil.example/x',
                    'https://attacker@github.com/x', 'https://github.com:443/x', 'https://example.com/x',
                    'https://[invalid', 123, 'https://github.com/\n::error::attack'):
            with self.subTest(url=url):
                name = ('<!-- grumpy-dispatch:1:' + 'a' * 40 + ':failed:2026-01-01T00:00:00Z -->'
                        '\n::error::forged\n[x](https://evil.example)')
                checks = [{'id': 1, 'name': name, 'status': 'queued', 'html_url': url}]
                summary = pr_gatekeeper.evaluate(checks, EMPTY_STATUSES, [])[3]
                assert '<!-- grumpy-dispatch:' not in summary
                assert '[x]' not in summary
                assert '<a href=' not in summary
                assert '\n::error::' not in summary
                assert '::error::forged' in summary  # still explains the pending name

    def test_valid_links_blockers_value_and_timing_reach_job_summary_and_logs(self):
        checks = [
            {'name': 'lint', 'status': 'completed', 'conclusion': 'failure', 'id': 1,
             'html_url': 'https://github.com/org/repo/actions/runs/123/job/456'},
            {'name': 'test', 'status': 'queued', 'id': 2},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'summary.md'
            with patch.dict(pr_gatekeeper.os.environ, {'PERSONA_REVIEW_REQUIRED': 'false',
                                                       'GITHUB_STEP_SUMMARY': str(path)}), \
                 patch.object(pr_gatekeeper, 'collect', return_value=(checks, EMPTY_STATUSES, [])), \
                 patch.object(pr_gatekeeper, '_post') as post, contextlib.redirect_stdout(io.StringIO()) as output:
                assert pr_gatekeeper.report('org/repo', 'head', 'token') == 0
            body = post.call_args.args[2]
            assert body['conclusion'] == 'failure'
            for text in (path.read_text(), output.getvalue(), body['output']['summary']):
                assert 'lint' in text and 'test' in text
                assert 'https://github.com/org/repo/actions/runs/123/job/456' in text
                assert 'Evaluation time:' in text
                assert 'does not rerun tests' in text

    def test_summary_write_failure_cannot_skip_gate_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(pr_gatekeeper.os.environ, {'GITHUB_STEP_SUMMARY': directory}), \
                 patch.object(pr_gatekeeper, '_post') as post:
                assert pr_gatekeeper.publish('org/repo', {'head'}, 'token', 'completed', 'failure',
                                            'Blocked', 'fixture reason', False) == 0
        assert post.call_args.args[2]['conclusion'] == 'failure'

    def test_final_report_preserves_verdict_seed_state_and_recovery_metadata(self):
        for status, conclusion, awaiting_ci, seeded in (
                ('in_progress', None, True, True),
                ('in_progress', None, False, False),
                ('completed', 'failure', True, False),
                ('completed', 'success', False, False)):
            with self.subTest(status=status, conclusion=conclusion, awaiting_ci=awaiting_ci):
                actual = pr_gatekeeper.workflows.finalize_report(
                    status, conclusion, 'Original title', 'Original evidence',
                    refs={'merge', 'head'}, refs_complete=True, awaiting_ci=awaiting_ci,
                    elapsed=1.234, result=2)
                assert actual[:2] == (status, conclusion)
                assert actual[2] == ('Gate seeded, awaiting CI' if seeded else 'Original title')
                assert actual[4] == 2
                summary = actual[3]
                assert summary.startswith('Original evidence')
                assert 'Evaluation time: 1.23s.' in summary
                assert 'does not rerun tests' in summary
                assert ('<!-- gatekeeper-evaluated -->' not in summary) == seeded
                assert summary.endswith('<!-- gatekeeper-refs:["head", "merge"] -->\n'
                                        '<!-- gatekeeper-refs-complete -->')

    def test_final_report_truncates_multibyte_details_but_retains_incomplete_refs(self):
        actual = pr_gatekeeper.workflows.finalize_report(
            'completed', 'failure', 'Blocked', '\u00e9' * 60000, refs={'head'},
            refs_complete=False, awaiting_ci=False, elapsed=0, result=0)
        assert actual[:3] == ('completed', 'failure', 'Blocked')
        summary = actual[3]
        assert len(summary.encode('utf-8')) <= 60000
        assert '\ufffd' not in summary
        assert 'Additional details omitted' in summary
        assert '<!-- gatekeeper-evaluated -->' in summary
        assert summary.endswith('<!-- gatekeeper-refs:["head"] -->')
        assert '<!-- gatekeeper-refs-complete -->' not in summary

    def test_distinct_workflow_ids_with_same_path_cannot_hide_failure(self):
        runs = [{'id': ident, 'workflow_id': ident, 'head_sha': 'head', 'event': 'pull_request',
                 'head_branch': 'feature', 'path': '.github/workflows/ci.yml',
                 'status': 'completed', 'conclusion': conclusion}
                for ident, conclusion in ((1, 'failure'), (2, 'success'))]
        with patch.object(pr_gatekeeper, '_get', return_value={'workflow_runs': runs, 'total_count': 2}):
            checks, _ = pr_gatekeeper.collect_workflow_checks('org/repo', 'head', 'token')
        assert pr_gatekeeper.evaluate(checks, EMPTY_STATUSES, [])[:2] == ('completed', 'failure')
