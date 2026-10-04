import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('usage.py')

def event(t, sender='Agent', **extra):
    return dict(insertedAt='2026-10-04T20:00:'+t+'Z', senderType=sender,
                senderName='reviewer' if sender=='Agent' else 'Carlos',
                messageType='text', content='fixture', metadata={}, **extra)

class ExportAuditTests(unittest.TestCase):
    def run_script(self, messages):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'room.json'
            p.write_text(json.dumps({'messages':messages}),encoding='utf-8')
            return subprocess.run([sys.executable,str(SCRIPT),str(p)],capture_output=True,text=True)

    def test_official_export_measures_from_human_not_setup_and_does_not_invent_tokens(self):
        p=self.run_script([event('05.500'),event('00.000'),event('02.000','User')])
        self.assertEqual(p.returncode,0,p.stderr)
        r=json.loads(p.stdout)
        self.assertEqual(r['message_count'],3)
        self.assertEqual(r['human_messages_after_dispatch'],0)
        self.assertEqual(r['dispatch_to_last_event_seconds'],3.5)
        self.assertEqual(r['room_interval_seconds'],5.5)
        self.assertEqual(r['usage']['status'],'unavailable')
        self.assertNotIn('output_tokens',r['usage'])

    def test_legacy_api_schema_remains_readable(self):
        old=[{'inserted_at':'2026-10-04T20:00:02Z','sender_type':'User',
              'sender_name':'Carlos','message_type':'text','content':'task','metadata':{}},
             {'inserted_at':'2026-10-04T20:00:05Z','sender_type':'Agent',
              'sender_name':'coordinator','message_type':'text','content':'done','metadata':{}}]
        p=self.run_script(old)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertTrue(p.stdout.lstrip().startswith('{'), 'expected structured timing evidence, got: '+p.stdout)
        self.assertEqual(json.loads(p.stdout)['dispatch_to_last_event_seconds'],3)

    def test_a_second_human_message_is_reported(self):
        p=self.run_script([event('02','User'),event('03','User'),event('05')])
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(json.loads(p.stdout)['human_messages_after_dispatch'],1)

    def test_no_human_dispatch_fails_instead_of_claiming_autonomy(self):
        p=self.run_script([event('02')])
        self.assertNotEqual(p.returncode,0)
        self.assertIn('human dispatch',p.stderr.lower())

    def test_usage_events_are_not_summed_without_proven_counter_semantics(self):
        a=event('05'); a['metadata']={'band_usage':{'output_tokens':999}}
        p=self.run_script([event('02','User'),a])
        self.assertEqual(p.returncode,0,p.stderr)
        u=json.loads(p.stdout)['usage']
        self.assertEqual(u['recorded_event_count'],1)
        self.assertEqual(u['status'],'unavailable')
        self.assertEqual(u['reason'],'counter_semantics_not_verified')

    def test_timezone_less_timestamp_is_rejected(self):
        a=event('02','User'); a['insertedAt']='2026-10-04T20:00:02'
        p=self.run_script([a,event('05')])
        self.assertNotEqual(p.returncode,0)
        self.assertIn('timezone',p.stderr.lower())

if __name__=='__main__': unittest.main()

