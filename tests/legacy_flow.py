"""Emulate the pre-0.6 wire contract, with no flow capability or request fields."""
class LegacyFlow:
    def dispatch(self, event):
        if event.get('type') == 'ready':
            event = dict(event, capabilities=['cleanup-ack'])
        return super().dispatch(event)

    async def send_event(self, event):
        event = {k: v for k, v in event.items() if k not in ('window', 'chunk')}
        return await super().send_event(event)
