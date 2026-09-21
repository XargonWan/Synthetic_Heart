# Ready-to-file bug report: unauthenticated requests return 500 when the client's PTR record is not valid UTF-8

Save this as-is into a new issue on `home-assistant/core`. Suggested title:

**`http`: every unauthenticated request returns 500 instead of 401 when `gethostbyaddr()` on the client IP raises `UnicodeDecodeError`**

---

## Environment

- Home Assistant Core **2026.8.3**, container install (`ghcr.io/home-assistant/home-assistant:stable`), Python **3.14**, aiohttp 3.14.x.
- Reverse-DNS answers on this LAN are produced by the ISP router (an all-in-one gateway that also does DHCP). The router answers PTR queries for its DHCP clients with a **malformed name** (details below).
- No custom components installed; `configuration.yaml` has no `http:` block.

## Description

Requests that fail authentication are supposed to answer `401 Unauthorized`. When the client's
reverse DNS answer is not decodable as UTF-8, the request instead answers **`500 Internal Server
Error`**, and nothing about the failed authentication is logged or counted.

Reproduced for every endpoint that requires auth (`GET /api/`, `GET /api/config`, `GET
/api/states`, with no `Authorization` header, and with an invalid one). Public endpoints are
unaffected: `GET /`, `GET /manifest.json`, `GET /auth/providers` all return 200, and the WebSocket
API authenticates normally. This makes a plain "did my token arrive correctly?" check look like a
server failure.

## Steps to reproduce

1. On a network whose reverse DNS for the client address returns a name containing invalid UTF-8
   (in our case the router answers PTR for `192.168.1.69` with a single 4-byte label
   `40 8a ed be`), request any auth-required endpoint without a token:
   `curl -i http://<ha>:8123/api/`
2. Observe `HTTP/1.1 500 Internal Server Error` with body `500 Internal Server Error / Server got
   itself in trouble`, instead of `401`.

Same network, minimal Python reproduction of the failing lookup (inside the HA container):

```
$ docker exec Home-Assistant-Container python -c "import socket; socket.gethostbyaddr('192.168.1.69')"
UnicodeDecodeError: 'utf-8' codec can't decode byte 0x8a in position 1: invalid start byte
```

## Traceback (from `/config/home-assistant.log`, verbatim)

```
2026-09-21 11:54:39.125 ERROR (MainThread) [aiohttp.server] Error handling request from 192.168.1.69
Traceback (most recent call last):
  File "/usr/src/homeassistant/homeassistant/components/http/ban.py", line 88, in ban_middleware
    return await handler(request)
  File "/usr/src/homeassistant/homeassistant/components/http/auth.py", line 261, in auth_middleware
    return await handler(request)
  File "/usr/src/homeassistant/homeassistant/components/http/headers.py", line 39, in headers_middleware
    response = await handler(request)
  ...
  File "/usr/src/homeassistant/homeassistant/components/http/request_context.py", line 24, in request_context_middleware
    return await handler(request)
  File "/usr/src/homeassistant/homeassistant/components/http/ban.py", line 90, in ban_middleware
    await process_wrong_login(request)
  File "/usr/src/homeassistant/homeassistant/components/http/ban.py", line 123, in process_wrong_login
    remote_host, _, _ = await hass.async_add_executor_job(
        gethostbyaddr, request.remote
    )
  File "/usr/local/lib/python3.14/concurrent/futures/thread.py", line 86, in run
    result = ctx.run(self.task)
  File "/usr/local/lib/python3.14/concurrent/futures/thread.py", line 73, in run
    return fn(*args, **kwargs)
UnicodeDecodeError: 'utf-8' codec can't decode byte 0x8a in position 1: invalid start byte
```

## Root cause

`homeassistant/components/http/ban.py::ban_middleware` treats a failed authentication as a wrong
login and calls `process_wrong_login(request)` (line 90). That function resolves the client
hostname for its log message:

```python
async def process_wrong_login(request: Request) -> None:
    hass = request.app[KEY_HASS]
    assert request.remote
    remote_addr = ip_address(request.remote)
    remote_host = request.remote
    with suppress(herror):                     # <-- only herror is tolerated
        remote_host, _, _ = await hass.async_add_executor_job(
            gethostbyaddr, request.remote
        )
```

`socket.gethostbyaddr()` decodes the PTR target as strict UTF-8. A PTR record whose name is not
valid UTF-8 therefore raises `UnicodeDecodeError` (a `ValueError`, not a `herror`), which the
`suppress(herror)` does not cover. Because this happens inside an aiohttp middleware, the intended
`401` never reaches the client: the request ends as a `500`.

Two aggravating consequences, both silent:

1. `process_wrong_login` raises **before** its own `_LOGGER.warning("Login attempt or request with
   invalid authentication from ...")` line and **before** the failed-attempt counter is
   incremented, so:
   - no failed-login warning is ever written,
   - the IP-ban/failed-login counter never advances, i.e. brute-force protection is silently
     disabled for as long as such a PTR answer is in play.
2. Every unauthenticated request writes a full traceback to the log (log noise that looks like an
   internal error rather than a client error).

The malformed answer is real, and it is the router's, not HA's (AdGuard Home's query log records the
raw upstream response for `69.1.168.192.in-addr.arpa` with `"Upstream":"192.168.1.1:53"`; decoding
the logged `Answer` field gives a PTR record whose RDATA is a single label of length 4,
`40 8a ed be`). That said, a client-controlled DNS answer must never be able to turn a 401 into a
500, and PTR data is arbitrary bytes by nature.

## Suggested fix

Tolerate any failure of this lookup, since it only produces a cosmetic hostname for a log line:

```python
    with suppress(herror, UnicodeDecodeError, OSError):
        remote_host, _, _ = await hass.async_add_executor_job(
            gethostbyaddr, request.remote
        )
```

or equivalently, narrowing by type:

```python
    try:
        remote_host, _, _ = await hass.async_add_executor_job(
            gethostbyaddr, request.remote
        )
    except (herror, UnicodeDecodeError, OSError) as err:
        _LOGGER.debug("Reverse lookup for %s failed: %s", remote_addr, err)
```

(`UnicodeDecodeError` subclasses `ValueError`, so `except (herror, ValueError)` also covers it,
though naming the exception is clearer.) A test can be added cheaply: patch
`socket.gethostbyaddr` (or the exec_job call) to raise `UnicodeDecodeError`, then assert that a
request without credentials still answers `401` and that the failure is logged as a failed login.

## Workaround for anyone hitting this

Make sure the HA host's reverse lookup for LAN clients either resolves cleanly or fails with
`herror`:

- point the HA container/host DNS at a resolver that does **not** forward private PTR queries to the
  broken upstream (we disabled "private reverse DNS servers" in AdGuard Home, after which it returns
  `NXDOMAIN` and HA behaves), or
- add the client addresses to the HA host's `/etc/hosts`, which `gethostbyaddr()` consults before
  DNS, or
- fix/disable the PTR answers on the router itself.

Diagnosis one-liner: an unauthenticated `curl -o /dev/null -w '%{http_code}' http://<ha>:8123/api/`
must print `401`; if it prints `500`, grep the HA log for `process_wrong_login`.
