"""
StayOnline - BUAA campus network auto-login daemon.

Checks connectivity every 3 minutes and re-authenticates via the
Srun portal challenge-response protocol when the network is down.
Monitoring and login are skipped while connected to a Wi-Fi network
other than BUAA-WiFi / BUAA-Mobile.
"""

import hashlib
import hmac
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CHECK_INTERVAL = 3 * 60          # seconds between connectivity checks
WAKE_POLL_INTERVAL = 15           # seconds between lightweight wake checks
RESUME_GAP_THRESHOLD = 60         # wall-clock gap that suggests sleep/hibernate
MAX_RETRIES = 3                   # login attempts per cycle
RETRY_DELAY = 10                  # seconds between retries
TEST_URL = "https://www.baidu.com"
GATEWAY_DOMAIN = "gw.buaa.edu.cn"
AC_ID = 67                       # campus-area id from the user's portal URL
# Gateway auth is only ever needed on these networks (matched case-insensitively)
BUAA_SSIDS = {"buaa-wifi", "buaa-mobile"}

# ---------------------------------------------------------------------------
# Gateway API URLs
# ---------------------------------------------------------------------------

_GET_CHALLENGE_URL = "https://gw.buaa.edu.cn/cgi-bin/get_challenge"
_SRUN_PORTAL_URL = "https://gw.buaa.edu.cn/cgi-bin/srun_portal"

# ---------------------------------------------------------------------------
# Logging / notification (lazy-initialized)
# ---------------------------------------------------------------------------

_EXE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
LOG_DIR = _EXE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "stay_online.log"

_logger: logging.Logger | None = None


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[
                logging.FileHandler(LOG_FILE, encoding="utf-8"),
            ],
        )
        _logger = logging.getLogger(__name__)
    return _logger

# ---------------------------------------------------------------------------
# Srun portal crypto helpers  (ported from Tony15246/buaa_wifi_login)
#
# The Srun portal uses XXTEA-based encryption (xencode), a custom Base64
# alphabet, HMAC-MD5 for the password field, and SHA1 for the checksum.
# MD5 and SHA1 are weak in general but mandated by the protocol.
# ---------------------------------------------------------------------------

_PADCHAR = "="
_ALPHA = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"


def _ordat(msg: str, idx: int) -> int:
    return ord(msg[idx]) if len(msg) > idx else 0


def _sencode(msg: str, include_length: bool) -> list[int]:
    """Encode a string into a list of 32-bit little-endian integers."""
    result: list[int] = []
    for i in range(0, len(msg), 4):
        result.append(
            _ordat(msg, i)
            | _ordat(msg, i + 1) << 8
            | _ordat(msg, i + 2) << 16
            | _ordat(msg, i + 3) << 24
        )
    if include_length:
        result.append(len(msg))
    return result


def _lencode(msg: list[int], use_len: bool) -> str:
    """Decode a list of 32-bit integers back to a string."""
    length = len(msg)
    ll = (length - 1) << 2
    if use_len:
        m = msg[length - 1]
        if m < ll - 3 or m > ll:
            return ""
        ll = m
    chars: list[str] = []
    for i in range(length):
        chars.append(
            chr(msg[i] & 0xFF)
            + chr(msg[i] >> 8 & 0xFF)
            + chr(msg[i] >> 16 & 0xFF)
            + chr(msg[i] >> 24 & 0xFF)
        )
    return "".join(chars)[:ll] if use_len else "".join(chars)


def _xencode(msg: str, key: str) -> str:
    """XXTEA encryption used by the Srun portal."""
    if not msg:
        return ""
    pwd = _sencode(msg, True)
    pwdk = _sencode(key, False)
    if len(pwdk) < 4:
        pwdk += [0] * (4 - len(pwdk))
    n = len(pwd) - 1
    z = pwd[n]
    # delta = 0x9E7979B9 (XXTEA constant), obfuscated as OR of complement pairs
    c = 0x86014019 | 0x183639A0
    q = math.floor(6 + 52 / (n + 1))
    d = 0
    while q > 0:
        # 0xFFFFFFFF — 32-bit truncation mask
        d = d + c & (0x8CE0D9BF | 0x731F2640)
        e = d >> 2 & 3
        p = 0
        while p < n:
            y = pwd[p + 1]
            m = z >> 5 ^ y << 2
            m += (y >> 3 ^ z << 4) ^ (d ^ y)
            m += pwdk[(p & 3) ^ e] ^ z
            pwd[p] = pwd[p] + m & (0xEFB8D130 | 0x10472ECF)
            z = pwd[p]
            p += 1
        y = pwd[0]
        m = z >> 5 ^ y << 2
        m += (y >> 3 ^ z << 4) ^ (d ^ y)
        m += pwdk[(p & 3) ^ e] ^ z
        pwd[n] = pwd[n] + m & (0xBB390742 | 0x44C6F8BD)
        z = pwd[n]
        q -= 1
    return _lencode(pwd, False)


def _srun_base64(s: str) -> str:
    """Srun's custom Base64 encoding with a non-standard alphabet."""
    if not s:
        return s
    x: list[str] = []
    imax = len(s) - len(s) % 3
    for i in range(0, imax, 3):
        b10 = (ord(s[i]) << 16) | (ord(s[i + 1]) << 8) | ord(s[i + 2])
        x.append(_ALPHA[(b10 >> 18)])
        x.append(_ALPHA[((b10 >> 12) & 63)])
        x.append(_ALPHA[((b10 >> 6) & 63)])
        x.append(_ALPHA[(b10 & 63)])
    if len(s) - imax == 1:
        b10 = ord(s[imax]) << 16
        x.append(
            _ALPHA[(b10 >> 18)]
            + _ALPHA[((b10 >> 12) & 63)]
            + _PADCHAR
            + _PADCHAR
        )
    elif len(s) - imax == 2:
        b10 = (ord(s[imax]) << 16) | (ord(s[imax + 1]) << 8)
        x.append(
            _ALPHA[(b10 >> 18)]
            + _ALPHA[((b10 >> 12) & 63)]
            + _ALPHA[((b10 >> 6) & 63)]
            + _PADCHAR
        )
    return "".join(x)


def _hmac_md5(password: str, token: str) -> str:
    """HMAC-MD5 — protocol-mandated, not a security choice."""
    return hmac.new(token.encode(), password.encode(), hashlib.md5).hexdigest()


def _sha1(value: str) -> str:
    """SHA-1 — protocol-mandated checksum."""
    return hashlib.sha1(value.encode()).hexdigest()

# ---------------------------------------------------------------------------
# Srun portal API
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}

_HTTP_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    global _HTTP_SESSION
    if _HTTP_SESSION is None:
        _HTTP_SESSION = requests.Session()
        _HTTP_SESSION.headers.update(_HEADERS)
    return _HTTP_SESSION


def _jsonp_get(url: str, params: dict) -> dict:
    """Send a JSONP GET request and parse the JSON payload."""
    callback = "jQuery1124044069126839574846_" + str(int(time.time() * 1000))
    params = {**params, "callback": callback}
    resp = _get_session().get(url, params=params, verify=False, timeout=10)
    body = resp.text
    prefix = callback + "("
    if not body.startswith(prefix) or not body.endswith(")"):
        raise ValueError(f"Unexpected JSONP response: {body[:200]}")
    return json.loads(body[len(prefix):-1])


def _get_challenge(username: str) -> tuple[str, str]:
    """Return (client_ip, challenge_token) from the gateway."""
    params = {
        "username": username,
        "ip": "0.0.0.0",
        "_": int(time.time() * 1000),
    }
    res = _jsonp_get(_GET_CHALLENGE_URL, params)
    if "challenge" not in res:
        raise RuntimeError(f"Challenge response missing 'challenge': {res}")
    return res["client_ip"], res["challenge"]


def _build_info(username: str, password: str, ip: str) -> str:
    """Build the info JSON (password is encrypted before transmission)."""
    info_obj = {
        "username": username,
        "password": password,
        "ip": ip,
        "acid": str(AC_ID),
        "enc_ver": "srun_bx1",
    }
    return json.dumps(info_obj)


def srun_login(username: str, password: str) -> dict:
    """Perform the full Srun portal login flow. Returns the API response."""
    ip, token = _get_challenge(username)
    info_plain = _build_info(username, password, ip)
    info_encrypted = "{SRBX1}" + _srun_base64(_xencode(info_plain, token))
    hmd5 = _hmac_md5(password, token)

    chkstr = (
        token + username
        + token + hmd5
        + token + str(AC_ID)
        + token + ip
        + token + "200"
        + token + "1"
        + token + info_encrypted
    )
    chksum = _sha1(chkstr)

    params = {
        "action": "login",
        "username": username,
        "password": "{MD5}" + hmd5,
        "ac_id": AC_ID,
        "ip": ip,
        "info": info_encrypted,
        "chksum": chksum,
        "n": "200",
        "type": "1",
        "os": "Windows 10",
        "name": "Windows",
        "double_stack": "0",
        "_": int(time.time() * 1000),
    }
    return _jsonp_get(_SRUN_PORTAL_URL, params)

# ---------------------------------------------------------------------------
# Network detection
# ---------------------------------------------------------------------------


def is_online() -> bool:
    """Check connectivity by requesting an external site.

    Returns False if the request fails or gets redirected to the gateway.
    """
    try:
        resp = _get_session().get(TEST_URL, timeout=5, allow_redirects=True)
        if resp.status_code in (200, 304) and GATEWAY_DOMAIN not in resp.text:
            return True
        return False
    except requests.RequestException:
        return False


def get_connected_wifi_ssid() -> str | None:
    """Return the SSID of the connected Wi-Fi, or None when not on Wi-Fi.

    Parses `netsh wlan show interfaces`. The `SSID` field name is not
    localized (unlike the rest of the output), so only that line is matched;
    `BSSID` lines do not match because of the leading-word anchor. None
    covers wired connections, Wi-Fi off, no wireless adapter, and
    non-Windows platforms — callers keep the normal behaviour then.
    """
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*SSID\s*[:：]\s*(\S.*?)\s*$", line)
        if match:
            return match.group(1)
    return None


def is_foreign_wifi_ssid(ssid: str | None) -> bool:
    """True when on a Wi-Fi that is not a BUAA campus network.

    Gateway authentication only applies to the BUAA network, so a foreign
    Wi-Fi (hotspot, home router, ...) needs neither monitoring nor login
    attempts. None (not on Wi-Fi, e.g. wired) is never treated as foreign.
    """
    return ssid is not None and ssid.casefold() not in BUAA_SSIDS

# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------


def notify(title: str, message: str) -> None:
    """Show a Windows toast notification and log the message."""
    log = _get_logger()
    log.info("%s: %s", title, message)
    try:
        ps = (
            f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            f"ContentType = WindowsRuntime] | Out-Null; "
            f"$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(0); "
            f"$text = $template.GetElementsByTagName('text')[0]; "
            f"$text.AppendChild($template.CreateTextNode('{message}')) | Out-Null; "
            f"$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{title}').Show($toast)"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        log.debug("Toast notification failed: %s", exc)

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


class ConfigurationError(Exception):
    """Raised when required environment variables are missing."""


def _load_credentials() -> tuple[str, str]:
    username = os.environ.get("BUAA_USERNAME")
    password = os.environ.get("BUAA_PASSWORD")
    if not username or not password:
        raise ConfigurationError("BUAA_USERNAME / BUAA_PASSWORD environment variables not set")
    return username, password


def attempt_login(username: str, password: str) -> bool:
    """Try to log in with retries. Returns True on success."""
    log = _get_logger()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = srun_login(username, password)
            error = result.get("error", "unknown")
            error_msg = result.get("error_msg", "")
            log.info("Login attempt %d: error=%s, msg=%s", attempt, error, error_msg)
            if error == "ok":
                notify("StayOnline", "Login successful")
                return True
            log.warning("Login attempt %d failed: %s", attempt, error_msg)
        except Exception as exc:
            log.error("Login attempt %d exception: %s", attempt, exc)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    notify("StayOnline", "All login attempts failed")
    return False


def should_run_resume_check(previous_wall_time: float, current_wall_time: float) -> bool:
    """Return True when the process appears to have resumed from sleep."""
    return current_wall_time - previous_wall_time >= RESUME_GAP_THRESHOLD


def validate_network_and_login(username: str, password: str, was_online: bool, reason: str) -> bool:
    """Check connectivity and attempt login if the network is unavailable."""
    log = _get_logger()
    if is_online():
        if not was_online or reason == "resume":
            log.info("Network OK after %s check", reason)
        return True

    if was_online:
        log.warning("Network down after %s check, attempting login", reason)
    else:
        log.warning("Network still down after %s check, attempting login", reason)
    return attempt_login(username, password)


def main() -> None:
    log = _get_logger()
    try:
        username, password = _load_credentials()
    except ConfigurationError as exc:
        log.error(str(exc))
        sys.exit(1)

    log.info("StayOnline started (check every %ds)", CHECK_INTERVAL)

    online = True  # suppress repeated "Network OK" spam; only log actual events
    next_check_at = time.time()
    last_poll_wall_time = time.time()
    paused_ssid: str | None = None  # foreign Wi-Fi currently being skipped

    while True:
        try:
            now = time.time()
            resumed = should_run_resume_check(last_poll_wall_time, now)
            if resumed or now >= next_check_at:
                ssid = get_connected_wifi_ssid()
                if is_foreign_wifi_ssid(ssid):
                    if paused_ssid != ssid:
                        log.info("Connected to Wi-Fi %r (not a BUAA network); pausing checks and login", ssid)
                        paused_ssid = ssid
                    next_check_at = time.time() + CHECK_INTERVAL
                else:
                    if paused_ssid is not None:
                        log.info("Left non-BUAA Wi-Fi, resuming checks and login")
                        paused_ssid = None
                    if resumed:
                        gap = int(now - last_poll_wall_time)
                        log.info("Wake/resume detected after %ds pause", gap)
                        reason = "resume"
                    else:
                        reason = "scheduled"
                    online = validate_network_and_login(username, password, online, reason)
                    next_check_at = time.time() + CHECK_INTERVAL
        except KeyboardInterrupt:
            log.info("User exit")
            sys.exit(0)
        except Exception as exc:
            log.error("Unexpected error in main loop: %s", exc)
        last_poll_wall_time = time.time()
        time.sleep(min(WAKE_POLL_INTERVAL, max(1, next_check_at - time.time())))


if __name__ == "__main__":
    main()
