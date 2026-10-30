"""Safety checks for the opt-in benchmark sender, not a production change."""
import asyncio
import base64
import json
from types import SimpleNamespace
import unittest

from benchmarks.pipeline import PipelinedBridge
from guac_bridge.errors import BridgeError


class AckTunnel:
    def __init__(self, bridge, delay=.01, reject=False):
        self.bridge, self.delay, self.reject = bridge, delay, reject
        self.inflight = self.peak = 0
        self.data = bytearray()
        self.handles = []
        self.closed = False
        self.inflight_at_close = None

    async def send(self, *args):
        if args[0] == 'blob':
            future = self.bridge.acks.get_nowait()
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            self.data.extend(base64.b64decode(args[2], validate=True))
            self.handles.append(asyncio.get_running_loop().call_later(self.delay, self.ack, future))

    def ack(self, future):
        self.inflight -= 1
        if not future.done():
            if self.reject:
                future.set_exception(BridgeError('Rejected'))
            else:
                future.set_result(None)

    async def close(self):
        self.inflight_at_close = self.inflight
        self.closed = True


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = PipelinedBridge(SimpleNamespace(connect_timeout=1), None, '', '1', 'c', 'mysql', 'token')
        self.tunnel = self.bridge.tunnel = AckTunnel(self.bridge)

    async def asyncTearDown(self):
        await self.bridge.close()
        for handle in self.tunnel.handles:
            handle.cancel()

    async def test_small_events_share_window_without_per_event_wait(self):
        self.tunnel.delay = .2
        await self.bridge.send_event({'type': 'credit', 'id': 'a'})
        await self.bridge.send_event({'type': 'credit', 'id': 'b'})
        self.assertEqual(self.tunnel.inflight, 2)

    async def test_eight_blob_bound_and_concurrent_frames_remain_intact(self):
        events = [{'type': 'data', 'id': str(i), 'body': 'x' * 9000} for i in range(16)]
        await asyncio.gather(*(self.bridge.send_event(event) for event in events))
        lines = self.tunnel.data.splitlines()
        received = [json.loads(base64.b64decode(line, validate=True)) for line in lines]
        self.assertEqual(received, events)
        self.assertEqual(self.tunnel.peak, 8)

    async def test_rejected_last_ack_fails_transport_without_another_send(self):
        self.tunnel.reject = True
        await self.bridge.send_event({'type': 'credit', 'id': 'a'})
        await asyncio.wait_for(self.bridge.failed.wait(), 1)
        self.assertIn('input rejected', self.bridge.failure)

    async def test_cancelled_partial_frame_marks_transport_failed(self):
        self.tunnel.delay = 10
        task = asyncio.create_task(self.bridge.send_input(b'x' * (3072 * 20)))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.bridge.failed.is_set())

    async def test_shutdown_drains_before_tunnel_close(self):
        self.bridge.ready.set()
        await self.bridge.send_event({'type': 'credit', 'id': 'a'})
        await self.bridge.close()
        self.assertEqual(self.tunnel.inflight_at_close, 0)
