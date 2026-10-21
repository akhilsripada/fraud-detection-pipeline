"""The --share link: provider choice, URL parsing, and falling back when one provider fails."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))
import tunnel  # noqa: E402


class FakeProc:
    def __init__(self, lines, exits=False):
        self.stdout, self.exits, self.terminated = iter(lines), exits, False

    def poll(self):
        return 1 if self.exits else None

    def terminate(self):
        self.terminated = True


class TunnelTests(unittest.TestCase):
    def test_finds_each_providers_link(self):
        self.assertEqual(tunnel.find_url("INF |  https://quiet-river-ab12.trycloudflare.com  |", "cloudflared"),
                         "https://quiet-river-ab12.trycloudflare.com")
        self.assertEqual(tunnel.find_url("3f2a.lhr.life tunneled with tls termination, https://3f2a9c.lhr.life", "ssh"),
                         "https://3f2a9c.lhr.life")
        self.assertEqual(tunnel.find_url('{"event":"tcpip-forward","address":"9d1f7a2b3c4d5e.lhr.life","message":"..."}', "ssh"),
                         "https://9d1f7a2b3c4d5e.lhr.life")
        self.assertIsNone(tunnel.find_url("To set up custom domains, add an ssh key at https://admin.localhost.run/", "ssh"))
        self.assertIsNone(tunnel.find_url("connecting...", "ssh"))

    def test_uses_cloudflared_first_and_returns_its_link(self):
        procs = []
        def popen(cmd, **kw):
            procs.append(cmd)
            return FakeProc(["starting\n", "Your quick Tunnel: https://a-b-c.trycloudflare.com\n"])
        with mock.patch.object(tunnel, "available", return_value=["cloudflared", "ssh"]):
            proc, url, name = tunnel.start(8000, timeout=2, popen=popen)
        self.assertEqual((url, name), ("https://a-b-c.trycloudflare.com", "cloudflared"))
        self.assertIn("http://localhost:8000", procs[0])

    def test_falls_back_to_ssh_when_the_first_provider_fails(self):
        made = []
        def popen(cmd, **kw):
            p = FakeProc(["error: no network\n"], exits=True) if cmd[0] == "cloudflared" else \
                FakeProc(["x.lhr.life tunneled with tls termination, https://x1y2.lhr.life\n"])
            made.append(p)
            return p
        with mock.patch.object(tunnel, "available", return_value=["cloudflared", "ssh"]):
            _, url, name = tunnel.start(8000, timeout=2, popen=popen)
        self.assertEqual((url, name), ("https://x1y2.lhr.life", "ssh"))
        self.assertTrue(made[0].terminated)
        self.assertIn("80:localhost:8000", " ".join(tunnel.PROVIDERS["ssh"][1](8000)))

    def test_clear_error_when_nothing_is_installed(self):
        with mock.patch.object(tunnel, "available", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "brew install cloudflared"):
                tunnel.start(8000)


if __name__ == "__main__":
    unittest.main()
