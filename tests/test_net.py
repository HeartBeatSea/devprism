"""网络出口工具测试：代理地址规范化与端口探测。"""

from __future__ import annotations

import unittest

from devprism import net


class TestNormalize(unittest.TestCase):
    def test_adds_scheme(self):
        self.assertEqual(net.normalize("127.0.0.1:7897"), "http://127.0.0.1:7897")

    def test_keeps_existing_scheme(self):
        self.assertEqual(net.normalize("socks5://127.0.0.1:1080"), "socks5://127.0.0.1:1080")

    def test_empty_values(self):
        self.assertIsNone(net.normalize(""))
        self.assertIsNone(net.normalize("   "))
        self.assertIsNone(net.normalize(None))


class TestBuildOpener(unittest.TestCase):
    def test_builds_without_proxy(self):
        self.assertIsNotNone(net.build_opener(None))

    def test_builds_with_proxy(self):
        self.assertIsNotNone(net.build_opener("127.0.0.1:7897"))


class TestProbe(unittest.TestCase):
    def test_closed_port_is_unreachable(self):
        reachable, detail = net.probe("http://127.0.0.1:1", timeout=1.0)

        self.assertFalse(reachable)
        self.assertIn("无法连接", detail)

    def test_bad_address_is_reported(self):
        reachable, detail = net.probe("http://", timeout=1.0)

        self.assertFalse(reachable)
        self.assertIn("无法解析", detail)


if __name__ == "__main__":
    unittest.main()
