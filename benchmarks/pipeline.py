"""Compare the legacy per-event drain with the production pipelined sender.

The legacy subclass freezes the 0.4.2 send policy only. Both variants use the
current transport lifecycle and ACK error handling, the same wire format, and
the same server helpers. These classes are only used by the benchmark harness.
"""
import base64

from guac_bridge.client import Bridge
from guac_bridge.ssh_client import SSHBridge


class LegacyInput:
    async def send_input(self, data):
        async with self.input_lock:
            pending = []
            try:
                for offset in range(0, len(data), 3072):
                    pending.append(self.new_ack())
                    await self.tunnel.send('blob', self.input_stream,
                                           base64.b64encode(data[offset:offset + 3072]).decode())
                    if len(pending) >= 8:
                        await self.wait_for(pending.pop(0), self.args.connect_timeout)
                for future in pending:
                    await self.wait_for(future, self.args.connect_timeout)
            except BaseException:
                self.fail('Terminal input failed; restart the bridge')
                raise


class LegacyBridge(LegacyInput, Bridge):
    pass


class LegacySSHBridge(LegacyInput, SSHBridge):
    pass


PipelinedBridge = Bridge
PipelinedSSHBridge = SSHBridge
