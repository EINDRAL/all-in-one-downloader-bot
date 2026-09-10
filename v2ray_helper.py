#!/usr/bin/env python3
"""
Lightweight V2Ray/VLESS/Trojan share link parser to Xray config.json generator.
Standard library only (no external dependencies required).
"""

import sys
import json
from urllib.parse import urlparse, parse_qs

def parse_vless(link: str, local_port: int = 10808) -> dict:
    p = urlparse(link)
    qs = parse_qs(p.query)
    
    uuid = p.username
    server = p.hostname
    port = p.port or 443
    
    security = qs.get("security", ["none"])[0].lower()
    net_type = qs.get("type", ["tcp"])[0].lower()
    flow = qs.get("flow", [""])[0]
    sni = qs.get("sni", [server])[0]
    pbk = qs.get("pbk", [""])[0]
    sid = qs.get("sid", [""])[0]
    fp = qs.get("fp", ["chrome"])[0]
    header_type = qs.get("headerType", ["none"])[0]
    host = qs.get("host", [""])[0]
    path = qs.get("path", ["/"])[0]
    
    outbound = {
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": server,
                    "port": port,
                    "users": [
                        {
                            "id": uuid,
                            "encryption": "none",
                            "flow": flow if flow else None
                        }
                    ]
                }
            ]
        },
        "streamSettings": {
            "network": net_type,
            "security": security
        }
    }
    
    # Remove None values
    if not flow:
        outbound["settings"]["vnext"][0]["users"][0].pop("flow", None)

    # Reality settings
    if security == "reality":
        outbound["streamSettings"]["realitySettings"] = {
            "show": False,
            "fingerprint": fp,
            "serverName": sni,
            "publicKey": pbk,
            "shortId": sid,
            "spiderX": ""
        }
    elif security == "tls":
        outbound["streamSettings"]["tlsSettings"] = {
            "serverName": sni,
            "fingerprint": fp
        }

    # Network specific settings
    if net_type == "tcp":
        if header_type == "http":
            outbound["streamSettings"]["tcpSettings"] = {
                "header": {
                    "type": "http",
                    "request": {
                        "version": "1.1",
                        "method": "GET",
                        "path": [path],
                        "headers": {
                            "Host": [host if host else sni],
                            "User-Agent": ["Mozilla/5.0"]
                        }
                    }
                }
            }
    elif net_type == "ws":
        outbound["streamSettings"]["wsSettings"] = {
            "path": path,
            "headers": {
                "Host": host if host else sni
            }
        }
    elif net_type == "grpc":
        service_name = qs.get("serviceName", [""])[0]
        outbound["streamSettings"]["grpcSettings"] = {
            "serviceName": service_name,
            "multiMode": False
        }

    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "port": local_port,
                "listen": "127.0.0.1",
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True}
            }
        ],
        "outbounds": [outbound]
    }
    return config

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python v2ray_helper.py <vless://...>", file=sys.stderr)
        sys.exit(1)
    
    raw_url = sys.argv[1].strip()
    if raw_url.startswith("vless://"):
        res = parse_vless(raw_url)
        print(json.dumps(res, indent=2))
    else:
        print("Unsupported protocol", file=sys.stderr)
        sys.exit(1)
