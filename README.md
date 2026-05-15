# tftp

A simple dependency-free, single-file userland TFTP server, similar in spirit to `python3 -m http.server`.

> If you want a TFTP client, use `curl`.

## Usage

The server is a single file that only depends on the python stdlib, thus it can be easily executed via `curl`:

```sh
curl -fsSL https://raw.githubusercontent.com/bytedream/tftp/main/src/server.py | python3 -
```

Or install locally (you may have to add the `--break-system-packages` flag):

```sh
git clone https://github.com/bytedream/tftp
pip install ./tftp
# alternative: install it from the git repo directly
pip install git+https://github.com/bytedream/tftp

python3 -m tftp.server
```

| Option | Description |
|---|---|
| `port` | Port number (default: 69) |
| `-b`, `--bind` | Bind address (default: all interfaces) |
| `-d`, `--directory` | Serve this directory (default: current directory) |
| `-w`, `--write` | Allow upload (WRQ) requests |

### Examples

Serve the current directory on the default port (requires root/elevated privileges):

```sh
curl -fsSL https://raw.githubusercontent.com/bytedream/tftp/main/src/server.py | python3 -
# or, if installed locally:
python3 -m tftp.server
```

Serve a specific directory on a non-privileged port:

```sh
curl -fsSL https://raw.githubusercontent.com/bytedream/tftp/main/src/server.py | python3 - 6969 -d /srv/tftp
# or, if installed locally:
python3 -m tftp.server 6969 -d /srv/tftp
```

Allow file uploads:

```sh
curl -fsSL https://raw.githubusercontent.com/bytedream/tftp/main/src/server.py | python3 - -d /srv/tftp -w
# or, if installed locally:
python3 -m tftp.server 6969 -d /srv/tftp -w
```

## Features

- Read (RRQ) and write (WRQ) requests
- `octet` and `netascii` transfer modes
- Option negotiation ([RFC 2347](https://www.rfc-editor.org/rfc/rfc2347)): `blksize`, `tsize`, `timeout`
- Path traversal protection
- Concurrent transfers via threading

### Implemented RFCs

- [RFC 1350](https://www.rfc-editor.org/rfc/rfc1350) -- The TFTP Protocol (Revision 2)
- [RFC 2347](https://www.rfc-editor.org/rfc/rfc2347) -- TFTP Option Extension
- [RFC 2348](https://www.rfc-editor.org/rfc/rfc2348) -- TFTP Blocksize Option
- [RFC 2349](https://www.rfc-editor.org/rfc/rfc2349) -- TFTP Timeout Interval and Transfer Size Options

## License

This project is licensed under the MIT License - see the [LICENSE](./LICENSE) file for more details.
