from cfr.core.events import CfrEvent


class EventProjector:
    """Project each logical event once, regardless of its observation source."""

    def __init__(self, store):
        self.store = store

    def project_event(self, event: CfrEvent):
        if self.store.seen_event(event.key):
            return None
        return event
