"""Bounded merchant responses, including decompressed/chunked payloads."""

import json


async def request_json(client, method, url, **kwargs):
    async with client.stream(method, url, **kwargs) as response:
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Merchant HTTP {response.status_code}; check method and local permissions")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > 2_000_000:
                raise ValueError("Merchant response exceeds size limit")
            data.extend(chunk)
        try:
            return json.loads(data)
        except ValueError:
            raise ValueError("Merchant response is not valid JSON") from None
