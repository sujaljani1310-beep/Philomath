"""Bound provider response allocation before HTTPX/SDK decompression.

Tokens requested from a model do not bound a gateway's HTTP response. Validate
encoded bytes and bounded decompression incrementally before forwarding each
original wire chunk to HTTPX. The SDK still receives its normal decoded body.
"""

import zlib

import httpx


MAX_DECODED_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ENCODED_RESPONSE_BYTES = MAX_DECODED_RESPONSE_BYTES + 64 * 1024
_CHUNK_BYTES = 64 * 1024
_ERROR = "Provider response exceeds the safe response limits or uses invalid encoding."


class _BoundedResponseStream(httpx.SyncByteStream):
    def __init__(self, response, request, encoding, decoded_limit, encoded_limit):
        self._response = response
        self._request = request
        self._encoding = encoding
        self._decoded_limit = decoded_limit
        self._encoded_limit = encoded_limit
        self._closed = False

    def __iter__(self):
        encoded_bytes = decoded_bytes = 0
        decoder = (
            zlib.decompressobj(zlib.MAX_WBITS | 16)
            if self._encoding == "gzip"
            else zlib.decompressobj() if self._encoding == "deflate" else None
        )
        first_deflate = self._encoding == "deflate"
        try:
            for chunk in self._response.iter_raw(chunk_size=_CHUNK_BYTES):
                encoded_bytes += len(chunk)
                if encoded_bytes > self._encoded_limit:
                    raise httpx.ReadError(_ERROR, request=self._request)

                remaining = self._decoded_limit - decoded_bytes
                try:
                    try:
                        decoded = chunk if decoder is None else decoder.decompress(chunk, remaining + 1)
                    except zlib.error:
                        # Match HTTPX's first-decode fallback. Raw deflate can
                        # start with bytes that resemble a valid zlib header;
                        # inspecting that header alone rejects valid gateways.
                        if not first_deflate:
                            raise
                        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                        decoded = decoder.decompress(chunk, remaining + 1)
                except zlib.error:
                    raise httpx.ReadError(_ERROR, request=self._request) from None
                first_deflate = False
                decoded_bytes += len(decoded)
                if decoded_bytes > self._decoded_limit or (decoder is not None and decoder.unused_data):
                    raise httpx.ReadError(_ERROR, request=self._request)

                # Forward the exact wire bytes only after expansion is known
                # to fit. HTTPX retains its normal decoder and response shape.
                yield chunk

            if self._encoding != "identity" and (decoder is None or not decoder.eof):
                raise httpx.ReadError(_ERROR, request=self._request)
        finally:
            self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            self._response.close()


def bounded_provider_response(response: httpx.Response, request: httpx.Request) -> httpx.Response:
    """Transfer one response stream into a bounded wrapper, preserving ownership."""
    decoded_limit = MAX_DECODED_RESPONSE_BYTES
    encoded_limit = MAX_ENCODED_RESPONSE_BYTES
    try:
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"identity", "gzip", "deflate"}:
            raise httpx.ReadError(_ERROR, request=request)
        length = response.headers.get("content-length")
        if length is not None:
            # Native HTTPX validates wire framing too; keep custom transports
            # from defeating this early rejection with malformed metadata.
            if len(length) > 20 or not length.isascii() or not length.isdigit():
                raise httpx.ReadError(_ERROR, request=request)
            maximum = min(decoded_limit, encoded_limit) if encoding == "identity" else encoded_limit
            if int(length) > maximum:
                raise httpx.ReadError(_ERROR, request=request)
        if response.is_stream_consumed:
            # Mock/custom transports can supply already-decoded content. Do
            # not decode it a second time or destroy HTTPX's cached body.
            if len(response.content) > decoded_limit:
                raise httpx.ReadError(_ERROR, request=request)
            return response
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_BoundedResponseStream(response, request, encoding, decoded_limit, encoded_limit),
            extensions=response.extensions,
            request=request,
        )
    except BaseException:
        response.close()
        raise
