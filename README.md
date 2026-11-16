# Socket Web Server + File Search

**CSE 614 – Computer Networks Lab**

A web server and an HTTP client built directly on raw TCP sockets, with no
`http.server` and no frameworks. Every request and response is assembled by hand.
On top of that sits a small search engine for the files the server holds.

Open the server in a browser and you get a search page. Type a word, or leave
the box empty, and the results page lists the matching files. Click a result and
the browser opens it: `index.html` renders as a page and `Cat.webp` shows the
picture.

## Features

- Raw TCP socket server and client (Python `socket` module only)
- Hand-built HTTP/1.1 requests and responses
- File search by name and content, with matched words in **bold**
- "I'm Feeling Lucky" using a `302 Found` redirect
- JSON search API, used by the terminal client
- Path-traversal protection (`403 Forbidden`) and proper `404` / `405` errors
- No third-party dependencies

## Requirements

- Python 3.10 or newer (the code uses `X | None` type hints and `Path.is_relative_to`)
- Nothing to install

## Project structure

| File | Runs on | Job |
|---|---|---|
| `web_server.py` | server laptop | listens on TCP 8080, serves the search page, results and files |
| `web_client.py` | client laptop | `search <words>` from the terminal, then open a result by number |
| `index.html`, `about.html`, `notes.txt`, `Cat.webp` | server laptop | the content being searched and served |

Drop any other file into this folder and it shows up in search right away.
The server's own `.py` files are hidden from search and refused if requested.

## How to run

**1. On the server laptop:**

```
python web_server.py
```

It prints its LAN IP, e.g. `192.168.0.105`.

**2a. From any browser on the same Wi-Fi:** open `http://192.168.0.105:8080`

**2b. Or from the terminal client:**

```
python web_client.py 192.168.0.105
```

To test on one PC, use `http://127.0.0.1:8080`, or run `python web_client.py`
with no arguments.

## What to try in the browser

| Action | What happens |
|---|---|
| Open `/` | the search home page |
| Search with an empty box | **all server content** is listed |
| Search `cat` | `Cat.webp` with a thumbnail, plus the files that mention "cat" |
| Search `group` | `index.html`, with the matched words in **bold** |
| Click a result | that file opens: an HTML page renders, an image displays |
| **I'm Feeling Lucky** with `cat` | `302 Found` redirect straight to `/Cat.webp` |
| Search `zzz` | "did not match any files" |
| `/nothing.html` | `404 Not Found` |
| `/../../secret.txt` | `403 Forbidden`, because the server refuses to leave its folder |

## What to try in the terminal client

```
Path or search: search cat        <- or ?cat
  [1] Cat.webp      image/webp, 280.3 KB   ->  /Cat.webp
  [2] about.html    text/html, 491 bytes   ->  /about.html
  ...
Path or search: 1                 <- downloads Cat.webp into downloads/
Path or search: search            <- empty search lists every file
Path or search: /index.html       <- direct paths still work
```

Press Enter on an empty line to quit.

## Server routes

| Request | Response |
|---|---|
| `GET /` | search home page (HTML) |
| `GET /search?q=...` | results page (HTML) |
| `GET /lucky?q=...` | `302 Found` + `Location:` the first result |
| `GET /api/search?q=...` | the same results as JSON, which the terminal client uses |
| `GET /<file>` | the file itself |

## Networking concepts shown in the code

- **TCP is a byte stream.** One `recv()` is not one message, so both sides keep
  reading until they see the end of the message: the blank line `\r\n\r\n` that
  ends HTTP headers on the server, and the server closing the connection on the
  client.
- **A search is just a GET with a query string.** The browser turns the form
  into `GET /search?q=cat HTTP/1.1`. The server splits the path at `?` and reads
  `q`. No new protocol is involved.
- **One page, many TCP connections.** The results page with a thumbnail makes
  the browser open a second connection for `Cat.webp`. Watch the server log:
  each request gets its own accept, response and close.
- **Redirects are only a header.** "I'm Feeling Lucky" sends `302 Found` with a
  `Location:` header, and the browser makes a fresh request to that URL.
- **Same data, two formats.** Browsers get HTML from `/search`. The Python
  client gets JSON from `/api/search`. `Content-Type` tells each side which one
  it got.
- **URL encoding.** A space in a file name travels as `%20`, so
  `resolve_path()` decodes it before the path-traversal check.
- **Single-threaded on purpose.** Browsers sometimes open a spare connection
  and send nothing on it. To stop that from blocking everyone else, the server
  waits at most 3 seconds for a request (`CLIENT_TIMEOUT`).

## Troubleshooting

- `Connection refused`: `web_server.py` is not running, or the IP is wrong
  (run `ipconfig` on the server laptop).
- `Timed out`: Windows Firewall is blocking inbound TCP 8080. Allow Python
  through on a **Private** network, and make sure both laptops are on the same
  Wi-Fi.

## Author

Course lab work for CSE 614, by [@EmranAli24](https://github.com/EmranAli24).
