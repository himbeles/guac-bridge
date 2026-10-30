"""Experimental sender only: same framing, eight unacknowledged input blobs.

Unlike production send_input, keep the window across event boundaries. This is
a benchmark prototype, not a supported client option or a protocol change.
"""
import collections
import base64

from guac_bridge.client import Bridge
from guac_bridge.ssh_client import SSHBridge


class PipelinedInput:
    async def send_event(self, event):
        await super().send_event(event)
        if event.get('type') == 'shutdown':
            # Teardown must not close the tunnel before queued shutdown bytes
            # reach the terminal. Preserve the production shutdown guarantee.
            async with self.input_lock:
                for future in getattr(self, 'pending_input', ()):
                    await self.wait_for(future, self.args.connect_timeout)

    def input_ack_done(self, future):
        # Observe failures even when no later send needs to wait on this ACK.
        if not future.cancelled() and future.exception() is not None:
            self.fail('Terminal input rejected; restart the bridge')

    async def send_input(self, data):
        async with self.input_lock:
            if not hasattr(self, 'pending_input'):
                self.pending_input = collections.deque()
            pending = self.pending_input
            try:
                for offset in range(0, len(data), 3072):
                    if self.failed.is_set():
                        raise RuntimeError('Input transport already failed')
                    while pending and pending[0].done():
                        pending.popleft().result()
                    if len(pending) >= 8:
                        await self.wait_for(pending.popleft(), self.args.connect_timeout)
                    future = self.new_ack()
                    future.add_done_callback(self.input_ack_done)
                    pending.append(future)
                    await self.tunnel.send('blob', self.input_stream,
                                           base64.b64encode(data[offset:offset + 3072]).decode())
            except BaseException:
                self.fail('Terminal input failed; restart the bridge')
                raise

    async def close(self):
        try:
            await super().close()
        finally:
            for future in getattr(self, 'pending_input', ()):
                if not future.done():
                    future.cancel()
            if hasattr(self, 'pending_input'):
                self.pending_input.clear()


class PipelinedBridge(PipelinedInput, Bridge):
    pass


class PipelinedSSHBridge(PipelinedInput, SSHBridge):
    pass
