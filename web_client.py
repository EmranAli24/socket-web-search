"""
Socket Web Client with File Search — CSE 614
Run this on the CLIENT laptop.

Usage:  python web_client.py <SERVER_IP> [PORT]
Example:
    python web_client.py 192.168.0.105
    python web_client.py                  (defaults to 127.0.0.1, same-PC test)

At the prompt you can type:
    search cat     -> ask the server which files match "cat"
    ?cat           -> same thing, shorter
    search         -> empty search = list everything on the server
    1, 2, 3 ...    -> open result number N from the last search
    /index.html    -> fetch a file by path, like the original client
    /Cat.webp      -> an image, saved into downloads/
Press Enter on an empty line to quit.

The client still builds every HTTP request by hand and sends it over a raw
socket. A search is just another GET: GET /api/search?q=cat HTTP/1.1.
"""

import json
import socket
import sys
from pathlib import Path
from urllib.parse import quote, unquote

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
TIMEOUT = 5.0                                    # seconds to wait on the server
DOWNLOAD_DIR = Path(__file__).parent / "downloads"
PREVIEW_CHARS = 800                              # how much of a text body to print


def build_request(host: str, port: int, path: str) -> bytes:
    """
    Hand-build a raw HTTP GET request. No libraries -- this is the point of the
    exercise. A GET has no body, so the request is just the request line, some
    headers, and the blank line that ends them.

    The Host header is what lets one server serve many sites from one IP, and
    HTTP/1.1 requires it.
    """
    request_text = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"User-Agent: PythonSocketClient/3.0-search\r\n"
        f"Accept: */*\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    )
    return request_text.encode()


def parse_headers(header_text: str) -> dict:
    """Turn the header block into a lowercase-keyed dict. Status line excluded."""
    headers = {}
    for line in header_text.split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return headers


def receive_full_response(sock: socket.socket) -> bytes:
    """
    Read the whole response.

    Same byte-stream problem the server has: one recv() is not guaranteed to
    give us everything. A 287 KB image arrives as dozens of chunks. We asked for
    "Connection: close", so the server hangs up when it is done -- recv()
    returning b"" is our end-of-message marker.
    """
    chunks = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:                # b"" means the server closed its end
            break
        chunks.append(chunk)
    return b"".join(chunks)


def save_body(path: str, body: bytes) -> Path:
    """Write a downloaded file into downloads/, named after the URL path."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    name = Path(unquote(path.split("?", 1)[0])).name or "index.html"
    out_path = DOWNLOAD_DIR / name
    out_path.write_bytes(body)
    return out_path


def http_get(host: str, port: int, path: str, show_request: bool = True) -> tuple[str, dict, bytes] | None:
    """
    Open a connection, send one GET, read the whole reply, close.
    Returns (header text, header dict, body), or None if the server sent nothing.
    """
    request = build_request(host, port, path)

    if show_request:
        print("\n--- Raw HTTP request sent ---")
        print(request.decode(errors="replace"))

    # A fresh socket per request, because the server replies with
    # "Connection: close" and hangs up after every response.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
        client_socket.settimeout(TIMEOUT)
        client_socket.connect((host, port))       # the TCP three-way handshake
        client_socket.sendall(request)
        response = receive_full_response(client_socket)

    if not response:
        print("[!] The server closed the connection without answering.")
        return None

    # Headers and body are separated by one blank line -- the same rule the
    # server uses when it builds the response.
    header_bytes, _, body = response.partition(b"\r\n\r\n")
    header_text = header_bytes.decode(errors="replace")
    return header_text, parse_headers(header_text), body


def fetch(host: str, port: int, path: str) -> None:
    """GET one path and show the answer: print text, save anything binary."""
    reply = http_get(host, port, path)
    if reply is None:
        return
    header_text, headers, body = reply

    print("--- Response headers received ---")
    print(header_text)
    print()

    status_line = header_text.split("\r\n")[0]
    ctype = headers.get("content-type", "")
    print("--- Result ---")
    print(f"Status : {status_line}")
    print(f"Type   : {ctype or '(none)'}")
    print(f"Body   : {len(body)} bytes")

    if ctype.startswith("text/") or ctype.startswith("application/json"):
        text = body.decode("utf-8", errors="replace")
        print("-" * 55)
        print(text[:PREVIEW_CHARS])
        if len(text) > PREVIEW_CHARS:
            print(f"... ({len(text) - PREVIEW_CHARS} more characters)")
        print("-" * 55)
    elif body:
        # Not text -- an image or some other binary file. Printing it would just
        # spray control characters at the terminal, so save it instead.
        saved = save_body(path, body)
        print(f"Saved  : {saved}")


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} bytes"


def search(host: str, port: int, query: str) -> list[dict]:
    """
    Ask the server which files match `query`. This is an ordinary GET to
    /api/search; the server answers with JSON instead of an HTML page.
    Returns the list of results so the user can open one by number.
    """
    path = "/api/search?q=" + quote(query)
    reply = http_get(host, port, path)
    if reply is None:
        return []
    header_text, headers, body = reply

    status_line = header_text.split("\r\n")[0]
    print(f"--- Server replied: {status_line}  ({len(body)} bytes of JSON) ---")
    if " 200 " not in status_line + " ":
        print(body.decode(errors="replace")[:PREVIEW_CHARS])
        return []

    data = json.loads(body.decode("utf-8"))
    results = data.get("results", [])

    label = "All files on the server" if not query else f"Results for '{query}'"
    print(f"\n{label}: {len(results)} found")
    print("=" * 55)
    if not results:
        print("  No files matched. Try 'search' with no words to list everything.")
    for i, r in enumerate(results, 1):
        print(f"  [{i}] {r['name']}")
        print(f"      {r['type']}, {human_size(r['size'])}   ->  {r['url']}")
        if r.get("snippet"):
            print(f"      {r['snippet'][:110]}")
    print("=" * 55)
    if results:
        print("Type a number to open that file.")
    return results


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT

    print("=" * 55)
    print(f"  Socket Web Client -> {host}:{port}")
    print("  search <words>  find files on the server")
    print("  <number>        open a result from the last search")
    print("  /path           fetch a file directly (e.g. /Cat.webp)")
    print("  Empty line quits.")
    print("=" * 55)

    last_results: list[dict] = []

    while True:
        try:
            line = input("\nPath or search: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[*] Client stopped.")
            break

        if line == "":
            print("[*] Client stopped.")
            break

        try:
            if line.lower() == "search" or line.lower().startswith("search ") or line.startswith("?"):
                query = line[1:] if line.startswith("?") else line[6:]
                last_results = search(host, port, query.strip())

            elif line.isdigit():
                n = int(line)
                if 1 <= n <= len(last_results):
                    fetch(host, port, last_results[n - 1]["url"])
                else:
                    print("[!] No such result. Run a search first, then pick one of its numbers.")

            else:
                path = line if line.startswith("/") else "/" + line   # "index.html" -> "/index.html"
                fetch(host, port, path)

        except ConnectionRefusedError:
            print(f"\n[!] Connection refused by {host}:{port}")
            print("    Is web_server.py running on the server laptop?")
            print("    Is the IP address correct? (run ipconfig there)")
        except socket.timeout:
            print(f"\n[!] Timed out after {TIMEOUT}s waiting for {host}:{port}")
            print("    A firewall blocking inbound TCP is the usual cause.")
        except OSError as e:
            print(f"\n[!] Network error: {e}")
        except json.JSONDecodeError:
            print("\n[!] The server's search reply was not valid JSON.")


if __name__ == "__main__":
    main()
