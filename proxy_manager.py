"""
Proxy and V2Ray link manager for All-in-One Downloader Bot.
Supports direct SOCKS5/HTTP proxies and V2Ray links (VLESS, VMess, Trojan, Shadowsocks).
Spins up an embedded lightweight Xray child process automatically when a V2Ray link is provided.
"""

import os
import sys
import json
import time
import base64
import socket
import urllib.parse
import subprocess
import atexit
import logging
from typing import Optional, Dict, Any, Tuple
from pathlib import Path

logger = logging.getLogger("ProxyManager")

DEFAULT_SOCKS_PORT = 10885
_XRAY_PROCESS: Optional[subprocess.Popen] = None
_CONFIG_FILE: Optional[Path] = None
_RESOLVED_PROXY_URL: Optional[str] = None


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def get_free_port(start_port: int = 10885) -> int:
    for port in range(start_port, start_port + 100):
        if not is_port_in_use(port):
            return port
    return start_port


def find_xray_binary() -> Optional[str]:
    candidates = [
        "xray",
        "/usr/local/bin/xray",
        "/usr/bin/xray",
        str(Path.home() / ".local/bin/xray"),
        str(Path(__file__).resolve().parent / "bin/xray")
    ]
    for c in candidates:
        if os.path.isabs(c):
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        else:
            path = subprocess.run(["which", c], capture_output=True, text=True).stdout.strip()
            if path and os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return None


def test_proxy_connectivity(proxy_url: str, timeout: float = 6.0) -> Tuple[bool, str]:
    """Tests SOCKS5 or HTTP proxy connectivity by establishing a test handshake."""
    t0 = time.time()
    try:
        parsed = urllib.parse.urlparse(proxy_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 1080
        s = socket.create_connection((host, port), timeout=timeout)
        if "socks" in parsed.scheme.lower():
            s.sendall(b"\x05\x01\x00")
            resp = s.recv(2)
            if resp != b"\x05\x00":
                s.close()
                return False, f"SOCKS5 auth failed ({resp.hex() if resp else 'empty'})"
        dt = int((time.time() - t0) * 1000)
        s.close()
        return True, f"Proxy connection verified successfully (Latency: {dt}ms)"
    except socket.timeout:
        return False, "Connection timed out"
    except ConnectionRefusedError:
        return False, "Connection refused (Port closed)"
    except Exception as e:
        return False, f"Proxy error: {e}"


def parse_vmess(uri: str) -> Dict[str, Any]:
    raw = uri[8:]
    missing_padding = len(raw) % 4
    if missing_padding:
        raw += "=" * (4 - missing_padding)
    decoded = base64.b64decode(raw).decode("utf-8")
    data = json.loads(decoded)

    host = data.get("add", "")
    port = int(data.get("port", 443))
    uuid = data.get("id", "")
    alter_id = int(data.get("aid", 0))
    net = data.get("net", "tcp").lower()
    security = data.get("tls", "none").lower()
    sni = data.get("sni") or data.get("host") or ""
    path = data.get("path", "")

    outbound = {
        "protocol": "vmess",
        "settings": {
            "vnext": [{
                "address": host,
                "port": port,
                "users": [{
                    "id": uuid,
                    "alterId": alter_id,
                    "security": "auto"
                }]
            }]
        },
        "streamSettings": {
            "network": net,
            "security": security if security in ["tls", "none"] else "none"
        }
    }

    if security == "tls":
        outbound["streamSettings"]["tlsSettings"] = {
            "serverName": sni,
            "allowInsecure": False
        }

    if net == "ws":
        outbound["streamSettings"]["wsSettings"] = {
            "path": path,
            "headers": {"Host": sni} if sni else {}
        }
    elif net == "grpc":
        outbound["streamSettings"]["grpcSettings"] = {
            "serviceName": path,
            "multiMode": False
        }

    return outbound


def parse_vless(parsed: urllib.parse.ParseResult) -> Dict[str, Any]:
    uuid = parsed.username
    host = parsed.hostname
    port = parsed.port or 443
    params = urllib.parse.parse_qs(parsed.query)

    def get_p(key: str, default: str = "") -> str:
        return params.get(key, [default])[0]

    net = get_p("type", "tcp").lower()
    security = get_p("security", "none").lower()
    flow = get_p("flow", "")
    sni = get_p("sni") or get_p("host", host or "")
    pbk = get_p("pbk", "")
    sid = get_p("sid", "")
    spx = get_p("spx", "")
    fp = get_p("fp", "chrome")
    path = get_p("path", "")
    service_name = get_p("serviceName", path)

    user_obj: Dict[str, Any] = {
        "id": uuid,
        "encryption": "none"
    }
    if flow:
        user_obj["flow"] = flow

    outbound = {
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": host,
                "port": port,
                "users": [user_obj]
            }]
        },
        "streamSettings": {
            "network": net,
            "security": security
        }
    }

    if security == "reality":
        outbound["streamSettings"]["realitySettings"] = {
            "serverName": sni,
            "fingerprint": fp or "chrome",
            "show": False,
            "publicKey": pbk,
            "shortId": sid,
            "spiderX": spx
        }
    elif security == "tls":
        outbound["streamSettings"]["tlsSettings"] = {
            "serverName": sni,
            "fingerprint": fp or "chrome",
            "allowInsecure": False
        }

    if net == "ws":
        outbound["streamSettings"]["wsSettings"] = {
            "path": path or "/",
            "headers": {"Host": sni} if sni else {}
        }
    elif net == "grpc":
        outbound["streamSettings"]["grpcSettings"] = {
            "serviceName": service_name,
            "multiMode": False
        }

    return outbound


def parse_trojan(parsed: urllib.parse.ParseResult) -> Dict[str, Any]:
    password = parsed.username
    host = parsed.hostname
    port = parsed.port or 443
    params = urllib.parse.parse_qs(parsed.query)

    def get_p(key: str, default: str = "") -> str:
        return params.get(key, [default])[0]

    net = get_p("type", "tcp").lower()
    security = get_p("security", "tls").lower()
    sni = get_p("sni", host or "")
    path = get_p("path", "")

    outbound = {
        "protocol": "trojan",
        "settings": {
            "servers": [{
                "address": host,
                "port": port,
                "password": password
            }]
        },
        "streamSettings": {
            "network": net,
            "security": security,
            "tlsSettings": {
                "serverName": sni,
                "allowInsecure": False
            }
        }
    }

    if net == "ws":
        outbound["streamSettings"]["wsSettings"] = {
            "path": path or "/",
            "headers": {"Host": sni} if sni else {}
        }
    elif net == "grpc":
        outbound["streamSettings"]["grpcSettings"] = {
            "serviceName": get_p("serviceName", path),
            "multiMode": False
        }

    return outbound


def parse_shadowsocks(uri: str) -> Dict[str, Any]:
    raw = uri[5:].split("#")[0]
    if "@" in raw:
        user_info, host_port = raw.split("@", 1)
        pad = len(user_info) % 4
        if pad:
            user_info += "=" * (4 - pad)
        decoded_info = base64.b64decode(user_info).decode("utf-8")
        method, password = decoded_info.split(":", 1)
        host, port_str = host_port.split(":", 1)
        port = int(port_str)
    else:
        pad = len(raw) % 4
        if pad:
            raw += "=" * (4 - pad)
        decoded = base64.b64decode(raw).decode("utf-8")
        user_part, host_port = decoded.split("@", 1)
        method, password = user_part.split(":", 1)
        host, port_str = host_port.split(":", 1)
        port = int(port_str)

    return {
        "protocol": "shadowsocks",
        "settings": {
            "servers": [{
                "address": host,
                "port": port,
                "method": method,
                "password": password
            }]
        }
    }


def convert_link_to_xray_config(link: str, listen_port: int) -> Dict[str, Any]:
    link = link.strip()
    if link.startswith("vmess://"):
        outbound = parse_vmess(link)
    elif link.startswith("ss://"):
        outbound = parse_shadowsocks(link)
    else:
        parsed = urllib.parse.urlparse(link)
        scheme = parsed.scheme.lower()
        if scheme == "vless":
            outbound = parse_vless(parsed)
        elif scheme == "trojan":
            outbound = parse_trojan(parsed)
        else:
            raise ValueError(f"Unsupported V2Ray protocol: {scheme}")

    config = {
        "log": {
            "loglevel": "warning"
        },
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": listen_port,
                "protocol": "socks",
                "settings": {
                    "auth": "noauth",
                    "udp": True
                }
            },
            {
                "listen": "127.0.0.1",
                "port": listen_port + 1,
                "protocol": "http",
                "settings": {
                    "timeout": 60
                }
            }
        ],
        "outbounds": [
            outbound,
            {
                "protocol": "freedom",
                "tag": "direct"
            }
        ]
    }
    return config


def stop_xray():
    global _XRAY_PROCESS, _CONFIG_FILE
    if _XRAY_PROCESS:
        logger.info("Stopping managed background Xray client...")
        try:
            _XRAY_PROCESS.terminate()
            _XRAY_PROCESS.wait(timeout=2)
        except Exception:
            _XRAY_PROCESS.kill()
        _XRAY_PROCESS = None
    if _CONFIG_FILE and _CONFIG_FILE.exists():
        try:
            _CONFIG_FILE.unlink()
        except Exception:
            pass


atexit.register(stop_xray)


def init_proxy(proxy_str: Optional[str] = None) -> Optional[str]:
    """
    Initializes and starts the proxy if configured in .env (PROXY or YTDLP_PROXY).
    Returns the resolved proxy URL (e.g. 'socks5://127.0.0.1:10885') or None.
    """
    global _XRAY_PROCESS, _CONFIG_FILE, _RESOLVED_PROXY_URL

    if proxy_str is None:
        proxy_str = os.getenv("PROXY") or os.getenv("YTDLP_PROXY") or ""

    proxy_str = proxy_str.strip()
    if not proxy_str:
        _RESOLVED_PROXY_URL = None
        return None

    # 1. Direct standard proxies (SOCKS5 / HTTP / SOCKS4)
    if any(proxy_str.startswith(pfx) for pfx in ("socks5://", "http://", "https://", "socks4://")):
        logger.info(f"Using direct proxy: {proxy_str.split('@')[-1]}")
        _RESOLVED_PROXY_URL = proxy_str
        return _RESOLVED_PROXY_URL

    # 2. V2Ray links (vless://, vmess://, trojan://, ss://)
    v2ray_schemes = ("vless://", "vmess://", "trojan://", "ss://")
    if any(proxy_str.startswith(s) for s in v2ray_schemes):
        xray_bin = find_xray_binary()
        if not xray_bin:
            logger.error("V2Ray link provided in .env, but Xray binary was not found!")
            return None

        listen_port = get_free_port(DEFAULT_SOCKS_PORT)
        proto_name = proxy_str.split("://")[0].upper()
        logger.info(f"Setting up embedded Xray for {proto_name} node on port {listen_port}...")

        try:
            cfg_dict = convert_link_to_xray_config(proxy_str, listen_port)
        except Exception as e:
            logger.error(f"Failed to parse V2Ray link: {e}")
            return None

        work_dir = Path(__file__).resolve().parent
        config_path = work_dir / f".xray_auto_{listen_port}.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg_dict, f, indent=2)

        stop_xray()
        _CONFIG_FILE = config_path

        _XRAY_PROCESS = subprocess.Popen(
            [xray_bin, "run", "-c", str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # Wait for port readiness
        for _ in range(25):
            if is_port_in_use(listen_port):
                break
            time.sleep(0.1)

        _RESOLVED_PROXY_URL = f"http://127.0.0.1:{listen_port + 1}"
        logger.info(f"Embedded Xray successfully running! HTTP proxy bound to {_RESOLVED_PROXY_URL} (SOCKS5 on port {listen_port})")
        return _RESOLVED_PROXY_URL

    logger.warning(f"Unrecognized proxy format: {proxy_str[:30]}...")
    return None


def get_active_proxy_url() -> Optional[str]:
    """Returns the cached resolved proxy URL or initializes it once."""
    global _RESOLVED_PROXY_URL
    if _RESOLVED_PROXY_URL is None:
        init_proxy()
    return _RESOLVED_PROXY_URL


if __name__ == "__main__":
    test_link = sys.argv[1] if len(sys.argv) > 1 else ""
    if not test_link:
        print("Usage: python proxy_manager.py <proxy_or_v2ray_link>")
        sys.exit(1)
    res = init_proxy(test_link)
    if res:
        print(f"SUCCESS: Proxy ready at {res}")
        ok, msg = test_proxy_connectivity(res)
        print(f"Connectivity test: {msg}")
    else:
        print("FAILED to initialize proxy.")
