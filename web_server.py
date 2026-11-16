"""
Socket web server with file search (CSE 614).
Run on the server machine:  python web_server.py

Built on raw TCP sockets, no http.server or frameworks.

Routes:
    GET /                  search home page
    GET /search?q=cat      results page (empty q lists every file)
    GET /lucky?q=cat       302 redirect to the first result
    GET /api/search?q=cat  results as JSON (used by web_client.py)
    GET /<file>            the file itself

Stop with Ctrl+C.
"""

import html
import json
import mimetypes
import re
import socket
import time
from email.utils import formatdate
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

HOST = ""                          # empty string = listen on all interfaces
PORT = 8080
WEB_ROOT = Path(__file__).parent   # only files under this folder are served
CLIENT_TIMEOUT = 3.0               # seconds to wait for a silent client
MAX_TEXT_SEARCH = 1_000_000        # skip content search in text files over 1 MB

# Hidden from search and never served: our own source and the client's download folder
SKIP_DIRS = {"__pycache__", "downloads"}
SKIP_SUFFIXES = {".py", ".pyc"}


# ---------------------------------------------------------------- HTTP helpers

def build_response(status_line: str, body: bytes, content_type: str = "text/html",
                   extra_headers: dict | None = None) -> bytes:
    """Join status line, headers, blank line and body into raw HTTP bytes."""
    # charset only applies to text types, not images
    if content_type.startswith("text/") or content_type == "application/json":
        content_type += "; charset=utf-8"

    extra = "".join(f"{name}: {value}\r\n" for name, value in (extra_headers or {}).items())

    headers = (
        f"{status_line}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"   # lets the client know where the body ends
        f"{extra}"
        f"Connection: close\r\n"             # one request per connection
        f"Date: {formatdate(usegmt=True)}\r\n"
        f"Server: PythonSocketServer/3.0-search\r\n"
        f"\r\n"                              # blank line ends the headers
    )
    return headers.encode() + body


def receive_request_headers(conn: socket.socket) -> str | None:
    """Read from the socket until the blank line that ends the request headers.
    Returns None if the client closed without sending anything."""
    buffer = b""
    # TCP is a byte stream, so one recv() may return only part of the request
    while b"\r\n\r\n" not in buffer:
        chunk = conn.recv(4096)
        if not chunk:                  # empty bytes = client closed its side
            return None if not buffer else buffer.decode(errors="replace")
        buffer += chunk

    header_bytes, _, _ = buffer.partition(b"\r\n\r\n")
    return header_bytes.decode(errors="replace")


def parse_headers(header_text: str) -> dict:
    """Header lines as a dict with lowercase names. Skips the request line."""
    headers = {}
    for line in header_text.split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return headers


def resolve_path(url_path: str) -> Path | None:
    """Map a URL path to a file under WEB_ROOT, or None if it points outside."""
    url_path = unquote(url_path)       # "My%20File.txt" -> "My File.txt"

    candidate = (WEB_ROOT / url_path.lstrip("/")).resolve()

    # blocks requests like /../../secret.txt
    if not candidate.is_relative_to(WEB_ROOT.resolve()):
        return None
    return candidate


def guess_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def is_text_type(ctype: str) -> bool:
    return ctype.startswith("text/") or ctype in ("application/json", "application/javascript")


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} bytes"


# ---------------------------------------------------------------- search

def list_server_files() -> list[Path]:
    """All files the server will show, sorted by relative path."""
    root = WEB_ROOT.resolve()
    files = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if not p.is_file():
            continue
        # skip dotfiles/dot-folders and the hidden folders above
        if any(part.startswith(".") or part in SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIXES:
            continue
        files.append(p)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix().lower())


def read_plain_text(path: Path, ctype: str) -> str:
    """Searchable text of a file. Empty for binaries and oversized files."""
    if not is_text_type(ctype) or path.stat().st_size > MAX_TEXT_SEARCH:
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if ctype == "text/html":
        # drop script/style/title blocks, then the remaining tags, so we match words not markup
        text = re.sub(r"<(script|style|title)\b.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()   # collapse whitespace


def make_snippet(text: str, words: list[str], width: int = 160) -> str:
    """Excerpt of `text` starting just before the earliest query word."""
    if not text:
        return ""
    lower = text.lower()
    hits = [lower.find(w) for w in words if lower.find(w) != -1]
    start = max(0, min(hits) - width // 3) if hits else 0
    snippet = text[start:start + width].strip()
    if start > 0:
        snippet = "... " + snippet
    if start + width < len(text):
        snippet += " ..."
    return snippet


def highlight(text: str, words: list[str]) -> str:
    """Escape `text` for HTML and wrap each query word in <b>."""
    escaped = html.escape(text)
    # longest words first so a short word does not split a longer match
    for w in sorted(set(words), key=len, reverse=True):
        escaped = re.sub(f"({re.escape(html.escape(w))})", r"<b>\1</b>", escaped, flags=re.I)
    return escaped


def search_files(query: str) -> list[dict]:
    """Files whose name, title or text contains every query word (case-insensitive).
    An empty query matches everything. Name matches rank above content-only matches."""
    words = query.lower().split()
    root = WEB_ROOT.resolve()
    results = []

    for path in list_server_files():
        rel = path.relative_to(root).as_posix()
        ctype = guess_type(path)
        text = read_plain_text(path, ctype)
        title = extract_title(path, ctype)

        name_lower = rel.lower() + " " + title.lower()
        text_lower = text.lower()
        # every word must appear in the name/title or in the body
        if not all(w in name_lower or w in text_lower for w in words):
            continue

        name_hits = sum(w in name_lower for w in words)
        results.append({
            "name": rel,
            "url": "/" + quote(rel),
            "type": ctype,
            "size": path.stat().st_size,
            "snippet": make_snippet(text, words),
            "title": title or path.name,
            "_score": name_hits,       # only used for sorting, removed below
        })

    results.sort(key=lambda r: (-r["_score"], r["name"].lower()))
    for r in results:
        del r["_score"]
    return results


def extract_title(path: Path, ctype: str) -> str:
    """The <title> of an HTML file, or an empty string."""
    if ctype != "text/html":
        return ""
    head = path.read_text(encoding="utf-8", errors="replace")[:4096]   # title is near the top
    m = re.search(r"<title[^>]*>(.*?)</title>", head, flags=re.S | re.I)
    return html.unescape(m.group(1)).strip() if m else ""


# ---------------------------------------------------------------- HTML pages

PAGE_CSS = """
:root { --ink:#202124; --muted:#5f6368; --link:#1a0dab; --url:#188038;
        --line:#dfe1e5; --bg:#fff; --soft:#f8f9fa; --accent:#3b5bdb; }
* { box-sizing:border-box; }
body { margin:0; font-family:Arial, Helvetica, sans-serif; color:var(--ink); background:var(--bg); }
a { color:var(--link); text-decoration:none; }
a:hover { text-decoration:underline; }
.logo { font-weight:700; letter-spacing:-1px; color:var(--ink); }
.logo span { color:var(--accent); }
.box { display:flex; align-items:center; border:1px solid var(--line); border-radius:24px;
           padding:0 8px 0 18px; height:46px; background:#fff; }
.box:hover, .box:focus-within { box-shadow:0 1px 6px rgba(32,33,36,.28); border-color:transparent; }
.box input { flex:1; border:0; outline:0; font-size:16px; height:100%; background:transparent; min-width:0; }
.box button { border:0; background:transparent; font-size:18px; cursor:pointer; color:var(--muted); }
.btns { margin-top:28px; display:flex; gap:12px; justify-content:center; flex-wrap:wrap; }
.btns button { background:var(--soft); border:1px solid var(--soft); border-radius:4px; color:#3c4043;
               font-size:14px; padding:0 16px; height:36px; cursor:pointer; }
.btns button:hover { border-color:var(--line); box-shadow:0 1px 1px rgba(0,0,0,.1); }
/* home page */
.home { min-height:100vh; display:flex; flex-direction:column; }
.home main { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center;
             padding:0 16px 12vh; }
.home .logo { font-size:clamp(44px, 10vw, 76px); margin-bottom:28px; }
.home .home-form { width:min(584px, 100%); }
.tagline { color:var(--muted); font-size:14px; margin-top:22px; text-align:center; }
footer { background:#f2f2f2; color:var(--muted); font-size:14px; padding:14px 24px;
         border-top:1px solid var(--line); display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px; }
/* results page */
header.top { display:flex; align-items:center; gap:28px; padding:20px 24px 14px; border-bottom:1px solid #ebebeb;
             flex-wrap:wrap; }
header.top .logo { font-size:26px; }
header.top .box { flex:1; max-width:692px; height:44px; }
.results { padding:12px 24px 40px 24px; max-width:760px; margin-left:calc(24px + 140px); }
@media (max-width:820px) { .results { margin-left:0; } header.top { gap:14px; } }
.stats { color:#70757a; font-size:14px; margin:6px 0 22px; }
.result { display:flex; gap:16px; margin-bottom:28px; }
.result .body { flex:1; min-width:0; }
.result .url { color:var(--url); font-size:14px; overflow-wrap:anywhere; }
.result h3 { margin:3px 0 4px; font-size:20px; font-weight:400; line-height:1.3; }
.result .meta { color:#70757a; font-size:13px; }
.result .snip { color:#4d5156; font-size:14px; line-height:1.58; margin-top:3px; overflow-wrap:anywhere; }
.result img.thumb { width:96px; height:96px; object-fit:cover; border-radius:8px; border:1px solid var(--line);
                    flex:none; }
.badge { display:inline-block; font-size:11px; font-weight:700; color:#fff; background:var(--accent);
         border-radius:3px; padding:1px 5px; margin-right:6px; vertical-align:1px; }
.empty { font-size:16px; line-height:1.6; }
.empty ul { color:var(--muted); }
"""

LOGO = '<span class="logo">Socket<span>Search</span></span>'


def page(title: str, body_class: str, body: str) -> bytes:
    """Wrap a body fragment in a full HTML document with the shared CSS."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{PAGE_CSS}</style>
</head>
<body class="{body_class}">
{body}
</body>
</html>""".encode()


def search_box(query: str = "") -> str:
    # the form is a plain GET, so submitting it requests /search?q=...
    return (f'<form class="box" action="/search" method="get" role="search">'
            f'<input name="q" value="{html.escape(query)}" placeholder="Search files on this server"'
            f' aria-label="Search" autocomplete="off">'
            f'<button type="submit" aria-label="Search">&#128269;</button></form>')


def home_page(host: str) -> bytes:
    count = len(list_server_files())
    body = f"""
<div class="home">
<main>
  {LOGO}
  <form class="home-form" action="/search" method="get" role="search">
    <div class="box">
      <input name="q" placeholder="Search files on this server" aria-label="Search" autocomplete="off" autofocus>
      <button type="submit" aria-label="Search">&#128269;</button>
    </div>
    <div class="btns">
      <button type="submit">Search Server</button>
      <button type="submit" formaction="/lucky">I'm Feeling Lucky</button>
    </div>
  </form>
  <p class="tagline">Searching {count} file{'s' if count != 1 else ''} on <b>{html.escape(host)}</b>.
     Leave the box empty to list everything.</p>
</main>
<footer><span>CSE 614 &middot; Raw socket web server</span><span>Served over TCP port {PORT}</span></footer>
</div>"""
    return page("SocketSearch", "home", body)


def results_page(query: str, results: list[dict], elapsed: float, host: str) -> bytes:
    words = query.lower().split()
    n = len(results)

    if n == 0:
        listing = f"""
<div class="empty">
  <p>Your search - <b>{html.escape(query)}</b> - did not match any files on this server.</p>
  <p>Suggestions:</p>
  <ul>
    <li>Make sure all words are spelled correctly.</li>
    <li>Try fewer or different keywords.</li>
    <li><a href="/search?q=">List every file on the server</a>.</li>
  </ul>
</div>"""
    else:
        items = []
        for r in results:
            url = r["url"]
            full_url = f"http://{host}{unquote(url)}"
            # badge shows the file extension, e.g. HTML or WEBP
            badge = r["name"].rsplit(".", 1)[-1].upper() if "." in r["name"] else "FILE"
            meta = f'<span class="badge">{html.escape(badge)}</span>{html.escape(r["type"])} &middot; {human_size(r["size"])}'
            if r["type"].startswith("image/"):
                # the browser fetches this thumbnail over its own connection
                thumb = f'<a href="{url}"><img class="thumb" src="{url}" alt="{html.escape(r["name"])}" loading="lazy"></a>'
                snip = f'Image file. Click to open <b>{html.escape(r["name"])}</b> in the browser.'
            else:
                thumb = ""
                snip = highlight(r["snippet"], words) if r["snippet"] else "No text preview for this file type."
            items.append(f"""
<div class="result">
  <div class="body">
    <div class="url">{html.escape(full_url)}</div>
    <h3><a href="{url}">{highlight(r["title"], words)}</a></h3>
    <div class="meta">{meta}</div>
    <div class="snip">{snip}</div>
  </div>
  {thumb}
</div>""")
        listing = "".join(items)

    label = "All server content" if not query else f"About {n} result{'s' if n != 1 else ''}"
    body = f"""
<header class="top"><a href="/">{LOGO}</a>{search_box(query)}</header>
<div class="results">
  <div class="stats">{label} &middot; {n} file{'s' if n != 1 else ''} ({elapsed:.3f} seconds)</div>
  {listing}
</div>"""
    title = f"{query} - SocketSearch" if query else "All files - SocketSearch"
    return page(title, "serp", body)


def error_page(code: str, message: str) -> bytes:
    # `message` may contain markup, so callers must escape any user input in it
    return page(code, "serp",
                f'<header class="top"><a href="/">{LOGO}</a>{search_box()}</header>'
                f'<div class="results"><h2>{html.escape(code)}</h2><p>{message}</p>'
                f'<p><a href="/search?q=">See every file on this server</a></p></div>')


# ---------------------------------------------------------------- request handling

def route(method: str, url_path: str, host: str) -> tuple[bytes, str]:
    """Pick a response for one request. Returns (raw response bytes, log line)."""
    if method != "GET":
        body = error_page("405 Method Not Allowed", "This server only speaks GET.")
        return build_response("HTTP/1.1 405 Method Not Allowed", body), f"405 Method Not Allowed (method={method})"

    # split "/search?q=cat" into the path and the query string
    path, _, query_string = url_path.partition("?")
    params = parse_qs(query_string, keep_blank_values=True)
    query = params.get("q", [""])[0].strip()

    if path == "/":
        body = home_page(host)
        return build_response("HTTP/1.1 200 OK", body), f"200 OK  search home page ({len(body)} bytes)"

    if path in ("/search", "/api/search", "/lucky"):
        t0 = time.perf_counter()
        results = search_files(query)
        elapsed = time.perf_counter() - t0

        if path == "/api/search":
            body = json.dumps({"query": query, "count": len(results), "results": results}, indent=2).encode()
            return (build_response("HTTP/1.1 200 OK", body, "application/json"),
                    f"200 OK  JSON search q={query!r} -> {len(results)} result(s)")

        if path == "/lucky" and results:
            # the redirect is just the Location header; the browser makes the next request
            target = results[0]["url"]
            body = f'<p>Redirecting to <a href="{target}">{target}</a></p>'.encode()
            return (build_response("HTTP/1.1 302 Found", body, extra_headers={"Location": target}),
                    f"302 Found  lucky q={query!r} -> {target}")

        # also reached by /lucky with no results: show the empty results page
        body = results_page(query, results, elapsed, host)
        return (build_response("HTTP/1.1 200 OK", body),
                f"200 OK  search q={query!r} -> {len(results)} result(s)")

    # anything else is a plain file request, e.g. from clicking a result
    file_path = resolve_path(path)
    if file_path is None:
        body = error_page("403 Forbidden", "That path is outside the web root.")
        return build_response("HTTP/1.1 403 Forbidden", body), f"403 Forbidden (escapes web root: {path})"

    try:
        if file_path.suffix.lower() in SKIP_SUFFIXES:
            raise PermissionError        # treated as 404 below so source files stay private
        body = file_path.read_bytes()
        ctype = guess_type(file_path)
        return (build_response("HTTP/1.1 200 OK", body, ctype),
                f"200 OK  {file_path.name}  ({len(body)} bytes, {ctype})")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        body = error_page("404 Not Found", f"The file <b>{html.escape(unquote(path))}</b> is not on this server.")
        return build_response("HTTP/1.1 404 Not Found", body), f"404 Not Found ({path})"


def handle_client(conn: socket.socket, addr: tuple) -> None:
    """Serve one request on this connection, then close it."""
    print(f"\n[+] Connection from {addr[0]}:{addr[1]}")
    # browsers sometimes open a spare connection and never send on it;
    # the timeout keeps that from blocking the single-threaded loop
    conn.settimeout(CLIENT_TIMEOUT)
    try:
        header_text = receive_request_headers(conn)
        if header_text is None:
            print("    (client sent nothing)")
            return

        # first line looks like "GET /search?q=cat HTTP/1.1"
        request_line = header_text.split("\r\n")[0]
        print(f"    Request : {request_line}")

        parts = request_line.split()
        method = parts[0] if parts else ""
        url_path = parts[1] if len(parts) > 1 else "/"
        host = parse_headers(header_text).get("host", f"localhost:{PORT}")

        response, log_line = route(method, url_path, host)
        print(f"    Response: {log_line}")
        conn.sendall(response)

    except socket.timeout:
        print(f"    (no request within {CLIENT_TIMEOUT}s, closing)")
    except Exception as e:
        print(f"    [!] Error: {e}")
    finally:
        conn.close()
        print(f"[-] Connection closed: {addr[0]}:{addr[1]}")


def get_local_ip() -> str:
    """This machine's LAN IP, used for the startup message."""
    try:
        # connecting a UDP socket sends nothing, but makes the OS pick the
        # outgoing interface, whose address we then read back
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def main() -> None:
    ip = get_local_ip()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        # lets us restart right away without "Address already in use"
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((HOST, PORT))
        server_socket.listen(5)          # backlog of 5 waiting connections
        # accept() times out every second so Ctrl+C is noticed on Windows
        server_socket.settimeout(1.0)

        files = list_server_files()
        print("=" * 60)
        print(f"  Socket Web Server + File Search on port {PORT}")
        print(f"  Serving : {WEB_ROOT}")
        print(f"  Files   : {', '.join(p.name for p in files) or '(none)'}")
        print(f"  Browser : http://{ip}:{PORT}        (search page)")
        print(f"  Client  : python web_client.py {ip}")
        print("  Press Ctrl+C to stop.")
        print("=" * 60)

        try:
            while True:
                try:
                    # accept() returns a new socket for this one client
                    conn, addr = server_socket.accept()
                except socket.timeout:
                    continue
                handle_client(conn, addr)    # one client at a time
        except KeyboardInterrupt:
            print("\n[*] Server stopped.")


if __name__ == "__main__":
    main()
