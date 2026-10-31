"""Production ACK-window bounds, failure propagation, and shutdown regressions."""
import asyncio
import base64
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from guac_bridge.client import Bridge
from guac_bridge.client import instruction
from guac_bridge.errors import BridgeError
from guac_bridge.ssh_client import SSHBridge


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


class ReaderTunnel(AckTunnel):
    def __init__(self, bridge, delay=.02):
        super().__init__(bridge, delay=delay)
        self.incoming = asyncio.Queue()

    async def messages(self):
        while True:
            message = await self.incoming.get()
            if message is None:
                return
            yield message

    async def send(self, *args):
        if args[0] != 'blob':
            return
        self.inflight += 1
        payload = base64.b64decode(args[2])
        event = json.loads(base64.b64decode(payload.strip()))
        if event['type'] == 'shutdown':
            self.incoming.put_nowait(instruction('end', '1'))
        self.handles.append(asyncio.get_running_loop().call_later(
            self.delay, self.incoming.put_nowait, instruction('ack', '0', 'OK', '0')))


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = Bridge(SimpleNamespace(connect_timeout=1), None, '', '1', 'c', 'mysql', 'token')
        self.tunnel = self.bridge.tunnel = AckTunnel(self.bridge)

    async def asyncTearDown(self):
        if not self.tunnel.closed:
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

    async def test_negotiated_32_blob_bound_preserves_concurrent_frames(self):
        self.bridge.input_window = 32
        events = [{'type': 'data', 'id': str(i), 'body': 'x' * 35000} for i in range(16)]
        await asyncio.gather(*(self.bridge.send_event(event) for event in events))
        received = [json.loads(base64.b64decode(line, validate=True))
                    for line in self.tunnel.data.splitlines()]
        self.assertEqual(received, events)
        self.assertEqual(self.tunnel.peak, 32)

    async def test_rejected_last_ack_fails_transport_without_another_send(self):
        self.tunnel.reject = True
        await self.bridge.send_event({'type': 'credit', 'id': 'a'})
        await asyncio.wait_for(self.bridge.failed.wait(), 1)
        self.assertEqual('Rejected', self.bridge.failure)

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

    async def test_missing_last_ack_fails_without_another_send(self):
        self.bridge.args.connect_timeout = .03
        self.tunnel.delay = 10
        await self.bridge.send_event({'type': 'credit', 'id': 'a'})
        await asyncio.wait_for(self.bridge.failed.wait(), .5)
        self.assertIn('acknowledgement timed out', self.bridge.failure)

    async def test_close_cancels_ack_deadlines_and_releases_futures(self):
        self.tunnel.delay = 10
        await self.bridge.send_event({'type': 'credit', 'id': 'a'})
        pending = list(self.bridge.pending_input)
        await self.bridge.close()
        self.assertFalse(self.bridge.ack_deadlines)
        self.assertFalse(self.bridge.pending_input)
        self.assertTrue(self.bridge.acks.empty())
        self.assertTrue(all(future.done() for future in pending))

    async def test_output_end_during_shutdown_does_not_drop_late_input_ack(self):
        self.tunnel = self.bridge.tunnel = ReaderTunnel(self.bridge)
        self.bridge.output_stream = 1
        self.bridge.ready.set()
        self.bridge.reader = asyncio.create_task(self.bridge.read_loop())
        await self.bridge.send_event({'type': 'credit', 'id': 'a'})
        await asyncio.wait_for(self.bridge.close(), .5)
        self.assertFalse(self.bridge.failed.is_set())
        self.assertFalse(self.bridge.pending_input)
        self.assertFalse(self.bridge.ack_deadlines)

    async def test_disconnect_resolves_all_pending_input_futures(self):
        self.tunnel = self.bridge.tunnel = ReaderTunnel(self.bridge, delay=10)
        self.bridge.reader = asyncio.create_task(self.bridge.read_loop())
        await self.bridge.send_event({'type': 'credit', 'id': 'a'})
        pending = list(self.bridge.pending_input)
        self.tunnel.incoming.put_nowait(None)
        await asyncio.wait_for(self.bridge.reader, .5)
        self.assertTrue(self.bridge.failed.is_set())
        self.assertTrue(all(future.done() for future in pending))

    async def test_ssh_shutdown_without_ack_is_bounded(self):
        bridge = SSHBridge(SimpleNamespace(connect_timeout=30), None, '', '1', 'c', 'mysql', 'token')
        tunnel = bridge.tunnel = AckTunnel(bridge, delay=10)
        bridge.ready.set()
        original_wait = asyncio.wait_for
        async def short_wait(awaitable, timeout):
            return await original_wait(awaitable, min(timeout, .05))
        try:
            with patch('guac_bridge.client.asyncio.wait_for', short_wait):
                await original_wait(bridge.close(), .5)
            self.assertTrue(tunnel.closed)
            self.assertFalse(bridge.ack_deadlines)
        finally:
            for handle in tunnel.handles:
                handle.cancel()
