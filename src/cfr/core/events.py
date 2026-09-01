from dataclasses import dataclass
from enum import StrEnum
import hashlib
class EventSource(StrEnum): FEISHU='feishu'; DESKTOP='desktop'; CODEX='codex'; CFR='cfr'; UNKNOWN='unknown'
@dataclass(frozen=True)
class CfrEvent:
    thread_id:str|None; turn_id:str|None; item_id:str|None; event_type:str; source:EventSource; text:str|None; timestamp:float|None; raw_type:str|None
    @property
    def key(self):
        base='|'.join(str(x or '') for x in (self.thread_id,self.turn_id,self.item_id,self.event_type))
        return base if any((self.thread_id,self.turn_id,self.item_id)) else hashlib.sha256((base+'|'+str(self.text)).encode()).hexdigest()
