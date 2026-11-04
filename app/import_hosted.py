import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit
import requests
from import_routes import MAX_UPLOAD, ROOT, LOCK, save_state

USER_AGENT = "Mozilla/5.0"
GOFILE_HOST = "api.gofile.io"
GOFILE_SALT = "12af056dacea0b"
BLOCK_SIZE = 1024 * 1024
MAX_SECONDS = 7200
MAX_FOLDERS = 32

class HostedError(Exception):
    pass

class HostedCancelled(Exception):
    pass

def provider_for(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    if parsed.port not in (None, 443):
        return None
    if host in ("mega.nz", "www.mega.nz", "mega.co.nz"):
        return "mega"
    if host in ("gofile.io", "www.gofile.io"):
        return "gofile"
    return None

def check_public_host(host):
    addresses = socket.getaddrinfo(
        host, 443, type=socket.SOCK_STREAM
    )
    if not addresses:
        raise HostedError("Provider hostname could not be resolved")
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise HostedError("Provider resolved to a non-public address")

def save(token, **updates):
    with LOCK:
        path = ROOT / token / "state.json"
        if not path.exists():
            raise HostedCancelled()
        state = json.loads(path.read_text("utf-8"))
        if state.get("cancel_requested"):
            raise HostedCancelled()

        state.update(updates)
        save_state(ROOT / token, state)

def is_cancelled(token):
    with LOCK:
        path = ROOT / token / "state.json"
        if not path.exists():
            return True
        state = json.loads(path.read_text("utf-8"))
        return bool(state.get("cancel_requested"))

def website_token(account_token):
    slot = int(time.time()) // 14400
    raw = (
        f"{USER_AGENT}::en-US::{account_token}::"
        f"{slot}::{GOFILE_SALT}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()

def gofile_api(client, method, path, account_token="",
               params=None):
    check_public_host(GOFILE_HOST)
    headers = {
        "User-Agent": USER_AGENT,
        "X-Website-Token": website_token(account_token),
        "X-BL": "en-US",
        "Origin": "https://gofile.io",
        "Referer": "https://gofile.io/",
    }
    if account_token:
        headers["Authorization"] = f"Bearer {account_token}"
    response = client.request(
        method,
        f"https://{GOFILE_HOST}{path}",
        headers=headers,
        params=params,
        timeout=(15, 30),
        allow_redirects=False,
    )

    if response.status_code != 200:
        raise HostedError(
            f"Gofile API returned HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise HostedError("Gofile returned invalid API data") from exc
    if payload.get("status") != "ok":
        raise HostedError(
            "Gofile could not access this share. "
            "It may require a password or an account."
        )
    return payload.get("data") or {}

def gofile_files(token, client, content_id, account_token):
    files = []
    pending = [content_id]
    visited = set()
    while pending:
        if is_cancelled(token):
            raise HostedCancelled()
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        if len(visited) > MAX_FOLDERS:
            raise HostedError("Gofile folder structure is too large")
        data = gofile_api(
            client, "GET",
            f"/contents/{current}",
            account_token,
            params={"cache": "true"}
        )
        if data.get("type") == "file":
            files.append(data)
            continue
        if data.get("type") != "folder":
            raise HostedError("Unsupported Gofile content type")
        children = data.get("children") or {}
        if isinstance(children, dict):
            children = list(children.values())
        for child in children:
            if not isinstance(child, dict):
                continue
            if child.get("type") == "folder":
                child_id = child.get("id")
                if not isinstance(child_id, str):
                    raise HostedError("Invalid Gofile folder ID")
                pending.append(child_id)
            elif child.get("type") == "file":
                files.append(child)
        if len(files) > 1000:
            raise HostedError("Gofile share contains too many files")
    return files

def _limit(token):
    from import_url_routes import download_limit
    return download_limit(token)

def download_gofile(token, url):
    from import_url_routes import open_download
    parsed = urlsplit(url)
    match = re.fullmatch(
        r"/d/([a-zA-Z0-9]+)/*", parsed.path
    )
    if not match:
        raise HostedError("Expected a Gofile /d/ share link")
    content_id = match.group(1)
    client = requests.Session()
    try:
        account = gofile_api(
            client, "POST", "/accounts"
        )
        account_token = account.get("token")
        if not isinstance(account_token, str) or not account_token:
            raise HostedError("Gofile did not provide an access token")
        files = gofile_files(
            token, client, content_id, account_token
        )
        archives = [
            item for item in files
            if isinstance(item.get("name"), str)
            and item["name"].lower().endswith(".zip")
        ]
        if len(archives) != 1:
            raise HostedError(
                f"Gofile share contains {len(archives)} ZIP files. "
                "This version requires exactly one ZIP."
            )
        item = archives[0]
        link = item.get("link")
        if not isinstance(link, str):
            raise HostedError("Gofile did not return a download link")
        declared_size = item.get("size", 0)
        if isinstance(declared_size, int) and declared_size > _limit(token):
            raise HostedError("File exceeds the 20 GiB import limit")
        headers = {
            "Authorization": f"Bearer {account_token}",
            "Cookie": f"accountToken={account_token}",
            "User-Agent": USER_AGENT,
            "Referer": "https://gofile.io/",
        }
        connection, response = open_download(
            link, extra_headers=headers
        )
        try:
            length_header = response.getheader("Content-Length")
            expected = (
                int(length_header)
                if length_header and length_header.isdecimal()
                else 0
            )
            if expected > _limit(token):
                raise HostedError("File exceeds the 20 GiB import limit")
            total = expected or (
                declared_size if isinstance(declared_size, int) else 0
            )
            save(token, total=total)
            received = 0
            started = time.monotonic()
            destination = ROOT / token / "upload.zip"
            with destination.open("xb") as output:
                while True:
                    if is_cancelled(token):
                        raise HostedCancelled()
                    if time.monotonic() - started > MAX_SECONDS:
                        raise HostedError("Gofile download timed out")
                    chunk = response.read(BLOCK_SIZE)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > _limit(token):
                        raise HostedError("File exceeds the import limit")
                    output.write(chunk)
                    save(
                        token,
                        received=received,
                        offsets=[received]
                    )
            if expected and received != expected:
                raise HostedError("Incomplete Gofile download")
            return item["name"], received
        finally:
            connection.close()
    finally:
        client.close()

def mega_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in (
        "mega.nz", "www.mega.nz", "mega.co.nz"
    ):
        raise HostedError("Invalid MEGA URL")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise HostedError("Invalid MEGA URL")
    # Public file shares only, including the older #! format.
    if not (
        re.fullmatch(r"/file/[A-Za-z0-9_-]+", parsed.path)
        and re.fullmatch(r"[A-Za-z0-9_-]+", parsed.fragment)
        or parsed.path in ("", "/")
        and parsed.fragment.startswith("!")
        and not parsed.fragment.startswith("!F")
    ):
        raise HostedError(
            "Use a public MEGA file link. "
            "Folder links need a file picker."
        )
    check_public_host(parsed.hostname)
    return url

def staged_size(directory):
    total = 0
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise HostedError("Unexpected symbolic link in download")
        if path.is_file():
            total += path.stat().st_size
    return total

def download_mega(token, url):
    url = mega_url(url)
    executable = shutil.which("megadl")
    if not executable:
        raise HostedError(
            "MEGA downloader unavailable: install megatools"
        )
    directory = ROOT / token / "mega-download"
    directory.mkdir(mode=0o700)
    process = subprocess.Popen(
        [
            executable, "--no-progress",
            "--path", str(directory), url
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    started = time.monotonic()
    last_progress = started
    last_size = 0
    STALL_SECONDS = 180
    try:
        while process.poll() is None:
            if is_cancelled(token):
                raise HostedCancelled()
            if time.monotonic() - started > MAX_SECONDS:
                raise HostedError("MEGA download timed out")
            received = staged_size(directory)
            now = time.monotonic()
            if received > last_size:
                last_progress = now
                last_size = received
            if received > _limit(token):
                raise HostedError("MEGA file exceeds the import limit")
            if now - last_progress > STALL_SECONDS:
                raise HostedError(
                    "MEGA download stalled for over three minutes"
                    "A large download may have exceeded the transfer quota."
                )
            save(token, received=received, offsets=[received])
            time.sleep(0.75)
        if process.returncode != 0:
            raise HostedError(
                "MEGA download failed. The link may be invalid, "
                "unavailable, or over its transfer quota."
            )
        files = [
            path for path in directory.rglob("*")
            if path.is_file()
        ]
        if len(files) != 1 or files[0].suffix.lower() != ".zip":
            raise HostedError(
                "MEGA share must contain exactly one ZIP file"
            )
        if files[0].is_symlink():
            raise HostedError("Unexpected symbolic link in download")
        size = files[0].stat().st_size
        if size > _limit(token):
            raise HostedError("MEGA ZIP exceeds the import limit")
        filename = files[0].name
        os.replace(files[0], ROOT / token / "upload.zip")
        return filename, size
    finally:
        if process.poll() is None:
            import signal
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        shutil.rmtree(directory, ignore_errors=True)

def hosted_worker(token, url, provider):
    from importer.inspector import inspect, InspectionError
    root = ROOT / token
    destination = root / "upload.zip"
    try:
        if provider == "gofile":
            filename, size = download_gofile(token, url)
        elif provider == "mega":
            filename, size = download_mega(token, url)
        else:
            raise HostedError("Unsupported provider")
        if is_cancelled(token):
            raise HostedCancelled()
        save(
            token,
            status="inspecting",
            total=size,
            received=size,
            offsets=[size],
            files=[{"path": filename, "size": size}],
        )
        from import_url_routes import finish_download
        finish_download(token, filename)

    except HostedCancelled:
        destination.unlink(missing_ok=True)
        try:
            save(token, status="cancelled")
        except HostedCancelled:
            with LOCK:
                state_path = root / "state.json"
                if state_path.exists():
                    state = json.loads(state_path.read_text("utf-8"))
                    state["status"] = "cancelled"
                    save_state(root, state)

    except Exception as exc:
        destination.unlink(missing_ok=True)
        with LOCK:
            state_path = root / "state.json"
            if state_path.exists():
                state = json.loads(state_path.read_text("utf-8"))
                state["status"] = "failed"
                state["error"] = str(exc)[:300]
                save_state(root, state)


