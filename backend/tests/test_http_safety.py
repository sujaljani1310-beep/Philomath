"""Credential-bearing redirect and stateless cookie regressions, offline."""

import concurrent.futures
import threading
import unittest

import httpx

from app.http_safety import reject_unsafe_redirects, stateless_cookies


class HttpSafetyTests(unittest.TestCase):
    def test_cross_origin_and_downgrade_redirects_never_receive_credentials_or_body(self):
        for status in (301, 302, 303, 307, 308):
            for destination in (
                "https://other.invalid/target",
                "http://upstream.invalid/target",
                "https://upstream.invalid:8443/target",
                "https://new:credentials@upstream.invalid/target",
                "//other.invalid/target",
            ):
                with self.subTest(status=status, destination=destination):
                    requests = []

                    def response(request):
                        requests.append(request)
                        return httpx.Response(status, headers={"location": destination}, request=request)

                    with httpx.Client(
                        transport=httpx.MockTransport(response), follow_redirects=True,
                        event_hooks={"response": [reject_unsafe_redirects]},
                    ) as client:
                        with self.assertRaisesRegex(httpx.ProtocolError, "Refusing an unsafe upstream redirect"):
                            client.post(
                                "https://upstream.invalid/start",
                                headers={"authorization": "Bearer synthetic", "apikey": "synthetic-service", "x-goog-api-key": "synthetic-provider"},
                                content=b"private synthetic body",
                            )
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].url.host, "upstream.invalid")

    def test_same_origin_https_307_and_308_preserve_method_body_and_headers(self):
        for status in (307, 308):
            with self.subTest(status=status):
                requests = []

                def response(request):
                    requests.append(request)
                    return httpx.Response(
                        status if len(requests) == 1 else 200,
                        headers={"location": "/target"} if len(requests) == 1 else {},
                        request=request,
                    )

                with httpx.Client(
                    transport=httpx.MockTransport(response), follow_redirects=True,
                    event_hooks={"response": [reject_unsafe_redirects]},
                ) as client:
                    result = client.post("https://upstream.invalid/start", headers={"apikey": "synthetic"}, content=b"synthetic body")
                self.assertEqual(result.status_code, 200)
                self.assertEqual([request.method for request in requests], ["POST", "POST"])
                self.assertEqual(requests[1].content, b"synthetic body")
                self.assertEqual(requests[1].headers["apikey"], "synthetic")

    def test_configured_http_gateway_direct_request_allowed_but_redirect_rejected(self):
        with httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
            follow_redirects=True, event_hooks={"response": [reject_unsafe_redirects]},
        ) as client:
            self.assertEqual(client.post("http://configured-gateway.invalid", content=b"test").status_code, 200)
        request = httpx.Request("POST", "http://configured-gateway.invalid/start")
        with self.assertRaises(httpx.ProtocolError):
            reject_unsafe_redirects(httpx.Response(307, headers={"location": "/target"}, request=request))

    def test_malformed_location_is_rejected_without_echoing_sensitive_value(self):
        request = httpx.Request("POST", "https://upstream.invalid")
        with self.assertRaises(httpx.ProtocolError) as failure:
            reject_unsafe_redirects(httpx.Response(
                307, headers={"location": "https://synthetic-secret@[broken"}, request=request,
            ))
        self.assertNotIn("synthetic-secret", str(failure.exception))

    def test_stateless_client_rejects_cookies_between_overlapping_users(self):
        recorded = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def response(request):
            with lock:
                recorded.append((request.headers["authorization"], request.headers.get("cookie")))
            return httpx.Response(
                200, headers={"set-cookie": f"session={request.headers['authorization']}; Path=/"}, request=request,
            )

        with httpx.Client(cookies=stateless_cookies(), transport=httpx.MockTransport(response)) as client:
            def user(index):
                barrier.wait(timeout=5)
                for path in ("/auth/v1/user", "/rest/v1/conversations"):
                    client.get("https://synthetic.invalid" + path, headers={"authorization": f"user-{index}"})

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(user, range(8)))
            self.assertEqual(len(client.cookies), 0)
        self.assertEqual(len(recorded), 16)
        self.assertTrue(all(cookie is None for _, cookie in recorded))

    def test_stateless_policy_never_sends_explicitly_preloaded_cookie(self):
        requests = []

        def response(request):
            requests.append(request)
            return httpx.Response(200, request=request)

        with httpx.Client(cookies=stateless_cookies(), transport=httpx.MockTransport(response)) as client:
            client.cookies.set("preloaded", "synthetic", domain="synthetic.invalid", path="/")
            client.get("https://synthetic.invalid/test")
        self.assertNotIn("cookie", requests[0].headers)


if __name__ == "__main__":
    unittest.main()
