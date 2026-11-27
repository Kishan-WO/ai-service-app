# Secure URL Fetching & SSRF Protection

**Source file reference:** [`src/ai_service/services/url_fetch.py:203-258`](../../src/ai_service/services/url_fetch.py#L203-L258)

When background workers download files from user-supplied URLs (e.g. `POST /api/v1/ingest`), the application faces critical security risks:
1. **Server-Side Request Forgery (SSRF):** Accessing internal resources (`http://localhost:5433`, `http://127.0.0.1:6379`, or cloud metadata `http://169.254.169.254`).
2. **DNS Rebinding:** A domain resolves to a public IP during checking, but resolves to `127.0.0.1` when the HTTP connection is made milliseconds later.
3. **Decompression / Zip Bombs:** A 5 KB compressed response that unpacks to 50 GB in RAM.

`fetch_url` implements an end-to-end hardened HTTP download pipeline to neutralize all of these attacks.

---

## 1. Code Walkthrough (`fetch_url`)

```python
async def fetch_url(
    url: str,
    *,
    max_bytes: int,
    resolve: Resolver = system_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
    total_seconds: float = TOTAL_SECONDS,
) -> FetchedFile:
```

### A. Isolated Client (`trust_env=False`)
- `parse_url(url)`: Enforces `http` or `https`, standard ports (80 or 443), length $\le 2048$, and rejects embedded credentials (`http://user:pass@host`).
- `httpx.AsyncClient(trust_env=False)`: Ignores system environment proxy variables (such as `HTTP_PROXY`), preventing requests from being routed through untrusted proxies that perform unvalidated DNS lookups.

### B. Public IP Enforcement & IP Pinning (Defeating DNS Rebinding)
```python
for _ in range(MAX_REDIRECTS + 1):
    address = await checked_address(current, resolve)
    response = await client.send(
        _pinned_request(current, address), stream=True, follow_redirects=False
    )
```
1. **`checked_address(current, resolve)`**: Resolves hostname to all IP addresses. Every resolved IP must be public (`is_public_address`). Private (`10.0.0.0/8`, `192.168.0.0/16`), loopback (`127.0.0.1`, `::1`), link-local, and NAT64 addresses are instantly rejected (`BlockedURLError`).
2. **IP Pinning (`_pinned_request`)**:
   - The TCP connection connects directly to the validated IP `address` (`url.copy_with(host=address)`).
   - The HTTP `Host` header and TLS SNI extension preserve the original hostname.
   - **Result:** DNS rebinding is completely impossible because the connection goes directly to the pre-checked IP without re-resolving DNS.
3. **`follow_redirects=False`**: Automatic redirects are disabled.

### C. Secure Manual Redirects
```python
if response.status_code in _REDIRECTS:
    location = response.headers.get("location")
    if not location:
        raise PermanentFetchError("The server sent a broken redirect.")
    current = parse_url(str(current.join(location)))
    continue
```
- Allows up to `MAX_REDIRECTS = 5`.
- **Crucial security guarantee:** Each redirect target loops back and undergoes the exact same DNS resolution and public IP verification. An attacker cannot use a public URL to redirect internally to `http://169.254.169.254`.

### D. Error Classification (`Transient` vs `Permanent`)
- **`TransientFetchError`** (HTTP 429 rate limit, 5xx server errors, network timeout): Temporary failure; the background worker queue (ARQ) can retry this job later.
- **`PermanentFetchError`** (HTTP 400, 403, 404): Fatal failure; the job stops immediately without retrying.

### E. Streaming & Decompression Bomb Protection
```python
data = await _read(response, max_bytes)
```
- **Rejects compression:** Rejects `content-encoding != "identity"`, defeating gzip/zip decompression bombs.
- **Raw byte streaming:** Uses `response.aiter_raw()` and aborts immediately if bytes exceed `max_bytes`, ignoring any forged or missing `Content-Length` headers.
- **File kind validation:** Rejects generic HTML pages (`text/html`), allowing only supported RAG document types (`.pdf`, `.md`, `.txt`), and validates PDF magic bytes (`%PDF-`).

### F. Privacy in Logs
- Logs only `current.host` (e.g. `example.com`), never full URLs or query strings, protecting user secrets and access tokens.
