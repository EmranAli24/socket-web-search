"""
Socket web client with file search (CSE 614).
Run on the client machine:  python web_client.py <SERVER_IP> [PORT]

    python web_client.py 192.168.0.105
    python web_client.py              (defaults to 127.0.0.1)

At the prompt:
    search cat     files matching "cat"
    ?cat           shorthand for the above
    search         list every file on the server
    1, 2, 3 ...    open result N from the last search
    /index.html    fetch a path directly
Empty line quits. Images and other binaries are saved to downloads/.

Requests are built by hand and sent over a raw socket.
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
PREVIEW_CHARS = 800                              # max characters of a text body to print


def build_request(host: str, port: int, path: str) -> bytes:
    """Build a raw HTTP GET request. A GET has no body, so it is just the
    request line, headers and the blank line that ends them."""
    request_text = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"               # required by HTTP/1.1
        f"User-Agent: PythonSocketClient/3.0-search\r\n"
        f"Accept: */*\r\n"
        f"Connection: close\r\n"                 # server hangs up after replying
        f"\r\n"
    )
    return request_text.encode()


def parse_headers(header_text: str) -> dict:
    """Header lines as a dict with lowercase names. Skips the status line."""
    headers = {}
    for line in header_text.split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return headers


def receive_full_response(sock: socket.socket) -> bytes:
    """Read until the server closes the connection."""
    chunks = []
    while True:
        chunk = sock.recv(4096)      # one recv() may return only part of the reply
        if not chunk:                # empty bytes = server closed, reply is complete
            break
        chunks.append(chunk)
    return b"".join(chunks)


def save_body(path: str, body: bytes) -> Path:
    """Save a downloaded body into downloads/, named after the URL path."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    name = Path(unquote(path.split("?", 1)[0])).name or "index.html"
    out_path = DOWNLOAD_DIR / name
    out_path.write_bytes(body)
    return out_path


def http_get(host: str, port: int, path: str, show_request: bool = True) -> tuple[str, dict, bytes] | None:
    """Send one GET and return (header text, header dict, body).
    Returns None if the server closed without answering."""
    request = build_request(host, port, path)

    if show_request:
        print("\n--- Raw HTTP request sent ---")
        print(request.decode(errors="replace"))

    # new socket per request because the server closes after each response
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
        client_socket.settimeout(TIMEOUT)
        client_socket.connect((host, port))       # TCP handshake happens here
        client_socket.sendall(request)
        response = receive_full_response(client_socket)

    if not response:
        print("[!] The server closed the connection without answering.")
        return None

    # a blank line separates the headers from the body
    header_bytes, _, body = response.partition(b"\r\n\r\n")
    header_text = header_bytes.decode(errors="replace")
    return header_text, parse_headers(header_text), body


def fetch(host: str, port: int, path: str) -> None:
    """GET one path. Print text bodies, save binary ones."""
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
        # binary data would only garble the terminal, so write it to disk
        saved = save_body(path, body)
        print(f"Saved  : {saved}")


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} bytes"


def search(host: str, port: int, query: str) -> list[dict]:
    """Query /api/search and print the matches.
    Returns the result list so a result can be opened by number."""
    path = "/api/search?q=" + quote(query)   # quote() escapes spaces and symbols
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

    last_results: list[dict] = []    # results of the most recent search

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
