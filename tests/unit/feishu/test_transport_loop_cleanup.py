import asyncio
import unittest

from cfr.feishu.transport import ChannelFeishuTransport


class TransportLoopCleanupTests(unittest.TestCase):
    def test_guard_closes_owned_loop_and_cancels_pending_tasks(self):
        loop = asyncio.new_event_loop()
        pending = loop.create_task(asyncio.sleep(60))
        transport = ChannelFeishuTransport.__new__(ChannelFeishuTransport)
        transport._loop = loop
        transport._owned_loop = loop
        transport._run_loop = lambda: None
        transport._run_loop_guarded()
        self.assertTrue(loop.is_closed())
        self.assertTrue(pending.cancelled())
        self.assertIsNone(transport._loop)
        self.assertIsNone(transport._owned_loop)


if __name__ == '__main__':
    unittest.main()
