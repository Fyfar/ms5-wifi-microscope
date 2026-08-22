#!/usr/bin/env python3
# One-shot check that MS5Client tells "OS refused the packet" (a local
# permission/firewall block) apart from plain silence (wrong WiFi/camera
# off) -- the ambiguity that turned one such incident into a long session.
from ms5_viewer import MS5Client


class FakeSocket:
    def __init__(self, err):
        self.err = err

    def sendto(self, *a):
        raise self.err

    def settimeout(self, *a):
        pass


def test_req_captures_os_error_for_connect_to_report():
    c = MS5Client()
    err = OSError()
    err.errno = 65  # EHOSTUNREACH, what a Local-Network-permission block looks like
    resp = c._req(FakeSocket(err), 10005, 1, 1, retries=3, timeout=0.1)
    assert resp is None
    assert c._last_err is err
    print("ok: _req captures OSError -> connect() can report a specific cause")


def test_req_leaves_last_err_none_on_plain_timeout():
    import socket

    class TimeoutSocket:
        def sendto(self, *a):
            raise socket.timeout()

        def settimeout(self, *a):
            pass

    c = MS5Client()
    resp = c._req(TimeoutSocket(), 10005, 1, 1, retries=2, timeout=0.1)
    assert resp is None
    assert c._last_err is None
    print("ok: plain timeouts stay silent -> connect() falls back to the WiFi-check message")


if __name__ == "__main__":
    test_req_captures_os_error_for_connect_to_report()
    test_req_leaves_last_err_none_on_plain_timeout()
