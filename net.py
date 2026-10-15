"""A tiny HTTP client on the standard library."""
import json
import uuid
from urllib import error as urlerror
from urllib import request as urlrequest


def http(method, url, payload=None, headers=None, body=None, timeout=30):
    """Returns (status, parsed JSON, headers). Network failures come back as status 0."""
    hdrs = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    req = urlrequest.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as r:
            raw, status, rh = r.read(), r.status, r.headers
    except urlerror.HTTPError as e:
        raw, status, rh = e.read(), e.code, e.headers
    except (urlerror.URLError, TimeoutError, OSError) as e:
        return 0, {"error": {"message": str(getattr(e, "reason", e))}}, {}
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        parsed = {"error": {"message": raw[:300].decode(errors="replace")}}
    return status, parsed if isinstance(parsed, dict) else {"data": parsed}, dict(rh or {})


def error_text(body, status):
    err = body.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or ""
        raw = (err.get("metadata") or {}).get("raw")
        return f"{msg} ({raw})" if raw and str(raw) not in msg else msg or f"HTTP {status}"
    return body.get("description") or (str(err) if err else f"HTTP {status}")


def multipart(fields, file_field, filename, content, mime):
    """Encode form fields plus one file for multipart/form-data. Returns (body, content type)."""
    boundary = "nazar" + uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f"Content-Type: {mime}\r\n\r\n".encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
