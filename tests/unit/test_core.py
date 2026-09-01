import json,tempfile,unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from cfr.core.events import CfrEvent,EventSource
from cfr.codex.rollout import RolloutWatcher
from cfr.codex.lease import WriterLease,LeaseState
from cfr.core.models import StructuredError
from cfr.storage.db import BindingStore
class CoreTests(unittest.TestCase):
 def test_event_key(self):
  self.assertEqual(CfrEvent('t','u','i','x',EventSource.CFR,None,None,None).key,'t|u|i|x')
 def test_lease(self):
  l=WriterLease();l.acquire();self.assertEqual(l.state,LeaseState.CFR_ACTIVE);l.release()
 def test_rollout_offset(self):
  with tempfile.TemporaryDirectory()as d:
   p=Path(d)/'x.jsonl';p.write_text(json.dumps({'type':'event_msg','payload':{'type':'user_message','message':'hello','turn_id':'t'}})+'\n',encoding='utf8');w=RolloutWatcher(p);self.assertEqual(len(w.poll()),1);self.assertEqual(w.poll(),[])
 def test_dedupe(self):
  with tempfile.TemporaryDirectory()as d:
   s=BindingStore(Path(d)/'x.db');self.assertFalse(s.seen('a'));self.assertTrue(s.seen('a'));s.close()
if __name__=='__main__':unittest.main()
