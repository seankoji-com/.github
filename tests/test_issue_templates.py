"""Keep the bug and incident severity intake on the shared SEV scale."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / '.github' / 'ISSUE_TEMPLATE'
SEVERITY_LEVELS = ('SEV1', 'SEV2', 'SEV3')


def fields(name):
    return yaml.safe_load((TEMPLATES / name).read_text())['body']


def dropdown(name, field_id):
    matches = [field for field in fields(name)
               if field.get('type') == 'dropdown' and field.get('id') == field_id]
    if len(matches) != 1:
        raise AssertionError(f'{name}: expected one {field_id} dropdown, found {len(matches)}')
    return matches[0]


def severity_options(name):
    return dropdown(name, 'severity')['attributes']['options']


class SeverityIntakeTests(unittest.TestCase):
    def test_bug_and_incident_offer_the_same_severity_options(self):
        incident = severity_options('incident.yml')
        bug = severity_options('bug.yml')
        self.assertEqual(bug, incident)
        self.assertEqual([option.split(' - ', 1)[0] for option in incident],
                         list(SEVERITY_LEVELS))

    def test_severity_wording_covers_impact_and_release_treatment(self):
        wording = ' '.join(severity_options('incident.yml'))
        self.assertIn('essential functionality unavailable', wording)
        self.assertIn('serious security exposure', wording)
        self.assertIn('irreversible data loss', wording)
        self.assertIn('workable alternative or workaround', wording)
        self.assertIn('low or no user impact', wording)

    def test_bug_keeps_priority_separate_from_severity(self):
        ids = [field.get('id') for field in fields('bug.yml')]
        self.assertIn('priority', ids)
        self.assertIn('severity', ids)
        self.assertLess(ids.index('priority'), ids.index('severity'))

    def test_severity_is_required_for_incidents(self):
        self.assertTrue(dropdown('incident.yml', 'severity')['validations']['required'])


if __name__ == '__main__':
    unittest.main()
