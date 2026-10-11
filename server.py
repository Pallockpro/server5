#!/usr/bin/env python3
"""
ThyAnime — MegaPlay stream proxy + custom embed player.
Plain Python HTTP server.

Routes:
  /docs                                  → developer docs page
  /embed/<anilist_id>/<episode>/<sub|dub>→ self-contained HLS player
  /api/resolve/<anilist_id>/<episode>/<sub|dub>
  /api/skip/<anilist_id>/<episode>?length=<seconds>
  /api/anime/<anilist_id>
  /api/debug/<anilist_id>/<episode>/<sub|dub>
  /proxy?url=<encoded>
  /__thy  ·  /health  ·  /robots.txt
  /                                      → 204 No Content

Tunnel:
  Named Cloudflare Tunnel via token (routes come from the Zero Trust dashboard)

Force off with:  TUNNEL=none

Deps:
    pip install requests pycryptodome

System dep (required for the public URL):
    cloudflared — see setup instructions
"""

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

import requests

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False
    print("[!] pycryptodome missing — run: pip install pycryptodome")

# ── config ────────────────────────────────────────────────────────────────
PORT             = int(os.environ.get("PORT", "5000"))
TUNNEL_MODE      = os.environ.get("TUNNEL", "named").lower()  # named|none

# ── Named tunnel configuration ────────────────────────────────────────────
TUNNEL_TOKEN     = os.environ.get(
    "TUNNEL_TOKEN",
    "eyJhIjoiMTUwMWY1OWM0YjVhN2MwNmJkNWNhNGYxY2ZkZmJkODciLCJ0IjoiYzYxOWZmNTYtYzdhZC00OGZhLWI5OGUtN2FlY2RkZWY0NjQ3IiwicyI6IlkyTm1ZbVV5TXpNdE9HWTBPUzAwWmpZNUxXRXdabVF0TVRsaU1XUXdZamMwTXpkaCJ9"
)
PUBLIC_HOSTNAME  = os.environ.get("PUBLIC_HOSTNAME", "api.thyanime.me")

MEGAPLAY_BASE = "https://megaplay.buzz"
ANISKIP_BASE  = "https://api.aniskip.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": MEGAPLAY_BASE + "/",
    "Accept": "text/html,application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

DATA_ID_PATTERNS = [
    re.compile(r'(?i)\bdata-id\s*=\s*["\']([A-Za-z0-9_-]+)["\']'),
    re.compile(r'(?i)["\']data-id["\']\s*:\s*["\']([A-Za-z0-9_-]+)["\']'),
    re.compile(r'(?i)data-id\s*=\s*["\']?([A-Za-z0-9_-]+)'),
    re.compile(r'(?i)\bdata-id\b[^A-Za-z0-9_-]{0,12}([A-Za-z0-9_-]{4,})'),
]

_AES_KEY = bytes.fromhex("693f4c4d5441783051362c3a7d35305500000000000000000000000000000000")
_AES_IV  = bytes.fromhex("57303b3237546f6155706c5f50252763")

HTTP = requests.Session()
HTTP.headers.update(BASE_HEADERS)

DEBUG = os.environ.get("DEBUG", "0") == "1"
HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC_URL = {"value": None}
_CLOUDFLARED_PROC = None
# Set by the drain thread when cloudflared logs "Registered tunnel connection".
# This is the definitive signal that the tunnel is up, and it doesn't depend
# on the local machine's DNS resolver working.
_CLOUDFLARED_CONNECTED = threading.Event()
# Counter so we can see how many connection registrations happen.
_CLOUDFLARED_CONN_COUNT = {"value": 0}

# ─────────────────────────── AES ───────────────────────────
def decrypt_enc(enc_b64: str) -> Optional[Dict]:
    if not _HAS_CRYPTO:
        return None
    try:
        b64 = enc_b64.replace("-", "+").replace("_", "/")
        b64 += "=" * ((4 - len(b64) % 4) % 4)
        ct = base64.b64decode(b64)
        cipher = AES.new(_AES_KEY, AES.MODE_CBC, _AES_IV)
        pt = unpad(cipher.decrypt(ct), AES.block_size)
        return json.loads(pt.decode("utf-8"))
    except Exception as e:
        print(f"[decrypt] failed: {e}")
        return None

# ─────────────────────────── AniList ────────────────────────
ANILIST_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id idMal
    title { romaji english native }
    synonyms episodes format
    nextAiringEpisode { episode }
  }
}
"""

def get_anilist_media(anilist_id: int) -> Optional[Dict]:
    try:
        r = requests.post(
            "https://graphql.anilist.co",
            json={"query": ANILIST_QUERY, "variables": {"id": anilist_id}},
            timeout=15,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        r.raise_for_status()
        return r.json().get("data", {}).get("Media")
    except Exception as e:
        print(f"[anilist] {e}")
        return None

# ─────────────────────── AniSkip ────────────────────────────
def get_skip_times(mal_id: int, episode: int,
                   episode_length: float) -> Optional[Dict]:
    base = f"{ANISKIP_BASE}/v2/skip-times/{mal_id}/{episode}"
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    ep_str = f"{episode_length:.3f}"

    all_types  = "types[]=op&types[]=ed&types[]=mixed-op&types[]=mixed-ed&types[]=recap"
    common     = "types[]=op&types[]=ed"
    length_q   = f"episodeLength={urllib.parse.quote(ep_str)}"

    attempts: List[str] = [
        f"{base}?{all_types}&{length_q}",
        f"{base}?{common}&{length_q}",
        f"{base}?" + urllib.parse.urlencode(
            [("types[]", "op"), ("types[]", "ed"), ("episodeLength", ep_str)]),
        f"{base}?{length_q}",
    ]

    last_reason = "no attempts"
    for i, url in enumerate(attempts, 1):
        try:
            r = HTTP.get(url, headers=hdrs, timeout=12)
        except Exception as e:
            last_reason = f"network: {e}"
            print(f"  \033[90m│\033[0m  aniskip  attempt {i} "
                  f"\033[31m{e}\033[0m")
            continue

        if r.status_code == 200:
            try:
                data = r.json()
            except Exception as e:
                last_reason = f"json parse: {e}"
                print(f"  \033[90m│\033[0m  aniskip  attempt {i} "
                      f"\033[31mjson err: {e}\033[0m")
                continue
            if isinstance(data, dict):
                found = data.get("found", False)
                n = len(data.get("results") or [])
                print(f"  \033[90m│\033[0m  aniskip  \033[32mOK\033[0m  "
                      f"found={found} results={n}  (attempt {i})")
                return data
            return {"found": False, "results": []}

        if r.status_code == 404:
            print(f"  \033[90m│\033[0m  aniskip  \033[33m404\033[0m  no skip data")
            return {"found": False, "results": []}

        body = (r.text or "").strip().replace("\n", " ")[:160]
        last_reason = f"HTTP {r.status_code} {body}"
        print(f"  \033[90m│\033[0m  aniskip  attempt {i} "
              f"\033[31mHTTP {r.status_code}\033[0m  {body}")

    print(f"  \033[90m│\033[0m  aniskip  \033[31mall attempts failed: {last_reason}\033[0m")
    return None

# ─────────────────────── MegaPlay resolution ────────────────
def warm_session():
    try:
        r = HTTP.get(MEGAPLAY_BASE + "/", timeout=12)
        print(f"  \033[90m│\033[0m  warm   megaplay.buzz  \033[32mHTTP {r.status_code}\033[0m")
    except Exception as e:
        print(f"  \033[90m│\033[0m  warm   \033[31m{e}\033[0m")

def fetch_embed(embed_url: str) -> Tuple[Optional[str], str]:
    try:
        r = HTTP.get(
            embed_url,
            headers={**BASE_HEADERS, "Referer": MEGAPLAY_BASE + "/"},
            timeout=15,
            allow_redirects=True,
        )
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        body = r.text
        if "cf-chl" in body or "Just a moment" in body or "challenge-platform" in body:
            return None, "Cloudflare challenge"
        if len(body) < 200:
            return None, f"short response ({len(body)} bytes)"
        return body, ""
    except Exception as e:
        return None, f"network: {e}"

def extract_data_id(html: str) -> Optional[str]:
    for pat in DATA_ID_PATTERNS:
        m = pat.search(html)
        if m:
            return m.group(1)
    return None

def _extract_m3u8(sources) -> Optional[str]:
    if not sources:
        return None
    if isinstance(sources, str):
        return sources if ".m3u8" in sources else None
    if isinstance(sources, dict):
        return sources.get("file") or sources.get("url") or sources.get("src")
    if isinstance(sources, list):
        for s in sources:
            if not isinstance(s, dict):
                continue
            if s.get("type") == "hls" or ".m3u8" in (s.get("file") or ""):
                return s.get("file") or s.get("url") or s.get("src")
        s0 = sources[0]
        if isinstance(s0, dict):
            return s0.get("file") or s0.get("url") or s0.get("src")
        if isinstance(s0, str):
            return s0
    return None

def _extract_subtitles(tracks) -> List[Dict]:
    out = []
    if not isinstance(tracks, list):
        return out
    for t in tracks:
        if not isinstance(t, dict):
            continue
        u = t.get("file") or t.get("url") or t.get("src")
        if u:
            out.append({"url": u, "lang": t.get("label") or t.get("language") or "und"})
    return out

def _parse_sources_payload(payload) -> Optional[Dict]:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]

    tracks = payload.get("tracks") or payload.get("subtitles") or []

    enc = payload.get("enc")
    if enc and isinstance(enc, str):
        dec = decrypt_enc(enc)
        if dec:
            if isinstance(dec, dict):
                sources = dec.get("sources") or dec.get("file") or dec.get("url")
                dec_tracks = dec.get("tracks") or dec.get("subtitles") or tracks
                m3u8 = _extract_m3u8(sources)
                if m3u8:
                    return {"m3u8": m3u8, "subtitles": _extract_subtitles(dec_tracks)}
            elif isinstance(dec, str) and ".m3u8" in dec:
                return {"m3u8": dec, "subtitles": _extract_subtitles(tracks)}
        return None

    m3u8 = _extract_m3u8(payload.get("sources"))
    if not m3u8:
        return None
    return {"m3u8": m3u8, "subtitles": _extract_subtitles(tracks)}

def get_sources(data_id: str, referer: str, audio: str) -> Optional[Dict]:
    type_val = "2" if audio == "dub" else "1"
    headers = {
        **BASE_HEADERS,
        "Referer": referer,
        "Origin": MEGAPLAY_BASE,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
    }
    endpoints = [
        f"{MEGAPLAY_BASE}/stream/getSources",
        f"{MEGAPLAY_BASE}/videojs/stream/getSources",
        f"{MEGAPLAY_BASE}/stream/getSources2",
    ]
    for endpoint in endpoints:
        for method in ("GET", "POST"):
            try:
                params = {"id": data_id, "type": type_val}
                if method == "GET":
                    r = HTTP.get(endpoint, params=params, headers=headers, timeout=15)
                else:
                    r = HTTP.post(endpoint, data=params, headers=headers, timeout=15)
                if r.status_code != 200:
                    continue
                ct = (r.headers.get("content-type") or "").lower()
                if "json" not in ct and not r.text.lstrip().startswith(("{", "[")):
                    continue
                try:
                    payload = r.json()
                except Exception:
                    continue
                parsed = _parse_sources_payload(payload)
                if parsed:
                    print(f"  \033[90m│\033[0m  source \033[32mOK\033[0m via {method} {endpoint.split('/')[-1]}")
                    return parsed
            except Exception:
                continue
    return None

def resolve_stream(anilist_id: int, episode: int, audio: str) -> Optional[Dict]:
    print(f"\n  \033[90m│\033[0m  \033[1mresolving\033[0m  anilist=\033[33m{anilist_id}\033[0m ep=\033[33m{episode}\033[0m lang=\033[33m{audio}\033[0m")
    media = get_anilist_media(anilist_id)
    if not media:
        print("  \033[90m│\033[0m    \033[31m✗\033[0m AniList failed")
        return None
    mal_id = media.get("idMal")
    title = (media.get("title") or {}).get("english") or (media.get("title") or {}).get("romaji")
    print(f"  \033[90m│\033[0m    title  {title!r}  MAL={mal_id}")

    candidates = [f"{MEGAPLAY_BASE}/stream/ani/{anilist_id}/{episode}/{audio}"]
    if mal_id:
        candidates.append(f"{MEGAPLAY_BASE}/stream/mal/{mal_id}/{episode}/{audio}")

    for url in candidates:
        print(f"  \033[90m│\033[0m    try   {url}")
        html, reason = fetch_embed(url)
        if not html:
            print(f"  \033[90m│\033[0m      \033[31m✗\033[0m {reason}")
            continue
        data_id = extract_data_id(html)
        if not data_id:
            print(f"  \033[90m│\033[0m      \033[31m✗\033[0m no data-id")
            continue
        print(f"  \033[90m│\033[0m      \033[32m✓\033[0m data-id={data_id}")
        stream = get_sources(data_id, referer=url, audio=audio)
        if stream:
            stream["malId"] = mal_id
            print(f"  \033[90m│\033[0m      \033[32m✓\033[0m m3u8  ({len(stream.get('subtitles', []))} subs)")
            return stream
    print("  \033[90m│\033[0m    \033[31m✗\033[0m all routes failed")
    return None

# ─────────────────────────── Cache ──────────────────────────
_cache: Dict = {}
_cache_lock = threading.Lock()

def cache_get(key):
    with _cache_lock:
        v = _cache.get(key)
        if v and v[1] > time.time():
            return v[0]
    return None

def cache_set(key, val, ttl=1800):
    with _cache_lock:
        _cache[key] = (val, time.time() + ttl)

# ─────────────────────── Playlist rewriting ─────────────────
def _rewrite_playlist(text: str, base_url: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            def _repl(m):
                u = urllib.parse.urljoin(base_url, m.group(1))
                return 'URI="/proxy?url=' + urllib.parse.quote(u, safe="") + '"'
            out.append(re.sub(r'URI="([^"]+)"', _repl, line))
        else:
            out.append("/proxy?url=" + urllib.parse.quote(
                urllib.parse.urljoin(base_url, s), safe=""))
    return "\n".join(out)

# ═══════════════════════════════════════════════════════════════
#  PLAYER TEMPLATE
# ═══════════════════════════════════════════════════════════════
PLAYER_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>ThyAnime</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  html, body { margin:0; padding:0; height:100%; background:#000; overflow:hidden;
               font-family:'Inter','Segoe UI',system-ui,-apple-system,sans-serif;
               color:#fff; -webkit-font-smoothing:antialiased; }
  #stage { position:fixed; inset:0; }
  video { width:100%; height:100%; display:block; background:#000; outline:none; }

  #stage.cursor-hidden,
  #stage.cursor-hidden * { cursor: none !important; }

  .brand {
    position:absolute; top:16px; right:20px; z-index:8;
    display:flex; align-items:center; gap:10px;
    pointer-events:none; user-select:none;
    opacity:.85;
    transition: opacity .7s ease;
    animation: brand-in .9s cubic-bezier(.34,1.56,.64,1) both;
  }
  .brand.idle { opacity:.22; }
  @keyframes brand-in {
    from { opacity:0; transform: translateY(-14px) scale(.85); }
    to   { opacity:.85; transform: translateY(0) scale(1); }
  }
  .brand-mark {
    width:34px; height:34px; border-radius:50%;
    overflow:hidden;
    display:flex; align-items:center; justify-content:center;
    box-shadow:0 0 18px rgba(168,85,247,.55), 0 0 36px rgba(124,58,237,.28);
    position:relative;
    border:1.5px solid rgba(168,85,247,.55);
    background:#0d0a1a;
    animation: brand-pulse 2.6s ease-in-out infinite;
  }
  .brand-mark img {
    width:100%; height:100%; object-fit:cover; display:block;
    border-radius:50%;
  }
  @keyframes brand-pulse {
    0%,100% { box-shadow:0 0 16px rgba(168,85,247,.5), 0 0 32px rgba(124,58,237,.22); }
    50%     { box-shadow:0 0 26px rgba(168,85,247,.9), 0 0 54px rgba(236,72,153,.45); }
  }
  .brand-text {
    font-weight:800; letter-spacing:1.6px; font-size:15px;
    background: linear-gradient(120deg, #e9d5ff, #a855f7 30%, #ec4899 55%, #6366f1 80%, #e9d5ff);
    background-size: 260% 100%;
    -webkit-background-clip:text; background-clip:text; color:transparent;
    animation: brand-shimmer 4s linear infinite;
  }
  @keyframes brand-shimmer {
    from { background-position: 0% 0; }
    to   { background-position: 260% 0; }
  }

  .overlay {
    position:absolute; inset:0; z-index:20;
    display:flex; flex-direction:column; align-items:center;
    justify-content:center; gap:20px;
    background: radial-gradient(circle at 50% 40%, #0d0a1a, #050308);
    transition: opacity .5s ease;
  }
  .overlay.hidden { opacity:0; pointer-events:none; }
  .spinner {
    width:56px; height:56px; position:relative;
    animation: spin-wrap 3s ease-in-out infinite;
  }
  @keyframes spin-wrap {
    0%,100% { transform: scale(1); }
    50%     { transform: scale(1.06); }
  }
  .spinner::before, .spinner::after {
    content:""; position:absolute; inset:0; border-radius:50%;
    border:3px solid transparent;
  }
  .spinner::before {
    border-top-color:#a855f7; border-right-color:#a855f7;
    animation: spin 1s linear infinite;
    box-shadow: 0 0 18px rgba(168,85,247,.4);
  }
  .spinner::after {
    inset:7px;
    border-bottom-color:#ec4899; border-left-color:#ec4899;
    animation: spin 1.5s linear infinite reverse;
    box-shadow: 0 0 14px rgba(236,72,153,.35);
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .overlay p {
    font-size:14px; color:#b7a8e6; margin:0; letter-spacing:.5px;
    animation: pulse-text 2s ease-in-out infinite;
  }
  @keyframes pulse-text {
    0%,100% { opacity:.65; }
    50%     { opacity:1; }
  }
  .overlay.error .spinner { display:none; }
  .overlay.error p {
    color:#f0a0c0; font-size:15px; max-width:80%;
    text-align:center; line-height:1.5;
    animation: none;
  }

  .skip-btn {
    position:absolute; right:24px; bottom:110px; z-index:12;
    display:inline-flex; align-items:center; gap:8px;
    padding:11px 20px; border-radius:12px;
    background: linear-gradient(120deg, #a855f7, #7c3aed 60%, #ec4899);
    background-size:200% 100%;
    color:#fff; font-size:13.5px; font-weight:700;
    border:1px solid rgba(168,85,247,.5);
    cursor:pointer; font-family:inherit;
    box-shadow: 0 8px 28px rgba(124,58,237,.55),
                0 0 30px rgba(168,85,247,.35);
    opacity:0; transform: translateX(20px) scale(.92);
    pointer-events:none;
    transition: opacity .3s ease,
                transform .35s cubic-bezier(.34,1.56,.64,1);
    animation: skip-shimmer 3s linear infinite;
  }
  @keyframes skip-shimmer {
    from { background-position: 0% 0; }
    to   { background-position: 200% 0; }
  }
  .skip-btn.show {
    opacity:1; transform: translateX(0) scale(1); pointer-events:auto;
  }
  .skip-btn:hover {
    box-shadow: 0 12px 40px rgba(124,58,237,.7),
                0 0 50px rgba(168,85,247,.5);
    transform: translateX(0) scale(1.05);
  }
  .skip-btn:active { transform: translateX(0) scale(.96); }
  .skip-btn svg { width:16px; height:16px; fill:#fff; }

  .controls {
    position:absolute; left:0; right:0; bottom:0; z-index:10;
    padding:60px 22px 18px;
    background: linear-gradient(0deg,
      rgba(0,0,0,.9) 0%,
      rgba(0,0,0,.55) 40%,
      rgba(0,0,0,.15) 70%,
      transparent 100%);
    opacity:0; transform: translateY(10px);
    transition: opacity .35s ease, transform .4s cubic-bezier(.34,1.2,.64,1);
    pointer-events:none;
  }
  .controls.visible {
    opacity:1; transform: translateY(0); pointer-events:auto;
  }

  .progress-wrap {
    position:relative; width:100%; height:18px;
    display:flex; align-items:center; cursor:pointer;
    margin-bottom:8px; touch-action:none;
  }
  .progress-bg {
    position:relative; width:100%; height:4px; border-radius:4px;
    background:rgba(255,255,255,.14); overflow:visible;
    transition:height .18s cubic-bezier(.34,1.56,.64,1);
  }
  .progress-wrap:hover .progress-bg { height:7px; }
  .progress-buffer {
    position:absolute; left:0; top:0; height:100%; border-radius:4px;
    background:rgba(255,255,255,.18); width:0%;
    transition: width .25s ease;
  }
  .progress-fill {
    position:absolute; left:0; top:0; height:100%; border-radius:4px;
    background: linear-gradient(90deg,#7c3aed,#a855f7,#ec4899,#a855f7);
    background-size: 200% 100%;
    width:0%; box-shadow: 0 0 12px rgba(168,85,247,.6);
    overflow:hidden; position:relative;
    animation: fill-shift 4s linear infinite;
  }
  @keyframes fill-shift {
    from { background-position: 0% 0; }
    to   { background-position: 200% 0; }
  }
  .progress-fill::after {
    content:""; position:absolute; inset:0;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,.45), transparent);
    transform: translateX(-100%);
    animation: fill-shimmer 2s linear infinite;
  }
  @keyframes fill-shimmer {
    to { transform: translateX(100%); }
  }
  .progress-thumb {
    position:absolute; top:50%; left:0%;
    width:14px; height:14px; border-radius:50%;
    background:#c4a8ff; border:2px solid #7c3aed;
    transform: translate(-50%,-50%) scale(0);
    transition: transform .2s cubic-bezier(.34,1.56,.64,1),
                box-shadow .2s ease;
    box-shadow:0 0 12px rgba(168,85,247,.7);
    pointer-events:none;
  }
  .progress-wrap:hover .progress-thumb,
  .progress-wrap.dragging .progress-thumb {
    transform: translate(-50%,-50%) scale(1);
    box-shadow: 0 0 20px rgba(168,85,247,1), 0 0 36px rgba(168,85,247,.5);
  }

  .time-tip {
    position:absolute; bottom:26px; left:0;
    padding:3px 9px; border-radius:6px;
    background: rgba(15,10,30,.92);
    border:1px solid rgba(168,85,247,.35);
    font-size:11px; font-weight:600; color:#e0d2ff;
    pointer-events:none; opacity:0; transform: translateX(-50%) translateY(4px);
    transition: opacity .15s ease, transform .15s ease;
    white-space:nowrap;
  }
  .progress-wrap:hover .time-tip { opacity:1; transform: translateX(-50%) translateY(0); }

  .btn-row { display:flex; align-items:center; gap:8px; }
  .btn-row .spacer { flex:1; }

  .ctrl {
    background:none; border:none; padding:6px; cursor:pointer;
    color: rgba(255,255,255,.85); border-radius:10px;
    display:flex; align-items:center; justify-content:center;
    transition: color .18s, background .18s,
                transform .22s cubic-bezier(.34,1.56,.64,1),
                box-shadow .25s;
    position:relative;
  }
  .ctrl:hover {
    color:#fff; background: rgba(168,85,247,.2);
    transform: translateY(-2px) scale(1.1);
    box-shadow: 0 6px 18px rgba(168,85,247,.28);
  }
  .ctrl:active { transform: translateY(0) scale(.9); }
  .ctrl svg { width:22px; height:22px; fill:currentColor; pointer-events:none; }
  .ctrl.big svg { width:28px; height:28px; }
  .ctrl.big:hover { box-shadow: 0 6px 22px rgba(168,85,247,.5); }

  .time {
    font-size:12.5px; font-weight:600; color: rgba(255,255,255,.78);
    letter-spacing:.4px; font-variant-numeric: tabular-nums;
    user-select:none; white-space:nowrap; margin:0 4px;
  }
  .time .sep { opacity:.4; margin:0 3px; }
  .time .dur { color: rgba(255,255,255,.45); }

  .vol-group { display:flex; align-items:center; gap:0; }
  .vol-slider-wrap {
    width:0; overflow:hidden;
    transition: width .3s cubic-bezier(.4,0,.2,1);
    display:flex; align-items:center; height:28px;
  }
  .vol-group:hover .vol-slider-wrap,
  .vol-group.open .vol-slider-wrap { width:96px; }
  .vol-slider-wrap input[type=range] {
    -webkit-appearance:none; appearance:none;
    width:86px; height:4px; border-radius:4px; margin-left:6px;
    background: linear-gradient(90deg,#a855f7 var(--vol,100%),
                                       rgba(255,255,255,.18) var(--vol,100%));
    outline:none; cursor:pointer;
    transition: filter .2s;
  }
  .vol-slider-wrap input[type=range]:hover { filter: brightness(1.2); }
  .vol-slider-wrap input[type=range]::-webkit-slider-thumb {
    -webkit-appearance:none; width:14px; height:14px; border-radius:50%;
    background:#c4a8ff; border:2px solid #7c3aed;
    box-shadow:0 0 10px rgba(168,85,247,.7);
    cursor:pointer; transition: transform .15s;
  }
  .vol-slider-wrap input[type=range]::-webkit-slider-thumb:hover {
    transform: scale(1.15);
  }
  .vol-slider-wrap input[type=range]::-moz-range-thumb {
    width:14px; height:14px; border-radius:50%;
    background:#c4a8ff; border:2px solid #7c3aed; cursor:pointer;
  }

  .center-flash {
    position:absolute; top:50%; left:50%;
    width:78px; height:78px; border-radius:50%;
    background: rgba(10,8,20,.55); backdrop-filter: blur(8px);
    border:1px solid rgba(168,85,247,.45);
    display:flex; align-items:center; justify-content:center;
    opacity:0; pointer-events:none; z-index:9;
    transform: translate(-50%,-50%) scale(.7);
    box-shadow: 0 0 40px rgba(168,85,247,.35);
  }
  .center-flash.show {
    animation: flash-pop .45s cubic-bezier(.34,1.56,.64,1) forwards;
  }
  @keyframes flash-pop {
    0%   { opacity:0; transform: translate(-50%,-50%) scale(.6); }
    45%  { opacity:1; transform: translate(-50%,-50%) scale(1.1); }
    100% { opacity:1; transform: translate(-50%,-50%) scale(1); }
  }
  .center-flash svg { width:38px; height:38px; fill:#fff; }

  .settings-panel {
    position:absolute; right:22px; bottom:80px; z-index:15;
    width:264px; max-height:70vh; overflow-y:auto;
    background: rgba(12,9,24,.94); backdrop-filter: blur(20px);
    border:1px solid rgba(168,85,247,.25);
    border-radius:16px; padding:6px;
    box-shadow: 0 20px 60px rgba(0,0,0,.7),
                0 0 40px rgba(124,58,237,.18);
    opacity:0; transform: translateY(14px) scale(.94);
    pointer-events:none;
    transition: opacity .25s ease,
                transform .3s cubic-bezier(.34,1.56,.64,1);
    scrollbar-width:thin;
    scrollbar-color: rgba(168,85,247,.35) transparent;
    transform-origin: bottom right;
  }
  .settings-panel.open {
    opacity:1; transform: translateY(0) scale(1); pointer-events:auto;
  }
  .settings-panel::-webkit-scrollbar { width:6px; }
  .settings-panel::-webkit-scrollbar-thumb {
    background: rgba(168,85,247,.35); border-radius:3px;
  }

  .set-section { padding:4px 0; }
  .set-section + .set-section { border-top:1px solid rgba(255,255,255,.06); }
  .set-label {
    font-size:10.5px; font-weight:700; letter-spacing:1.4px;
    text-transform:uppercase; color: rgba(180,160,230,.65);
    padding:10px 12px 6px;
  }
  .set-item {
    display:flex; align-items:center; justify-content:space-between;
    padding:9px 12px; border-radius:10px; cursor:pointer;
    font-size:13px; font-weight:500; color: rgba(255,255,255,.85);
    transition: background .18s, color .18s,
                transform .18s cubic-bezier(.34,1.56,.64,1);
    user-select:none;
  }
  .set-item:hover {
    background: rgba(168,85,247,.16); color:#fff;
    transform: translateX(3px);
  }
  .set-item.active {
    color:#d8c4ff; background: rgba(168,85,247,.12);
  }
  .set-item.active::before {
    content:""; width:6px; height:6px; border-radius:50%;
    background:#a855f7; box-shadow: 0 0 10px #a855f7;
    margin-right:8px; flex-shrink:0;
    animation: dot-pulse 1.8s ease-in-out infinite;
  }
  @keyframes dot-pulse {
    0%,100% { box-shadow: 0 0 8px #a855f7; }
    50%     { box-shadow: 0 0 16px #a855f7, 0 0 24px rgba(168,85,247,.6); }
  }
  .set-item .val {
    font-size:11.5px; color: rgba(200,185,255,.55); font-weight:600;
  }
  .set-item.active .val { color:#a855f7; }

  .toast {
    position:absolute; bottom:88px; left:50%;
    padding:9px 20px; border-radius:12px;
    background: rgba(15,10,30,.9); backdrop-filter: blur(12px);
    border:1px solid rgba(168,85,247,.35);
    font-size:12.5px; font-weight:600; color:#e0d2ff;
    opacity:0; pointer-events:none; z-index:25;
    transition: opacity .3s ease;
    white-space:nowrap;
    transform: translateX(-50%);
    box-shadow: 0 8px 30px rgba(0,0,0,.55);
  }
  .toast.show {
    opacity:1;
    animation: toast-pop .4s cubic-bezier(.34,1.56,.64,1);
  }
  @keyframes toast-pop {
    0%   { transform: translateX(-50%) translateY(12px) scale(.9); opacity:0; }
    60%  { transform: translateX(-50%) translateY(-3px) scale(1.03); opacity:1; }
    100% { transform: translateX(-50%) translateY(0) scale(1); opacity:1; }
  }

  @media (max-width:600px) {
    .controls { padding:50px 12px 12px; }
    .settings-panel { right:12px; width: calc(100vw - 24px); bottom:74px; }
    .brand { top:12px; right:14px; }
    .brand-mark { width:28px; height:28px; }
    .brand-text { font-size:13px; }
    .skip-btn { right:12px; bottom:96px; padding:9px 15px; font-size:12.5px; }
  }
</style>
</head>
<body>
<div id="stage">
  <video id="video" playsinline crossorigin="anonymous"></video>

  <div class="brand" id="brand">
    <div class="brand-mark"><img src="https://media1.tenor.com/m/fSskkWtbhHIAAAAC/delta-eminence-in-shadow.gif" alt="ThyAnime"/></div>
    <div class="brand-text">ThyAnime</div>
  </div>

  <button class="skip-btn" id="skipBtn">
    <svg viewBox="0 0 24 24"><path d="M4 18l8.5-6L4 6v12zm9-12v12l8.5-6L13 6z"/></svg>
    <span id="skipLabel">Skip Intro</span>
  </button>

  <div class="center-flash" id="flash">
    <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
  </div>

  <div class="overlay" id="overlay">
    <div class="spinner"></div>
    <p id="overlayMsg">Resolving stream…</p>
  </div>

  <div class="toast" id="toast"></div>

  <div class="settings-panel" id="settings">
    <div class="set-section">
      <div class="set-label">Quality</div>
      <div id="qualityList"></div>
    </div>
    <div class="set-section">
      <div class="set-label">Speed</div>
      <div id="speedList"></div>
    </div>
    <div class="set-section" id="subSection" style="display:none">
      <div class="set-label">Subtitles</div>
      <div id="subList"></div>
    </div>
  </div>

  <div class="controls" id="controls">
    <div class="progress-wrap" id="progressWrap">
      <div class="progress-bg">
        <div class="progress-buffer" id="progressBuffer"></div>
        <div class="progress-fill" id="progressFill"></div>
        <div class="progress-thumb" id="progressThumb"></div>
      </div>
      <div class="time-tip" id="timeTip">0:00</div>
    </div>

    <div class="btn-row">
      <button class="ctrl big" id="btnPlay" title="Play / Pause (Space)">
        <svg viewBox="0 0 24 24" id="iconPlay"><path d="M8 5v14l11-7z"/></svg>
      </button>
      <button class="ctrl" id="btnBack" title="Back 10s (←)">
        <svg viewBox="0 0 24 24"><path d="M11.99 5V1l-5 5 5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6h-2c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"/></svg>
      </button>
      <button class="ctrl" id="btnFwd" title="Forward 10s (→)">
        <svg viewBox="0 0 24 24"><path d="M12.01 5V1l5 5-5 5V7c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6h2c0 4.42-3.58 8-8 8s-8-3.58-8-8 3.58-8 8-8z"/></svg>
      </button>
      <div class="vol-group" id="volGroup">
        <button class="ctrl" id="btnMute" title="Mute (M)">
          <svg viewBox="0 0 24 24" id="iconVol"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>
        </button>
        <div class="vol-slider-wrap">
          <input type="range" id="volSlider" min="0" max="1" step="0.02" value="1">
        </div>
      </div>
      <div class="time">
        <span id="curTime">0:00</span><span class="sep">/</span><span class="dur" id="durTime">0:00</span>
      </div>
      <div class="spacer"></div>
      <button class="ctrl" id="btnSettings" title="Settings">
        <svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.49.49 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.48.48 0 00-.48-.41h-3.84a.48.48 0 00-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 00-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg>
      </button>
      <button class="ctrl" id="btnPip" title="Picture-in-Picture (P)">
        <svg viewBox="0 0 24 24"><path d="M19 11h-8v6h8v-6zm4 8V4.98C23 3.88 22.1 3 21 3H3c-1.1 0-2 .88-2 1.98V19c0 1.1.9 2 2 2h18c1.1 0 2-.9 2-2zm-2 .02H3V4.97h18v14.05z"/></svg>
      </button>
      <button class="ctrl" id="btnFs" title="Fullscreen (F)">
        <svg viewBox="0 0 24 24" id="iconFs"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>
      </button>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js"></script>
<script>
(function(){
"use strict";
const ANILIST_ID = __ANILIST__;
const EPISODE    = __EPISODE__;
const LANG       = __LANG__;

const video     = document.getElementById('video');
const stageEl   = document.getElementById('stage');
const overlay   = document.getElementById('overlay');
const overlayMsg= document.getElementById('overlayMsg');
const controls  = document.getElementById('controls');
const settings  = document.getElementById('settings');
const progressW = document.getElementById('progressWrap');
const pFill     = document.getElementById('progressFill');
const pBuf      = document.getElementById('progressBuffer');
const pThumb    = document.getElementById('progressThumb');
const timeTip   = document.getElementById('timeTip');
const curTime   = document.getElementById('curTime');
const durTime   = document.getElementById('durTime');
const btnPlay   = document.getElementById('btnPlay');
const iconPlay  = document.getElementById('iconPlay');
const btnMute   = document.getElementById('btnMute');
const iconVol   = document.getElementById('iconVol');
const volSlider = document.getElementById('volSlider');
const btnFs     = document.getElementById('btnFs');
const iconFs    = document.getElementById('iconFs');
const flash     = document.getElementById('flash');
const toastEl   = document.getElementById('toast');
const skipBtn   = document.getElementById('skipBtn');
const skipLabel = document.getElementById('skipLabel');
const brandEl   = document.getElementById('brand');

const ICON_PLAY  = 'M8 5v14l11-7z';
const ICON_PAUSE = 'M6 19h4V5H6v14zm8-14v14h4V5h-4z';
const VOL_HIGH = 'M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z';
const VOL_MUTE = 'M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a8.99 8.99 0 003.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z';

let hls = null, hideTimer = null, dragging = false, subTracks = [], toastTimer = null;

brandEl.addEventListener('animationend', function(e){
  if(e.animationName === 'brand-in'){
    brandEl.style.animation = 'none';
  }
});

let skipSegments = [];
let activeSkip = null;

function fmt(s){ if(!isFinite(s)||s<0) s=0; s=Math.floor(s);
  const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), sec=s%60;
  return h>0?h+':'+String(m).padStart(2,'0')+':'+String(sec).padStart(2,'0')
            :m+':'+String(sec).padStart(2,'0'); }
function showToast(msg){ toastEl.textContent=msg;
  toastEl.classList.remove('show');
  void toastEl.offsetWidth;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>toastEl.classList.remove('show'),1500); }
function fail(msg){ overlay.classList.remove('hidden'); overlay.classList.add('error');
  overlayMsg.textContent=msg; }

function showControls(){ controls.classList.add('visible');
  brandEl.classList.remove('idle');
  stageEl.classList.remove('cursor-hidden');
  clearTimeout(hideTimer); hideTimer=setTimeout(hideControls,3000); }
function hideControls(){ if(video.paused) return;
  if(settings.classList.contains('open')) return; if(dragging) return;
  controls.classList.remove('visible');
  brandEl.classList.add('idle');
  stageEl.classList.add('cursor-hidden'); }

function togglePlay(){ if(video.paused) video.play().catch(()=>{}); else video.pause(); }
function flashIcon(d){
  flash.querySelector('svg path').setAttribute('d',d);
  flash.classList.remove('show');
  void flash.offsetWidth;
  flash.classList.add('show');
  setTimeout(()=>flash.classList.remove('show'), 500);
}
btnPlay.addEventListener('click', togglePlay);
video.addEventListener('click', togglePlay);
video.addEventListener('dblclick', toggleFs);
video.addEventListener('play', ()=>{ iconPlay.setAttribute('d',ICON_PAUSE); showControls(); });
video.addEventListener('pause',()=>{ iconPlay.setAttribute('d',ICON_PLAY); showControls(); });

function pctFromEvent(e){ const r=progressW.getBoundingClientRect();
  const x=(e.touches?e.touches[0].clientX:e.clientX)-r.left;
  return Math.min(1,Math.max(0,x/r.width)); }
function seekTo(p){ if(video.duration) video.currentTime=p*video.duration; }
progressW.addEventListener('mousedown', e=>{ dragging=true; progressW.classList.add('dragging'); seekTo(pctFromEvent(e)); });
window.addEventListener('mousemove', e=>{ if(dragging) seekTo(pctFromEvent(e));
  else if(e.target===progressW||progressW.contains(e.target)){ const p=pctFromEvent(e);
    timeTip.style.left=(p*100)+'%'; timeTip.textContent=fmt(p*(video.duration||0)); } });
window.addEventListener('mouseup', ()=>{ if(dragging){ dragging=false; progressW.classList.remove('dragging'); } });
progressW.addEventListener('touchstart', e=>{ dragging=true; seekTo(pctFromEvent(e)); }, {passive:true});
progressW.addEventListener('touchmove', e=>{ if(dragging) seekTo(pctFromEvent(e)); }, {passive:true});
progressW.addEventListener('touchend', ()=>{ dragging=false; });

function updateProgress(){ if(dragging) return;
  const d=video.duration||0, c=video.currentTime||0, p=d?(c/d)*100:0;
  pFill.style.width=p+'%'; pThumb.style.left=p+'%'; curTime.textContent=fmt(c); }
video.addEventListener('timeupdate', updateProgress);
video.addEventListener('durationchange', ()=>{ durTime.textContent=fmt(video.duration||0); });
video.addEventListener('progress', ()=>{ if(!video.buffered.length||!video.duration) return;
  const end=video.buffered.end(video.buffered.length-1);
  pBuf.style.width=(end/video.duration*100)+'%'; });

function skipLabelFor(type){
  if(type === 'op' || type === 'mixed-op') return 'Skip Intro';
  if(type === 'ed' || type === 'mixed-ed') return 'Skip Outro';
  if(type === 'recap') return 'Skip Recap';
  return 'Skip';
}

function checkSkip(){
  const t = video.currentTime;
  const seg = skipSegments.find(s => t >= s.start && t < s.end);
  if(seg){
    activeSkip = seg;
    skipLabel.textContent = skipLabelFor(seg.type);
    skipBtn.classList.add('show');
  } else {
    activeSkip = null;
    skipBtn.classList.remove('show');
  }
}
video.addEventListener('timeupdate', checkSkip);

skipBtn.addEventListener('click', ()=>{
  if(activeSkip){
    video.currentTime = activeSkip.end;
    skipBtn.classList.remove('show');
    showToast(skipLabelFor(activeSkip.type) + ' ✓');
    activeSkip = null;
  }
});

function whenDurationKnown(){
  return new Promise(resolve => {
    if(video.duration && isFinite(video.duration) && video.duration > 0){
      return resolve(video.duration);
    }
    const onDur = () => {
      if(video.duration && isFinite(video.duration) && video.duration > 0){
        video.removeEventListener('durationchange', onDur);
        resolve(video.duration);
      }
    };
    video.addEventListener('durationchange', onDur);
    setTimeout(() => {
      video.removeEventListener('durationchange', onDur);
      resolve(video.duration && isFinite(video.duration) ? video.duration : 0);
    }, 8000);
  });
}

async function loadSkipTimes(){
  try{
    const dur = await whenDurationKnown();
    if(!dur || dur <= 0 || !isFinite(dur)){
      console.log('[skip] unknown episode length — cannot query AniSkip');
      return;
    }
    const url = '/api/skip/' + ANILIST_ID + '/' + EPISODE +
                '?length=' + dur.toFixed(3);
    console.log('[skip] fetching', url);
    const r = await fetch(url);
    if(!r.ok){
      console.warn('[skip] /api/skip returned', r.status);
      return;
    }
    const data = await r.json();
    if(data && data.found && Array.isArray(data.results)){
      skipSegments = data.results
        .filter(x => x && x.interval && typeof x.interval.startTime === 'number')
        .map(x => ({
          start: x.interval.startTime,
          end: x.interval.endTime,
          type: x.skipType || 'op'
        }));
      console.log('[skip] loaded', skipSegments.length, 'segments', skipSegments);
    } else {
      console.log('[skip] no skip data for this episode', data);
    }
  }catch(e){ console.warn('[skip] unavailable:', e.message); }
}

let lastVol=1;
function applyVol(v){ video.volume=v; video.muted=v===0; volSlider.value=v;
  volSlider.style.setProperty('--vol',(v*100)+'%'); updateVolIcon(); }
function updateVolIcon(){ iconVol.querySelector('path').setAttribute('d',
  (video.muted||video.volume===0)?VOL_MUTE:VOL_HIGH); }
volSlider.addEventListener('input', ()=>{ lastVol=parseFloat(volSlider.value); applyVol(lastVol); });
btnMute.addEventListener('click', ()=>{ if(video.muted||video.volume===0) applyVol(lastVol||0.6);
  else { lastVol=video.volume; applyVol(0); } });
video.addEventListener('volumechange', updateVolIcon);
applyVol(1);

function toggleFs(){ const d=document;
  if(!d.fullscreenElement){ const el=document.getElementById('stage');
    (el.requestFullscreen||el.webkitRequestFullscreen).call(el); }
  else (d.exitFullscreen||d.webkitExitFullscreen).call(d); }
btnFs.addEventListener('click', toggleFs);
document.addEventListener('fullscreenchange', ()=>{ const fs=!!document.fullscreenElement;
  iconFs.querySelector('path').setAttribute('d',
    fs?'M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z'
      :'M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z'); });

document.getElementById('btnPip').addEventListener('click', async ()=>{
  try { if(document.pictureInPictureElement) await document.exitPictureInPicture();
        else await video.requestPictureInPicture(); }
  catch(e){ showToast('PiP unavailable'); } });

document.getElementById('btnSettings').addEventListener('click', e=>{
  e.stopPropagation(); settings.classList.toggle('open');
  if(settings.classList.contains('open')) clearTimeout(hideTimer); });
document.addEventListener('click', e=>{
  if(!settings.contains(e.target) && !document.getElementById('btnSettings').contains(e.target))
    settings.classList.remove('open'); });

function buildQualityList(){ const list=document.getElementById('qualityList'); list.innerHTML='';
  if(!hls||!hls.levels||hls.levels.length<2){
    list.innerHTML='<div class="set-item" style="opacity:.45;pointer-events:none">Single quality</div>'; return; }
  const auto=document.createElement('div');
  auto.className='set-item'+(hls.currentLevel===-1?' active':'');
  auto.innerHTML='<span>Auto</span><span class="val">ABR</span>';
  auto.onclick=()=>{ hls.currentLevel=-1; buildQualityList(); showToast('Quality: Auto'); };
  list.appendChild(auto);
  hls.levels.map((l,i)=>({l,i})).sort((a,b)=>(b.l.height||0)-(a.l.height||0))
    .forEach(({l,i})=>{ const item=document.createElement('div');
      item.className='set-item'+(hls.currentLevel===i?' active':'');
      item.innerHTML='<span>'+(l.height?l.height+'p':'Level '+i)+'</span>'+
                     '<span class="val">'+Math.round((l.bitrate||0)/1000)+'k</span>';
      item.onclick=()=>{ hls.currentLevel=i; buildQualityList();
        showToast('Quality: '+(l.height?l.height+'p':'Level '+i)); };
      list.appendChild(item); }); }

const SPEEDS=[0.25,0.5,0.75,1,1.25,1.5,1.75,2];
function buildSpeedList(){ const list=document.getElementById('speedList'); list.innerHTML='';
  const cur=video.playbackRate;
  SPEEDS.forEach(s=>{ const item=document.createElement('div');
    item.className='set-item'+(Math.abs(cur-s)<0.01?' active':'');
    item.innerHTML='<span>'+s+'×</span>'+(s===1?'<span class="val">Normal</span>':'');
    item.onclick=()=>{ video.playbackRate=s; buildSpeedList(); showToast('Speed: '+s+'×'); };
    list.appendChild(item); }); }

function _onlyOneShowing(active){
  subTracks.forEach(t => {
    if(t !== active && t.mode === 'showing'){
      try { t.mode = 'disabled'; } catch(e){}
    }
  });
}

function buildSubList(){
  const sec=document.getElementById('subSection');
  const list=document.getElementById('subList');
  if(!subTracks.length){ sec.style.display='none'; return; }
  sec.style.display=''; list.innerHTML='';
  const off=document.createElement('div');
  off.className='set-item'+(subTracks.every(t=>t.mode!=='showing')?' active':'');
  off.innerHTML='<span>Off</span>';
  off.onclick=()=>{
    subTracks.forEach(t=>{ try { t.mode='disabled'; } catch(e){} });
    buildSubList();
    showToast('Subtitles: Off');
  };
  list.appendChild(off);
  subTracks.forEach((t,i)=>{
    const item=document.createElement('div');
    item.className='set-item'+(t.mode==='showing'?' active':'');
    item.innerHTML='<span>'+(t.label||('Track '+(i+1)))+'</span>'+
                   '<span class="val">'+(t.language||'')+'</span>';
    item.onclick=()=>{
      subTracks.forEach(x=>{ try { x.mode='disabled'; } catch(e){} });
      try { t.mode='showing'; } catch(e){}
      _onlyOneShowing(t);
      buildSubList();
      showToast('Subtitles: '+(t.label||'Track'));
    };
    list.appendChild(item);
  });
}

function addSubtitles(subs){
  video.querySelectorAll('track').forEach(t=>t.remove());
  subTracks = [];

  if(!subs || !subs.length){ buildSubList(); return; }

  subs.forEach((s,i)=>{
    const t=document.createElement('track');
    t.kind='subtitles';
    t.label=s.lang || ('Track '+(i+1));
    t.srclang=((s.lang||'und').slice(0,2)||'und').toLowerCase();
    t.src=s.url;
    t.default = false;
    video.appendChild(t);
  });

  const want = subs.length;
  let tries = 0;
  (function waitTracks(){
    tries++;
    const list = Array.from(video.textTracks);
    if(list.length >= want || tries > 40){
      finalize(list);
    } else {
      setTimeout(waitTracks, 50);
    }
  })();

  function finalize(list){
    subTracks = list;
    subTracks.forEach(t => { try { t.mode='disabled'; } catch(e){} });
    subTracks.forEach(t => {
      t.addEventListener('change', () => {
        if(t.mode === 'showing') _onlyOneShowing(t);
      });
    });
    const isEng = t => {
      const lab=(t.label||'').toLowerCase();
      const lng=(t.language||'').toLowerCase();
      return lng.startsWith('en') || lab.includes('english') || lab.includes('eng');
    };
    const preferred = subTracks.find(isEng) || subTracks[0];
    if(preferred){
      try { preferred.mode='showing'; _onlyOneShowing(preferred); } catch(e){}
    }
    buildSubList();
  }
}

document.addEventListener('keydown', e=>{
  if(['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) return;
  switch(e.key){
    case ' ': case 'k': e.preventDefault(); togglePlay(); flashIcon(video.paused?ICON_PAUSE:ICON_PLAY); break;
    case 'ArrowLeft': e.preventDefault(); video.currentTime=Math.max(0,video.currentTime-10); showToast('⏪ 10s'); break;
    case 'ArrowRight': e.preventDefault(); video.currentTime=Math.min(video.duration||0,video.currentTime+10); showToast('10s ⏩'); break;
    case 'ArrowUp': e.preventDefault(); applyVol(Math.min(1,video.volume+0.05)); break;
    case 'ArrowDown': e.preventDefault(); applyVol(Math.max(0,video.volume-0.05)); break;
    case 'm': case 'M': btnMute.click(); break;
    case 'f': case 'F': toggleFs(); break;
    case 'p': case 'P': document.getElementById('btnPip').click(); break;
    case 'Escape': settings.classList.remove('open'); break; } });

document.getElementById('btnBack').addEventListener('click', ()=>{
  video.currentTime=Math.max(0,video.currentTime-10); showToast('⏪ 10s'); });
document.getElementById('btnFwd').addEventListener('click', ()=>{
  video.currentTime=Math.min(video.duration||0,video.currentTime+10); showToast('10s ⏩'); });

let lastMove=0;
function activity(){ const now=Date.now(); if(now-lastMove<120) return; lastMove=now; showControls(); }
stageEl.addEventListener('mousemove', activity);
stageEl.addEventListener('touchstart', activity, {passive:true});
stageEl.addEventListener('mouseleave', ()=>{ if(!video.paused){
  controls.classList.remove('visible');
  brandEl.classList.add('idle');
  stageEl.classList.add('cursor-hidden');
} });

function attach(src, subs){
  if(window.Hls && Hls.isSupported()){
    hls=new Hls({ enableWorker:true, lowLatencyMode:false, startLevel:-1 });
    hls.loadSource(src); hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, ()=>{ overlay.classList.add('hidden');
      addSubtitles(subs); buildQualityList(); buildSpeedList();
      loadSkipTimes();
      video.play().catch(()=>{}); showControls(); });
    hls.on(Hls.Events.LEVEL_SWITCHED, ()=>buildQualityList());
    hls.on(Hls.Events.ERROR, (_,data)=>{ if(data&&data.fatal)
      fail('Playback error: '+data.type+' / '+data.details); });
  } else if(video.canPlayType('application/vnd.apple.mpegurl')){
    video.src=src;
    video.addEventListener('loadedmetadata', ()=>{ overlay.classList.add('hidden');
      addSubtitles(subs); buildSpeedList();
      loadSkipTimes();
      video.play().catch(()=>{}); showControls(); }, {once:true});
    video.addEventListener('error', ()=>fail('Native playback error'));
  } else fail('HLS is not supported in this browser.'); }

(async function init(){
  try { overlayMsg.textContent='Resolving stream…';
    const r=await fetch('/api/resolve/'+ANILIST_ID+'/'+EPISODE+'/'+LANG);
    if(!r.ok){ const err=await r.json().catch(()=>({}));
      return fail(err.error||('Resolve failed ('+r.status+')')); }
    const data=await r.json();
    overlayMsg.textContent='Loading segments…';
    attach(data.m3u8, data.subtitles||[]);
  } catch(e){ fail('Network error: '+e.message); }
})();
})();
</script>
</body>
</html>
"""

# ═══════════════════════════════════════════════════════════════
#  DOCS PAGE
# ═══════════════════════════════════════════════════════════════
DOCS_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ThyAnime · Docs</title>
<style>
  :root { --bg:#080611; --panel:#100d1f; --border:#251f42;
    --accent:#a855f7; --accent-2:#7c3aed; --text:#ecebff; --muted:#948fb8;
    --code-bg:#0d0a1c; }
  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--text);
    font-family:'Inter','Segoe UI',system-ui,-apple-system,sans-serif;
    -webkit-font-smoothing:antialiased;}
  body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
    background:radial-gradient(circle at 15% 0%, rgba(168,85,247,0.13), transparent 55%),
               radial-gradient(circle at 85% 100%, rgba(124,58,237,0.10), transparent 55%);}
  .wrap{position:relative;z-index:1;max-width:960px;margin:0 auto;padding:48px 24px 100px;}
  .logo{display:inline-flex;align-items:center;gap:10px;font-weight:800;
    font-size:24px;letter-spacing:.4px;margin-bottom:6px;}
  .logo-mark{width:34px;height:34px;border-radius:50%;
    background:conic-gradient(from 180deg,#a855f7,#6366f1,#ec4899,#a855f7);
    display:flex;align-items:center;justify-content:center;font-size:15px;
    font-weight:900;box-shadow:0 0 20px rgba(168,85,247,.5);}
  .logo-text{background:linear-gradient(120deg,#e9d5ff,#a855f7 45%,#7c3aed 75%,#ec4899);
    -webkit-background-clip:text;background-clip:text;color:transparent;}
  h1{font-size:32px;margin:24px 0 8px;font-weight:800;letter-spacing:-.5px;}
  .lede{color:var(--muted);font-size:15px;line-height:1.65;margin:0 0 40px;max-width:680px;}
  h2{font-size:19px;margin:44px 0 14px;font-weight:700;display:flex;align-items:center;gap:10px;}
  h2::before{content:"";width:4px;height:18px;border-radius:4px;
    background:linear-gradient(180deg,var(--accent),var(--accent-2));}
  p{color:#c8c3e0;font-size:14px;line-height:1.7;}
  .ep{background:var(--panel);border:1px solid var(--border);
    border-radius:14px;padding:18px 20px;margin:14px 0;transition:border-color .18s;}
  .ep:hover{border-color:rgba(168,85,247,.4);}
  .ep-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px;}
  .method{font-size:11px;font-weight:800;letter-spacing:.8px;padding:3px 9px;
    border-radius:6px;color:#fff;}
  .method.get{background:#2563eb;}
  .path{font-family:'JetBrains Mono','Fira Code',monospace;font-size:13.5px;
    font-weight:600;color:#d8caff;}
  .desc{color:var(--muted);font-size:13.5px;margin:0 0 12px;}
  pre{background:var(--code-bg);border:1px solid var(--border);border-radius:9px;
    padding:14px 16px;overflow-x:auto;font-family:'JetBrains Mono','Fira Code',monospace;
    font-size:12.5px;line-height:1.6;color:#c9c2e8;margin:10px 0 0;}
  pre .k{color:#c4a8ff;} pre .s{color:#8be9b0;}
  code{background:rgba(168,85,247,.13);color:#d8caff;padding:2px 7px;border-radius:5px;
    font-family:'JetBrains Mono','Fira Code',monospace;font-size:12.5px;}
  footer{margin-top:70px;padding-top:22px;border-top:1px solid var(--border);
    color:var(--muted);font-size:12.5px;text-align:center;}
  .pill{display:inline-block;font-size:11px;font-weight:700;padding:3px 9px;
    border-radius:999px;margin-left:6px;background:rgba(168,85,247,.14);
    color:#c4a8ff;border:1px solid rgba(168,85,247,.28);}
</style>
</head>
<body>
<div class="wrap">
  <div class="logo">
    <div class="logo-mark">T</div>
    <span class="logo-text">ThyAnime</span>
    <span class="pill">API v1</span>
  </div>
  <h1>Developer Docs</h1>
  <p class="lede">
    ThyAnime is a MegaPlay stream proxy with a custom HLS player and an
    AniList-powered frontend. All endpoints are open and CORS-enabled. Every
    m3u8, segment, AES key, and subtitle is proxied through this server so
    nothing is stored on disk.
  </p>

  <h2>Endpoints</h2>

  <div class="ep">
    <div class="ep-head"><span class="method get">GET</span>
      <span class="path">/embed/&lt;anilist_id&gt;/&lt;episode&gt;/&lt;sub|dub&gt;</span></div>
    <p class="desc">Self-contained HLS player with custom controls
      (quality, speed, volume, PiP, fullscreen, subtitles) and an
      auto-detected skip-intro/outro button powered by AniSkip. Use as an
      iframe <code>src</code>.</p>
    <pre>GET /embed/21/1/sub</pre>
  </div>

  <div class="ep">
    <div class="ep-head"><span class="method get">GET</span>
      <span class="path">/api/resolve/&lt;anilist_id&gt;/&lt;episode&gt;/&lt;sub|dub&gt;</span></div>
    <p class="desc">Resolves the stream and returns JSON with the proxied
      m3u8 URL and subtitle tracks. Cached 30 minutes.</p>
    <pre>{
  <span class="k">"m3u8"</span>: <span class="s">"/proxy?url=https%3A%2F%2F..."</span>,
  <span class="k">"subtitles"</span>: [
    { <span class="k">"url"</span>: <span class="s">"/proxy?url=..."</span>, <span class="k">"lang"</span>: <span class="s">"English"</span> }
  ]
}</pre>
  </div>

  <div class="ep">
    <div class="ep-head"><span class="method get">GET</span>
      <span class="path">/api/skip/&lt;anilist_id&gt;/&lt;episode&gt;?length=&lt;seconds&gt;</span></div>
    <p class="desc">Returns AniSkip intro/outro/recap timestamps. The
      <code>length</code> query param (episode runtime in seconds) is
      <b>required</b> by AniSkip v2. The server resolves the AniList ID to a
      MAL ID, then queries <code>api.aniskip.com/v2</code>. Cached 1 hour.</p>
    <pre>{
  <span class="k">"found"</span>: <span class="s">true</span>,
  <span class="k">"results"</span>: [
    {
      <span class="k">"interval"</span>: { <span class="k">"startTime"</span>: <span class="s">1321.01</span>, <span class="k">"endTime"</span>: <span class="s">1401.16</span> },
      <span class="k">"skipType"</span>: <span class="s">"ed"</span>,
      <span class="k">"skipId"</span>: <span class="s">"a6ab121c-..."</span>,
      <span class="k">"episodeLength"</span>: <span class="s">1417.16</span>
    }
  ]
}</pre>
  </div>

  <div class="ep">
    <div class="ep-head"><span class="method get">GET</span>
      <span class="path">/api/anime/&lt;anilist_id&gt;</span></div>
    <p class="desc">Raw AniList metadata for the given ID.</p>
    <pre>GET /api/anime/21</pre>
  </div>

  <div class="ep">
    <div class="ep-head"><span class="method get">GET</span>
      <span class="path">/proxy?url=&lt;encoded_url&gt;</span></div>
    <p class="desc">Proxies any MegaPlay/CDN resource. m3u8 playlists are
      rewritten so every child segment and key URI routes through this
      endpoint. Forwards <code>Range</code> headers.</p>
    <pre>GET /proxy?url=https%3A%2F%2Fcdn.example%2Fmaster.m3u8</pre>
  </div>

  <div class="ep">
    <div class="ep-head"><span class="method get">GET</span>
      <span class="path">/api/debug/&lt;anilist_id&gt;/&lt;episode&gt;/&lt;sub|dub&gt;</span></div>
    <p class="desc">Diagnostic JSON dump of the embed HTML head, extracted
      <code>data-id</code>, and raw <code>getSources</code> response.</p>
    <pre>GET /api/debug/21/1/sub</pre>
  </div>

  <div class="ep">
    <div class="ep-head"><span class="method get">GET</span>
      <span class="path">/__thy</span></div>
    <p class="desc">Server fingerprint. Used internally to verify the
      tunnel is actually pointing at this process.</p>
  </div>

  <div class="ep">
    <div class="ep-head"><span class="method get">GET</span>
      <span class="path">/health</span></div>
    <p class="desc">Health check: <code>{ "ok": true, "crypto": true }</code>.</p>
  </div>

  <h2>Player Keyboard Shortcuts</h2>
  <p>
    <code>Space</code> / <code>K</code> — play / pause<br>
    <code>←</code> / <code>→</code> — seek ±10s<br>
    <code>↑</code> / <code>↓</code> — volume ±5%<br>
    <code>M</code> — mute &nbsp;·&nbsp; <code>F</code> — fullscreen
    &nbsp;·&nbsp; <code>P</code> — picture-in-picture<br>
    <code>Esc</code> — close settings panel
  </p>

  <h2>Skip Times (AniSkip)</h2>
  <p>
    The player automatically fetches intro/outro/recap timestamps from
    <a href="https://api.aniskip.com/api-docs" style="color:#c4a8ff">AniSkip</a>
    once the video's runtime is known. When you reach an intro, outro, or
    recap segment, a <b>Skip</b> button fades in on the right side of the
    player. Clicking it seeks to the end of the segment.
  </p>

  <footer>ThyAnime · built on MegaPlay + AniList + AniSkip · powered by hls.js</footer>
</div>
</body>
</html>
"""

# ═══════════════════════════════════════════════════════════════
#  HTTP HANDLER
# ═══════════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    server_version = "ThyAnime/1.0"

    def log_message(self, fmt, *args):
        msg = fmt % args
        if " /proxy" in msg or " /__thy" in msg or " / " in msg:
            return
        parts = msg.split('"')
        if len(parts) >= 3:
            code = parts[2].strip().split()[0]
            color = "\033[32m" if code.startswith("2") else \
                    "\033[33m" if code.startswith("3") else "\033[31m"
            sys.stderr.write(f"  \033[90m│\033[0m  http   {parts[1]:<48} {color}{code}\033[0m\n")
        else:
            sys.stderr.write(f"  \033[90m│\033[0m  http   {msg}\n")

    def _send(self, status, body, ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str): body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("X-ThyAnime", "1")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _empty(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-ThyAnime", "1")
        self.end_headers()

    def _json(self, status, obj):
        self._send(status, json.dumps(obj), "application/json; charset=utf-8")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        query  = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                return self._empty()
            if path == "/docs":
                return self._send(200, DOCS_TEMPLATE, "text/html; charset=utf-8")
            if path == "/__thy":
                return self._json(200, {"server": "thy", "ok": True,
                                        "crypto": _HAS_CRYPTO})
            if path == "/health":
                return self._json(200, {"ok": True, "crypto": _HAS_CRYPTO})
            if path == "/robots.txt":
                return self._send(200, "User-agent: *\nDisallow: /\n",
                                  "text/plain; charset=utf-8")
            if path.startswith("/api/anime/"):
                return self._api_anime(path)
            if path.startswith("/api/resolve/"):
                return self._api_resolve(path)
            if path.startswith("/api/skip/"):
                return self._api_skip(path)
            if path.startswith("/api/debug/"):
                return self._api_debug(path)
            if path.startswith("/embed/"):
                return self._embed(path)
            if path == "/proxy":
                return self._proxy(query)
            return self._send(404, "not found")
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send(500, f"server error: {e}")

    def _api_anime(self, path):
        try:
            aid = int(path.rsplit("/", 1)[-1])
        except ValueError:
            return self._json(400, {"error": "bad id"})
        media = get_anilist_media(aid)
        if not media:
            return self._json(404, {"error": "not found"})
        return self._json(200, media)

    def _api_resolve(self, path):
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            return self._json(400, {"error": "bad path"})
        try:
            aid, ep = int(parts[2]), int(parts[3])
            lang = parts[4]
        except ValueError:
            return self._json(400, {"error": "bad params"})
        if lang not in ("sub", "dub"):
            return self._json(400, {"error": "lang must be sub or dub"})

        key = (aid, ep, lang)
        cached = cache_get(key)
        if cached:
            print(f"  \033[90m│\033[0m  cache  hit  {key}")
            return self._json(200, cached)

        stream = resolve_stream(aid, ep, lang)
        if not stream:
            return self._json(404, {
                "error": "Could not resolve stream — try /api/debug/%d/%d/%s" % (aid, ep, lang)
            })

        subs = [{"url": "/proxy?url=" + urllib.parse.quote(s["url"], safe=""),
                 "lang": s["lang"]}
                for s in stream.get("subtitles", [])]
        out = {"m3u8": "/proxy?url=" + urllib.parse.quote(stream["m3u8"], safe=""),
               "subtitles": subs,
               "malId": stream.get("malId")}
        cache_set(key, out)
        return self._json(200, out)

    def _api_skip(self, path):
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            return self._json(400, {"error": "path must be /api/skip/<id>/<ep>"})
        try:
            aid, ep = int(parts[2]), int(parts[3])
        except ValueError:
            return self._json(400, {"error": "bad params"})

        ep_len = None
        qs = urllib.parse.urlparse(self.path).query
        if qs:
            q = urllib.parse.parse_qs(qs)
            try:
                ep_len = float((q.get("length") or [""])[0])
            except (ValueError, TypeError):
                ep_len = None

        if not ep_len or ep_len <= 0:
            print(f"  \033[90m│\033[0m  aniskip  \033[33mskipped\033[0m "
                  f"(missing/invalid length for aid={aid} ep={ep})")
            return self._json(200, {"found": False, "results": []})

        key = ("skip", aid, ep, round(ep_len, 1))
        cached = cache_get(key)
        if cached:
            print(f"  \033[90m│\033[0m  cache  hit  skip {aid}/{ep}")
            return self._json(200, cached)

        media = get_anilist_media(aid)
        if not media:
            return self._json(404, {"error": "anilist lookup failed"})
        mal_id = media.get("idMal")
        if not mal_id:
            return self._json(404, {"error": "no MAL id for this anime"})

        print(f"  \033[90m│\033[0m  aniskip  mal=\033[33m{mal_id}\033[0m "
              f"ep=\033[33m{ep}\033[0m len=\033[33m{ep_len:.1f}\033[0m")
        result = get_skip_times(mal_id, ep, ep_len)
        if result is None:
            return self._json(200, {"found": False, "results": []})

        cache_set(key, result, ttl=3600)
        return self._json(200, result)

    def _api_debug(self, path):
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            return self._json(400, {"error": "path must be /api/debug/<id>/<ep>/<lang>"})
        try:
            aid, ep = int(parts[2]), int(parts[3])
            lang = parts[4]
        except ValueError:
            return self._json(400, {"error": "bad params"})
        media = get_anilist_media(aid)
        mal_id = (media or {}).get("idMal")
        urls = [f"{MEGAPLAY_BASE}/stream/ani/{aid}/{ep}/{lang}"]
        if mal_id:
            urls.append(f"{MEGAPLAY_BASE}/stream/mal/{mal_id}/{ep}/{lang}")
        report = {"anilist": media, "tries": []}
        for u in urls:
            html, reason = fetch_embed(u)
            entry = {"url": u, "ok": bool(html), "reason": reason}
            if html:
                entry["bytes"] = len(html)
                entry["data_id"] = extract_data_id(html)
                entry["html_head"] = html[:800]
                if entry["data_id"]:
                    type_val = "2" if lang == "dub" else "1"
                    try:
                        r = HTTP.get(f"{MEGAPLAY_BASE}/stream/getSources",
                                     params={"id": entry["data_id"], "type": type_val},
                                     headers={**BASE_HEADERS, "Referer": u,
                                              "Origin": MEGAPLAY_BASE,
                                              "X-Requested-With": "XMLHttpRequest"},
                                     timeout=15)
                        entry["getSources_status"] = r.status_code
                        entry["getSources_ct"] = r.headers.get("content-type")
                        entry["getSources_body"] = r.text[:1500]
                    except Exception as e:
                        entry["getSources_error"] = str(e)
            report["tries"].append(entry)
        return self._json(200, report)

    def _embed(self, path):
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            return self._send(400, "bad embed path")
        try:
            aid, ep = int(parts[1]), int(parts[2])
        except ValueError:
            return self._send(400, "bad embed params")
        lang = parts[3]
        if lang not in ("sub", "dub"):
            return self._send(400, "lang must be sub or dub")
        html = (PLAYER_TEMPLATE
                .replace("__ANILIST__", str(aid))
                .replace("__EPISODE__", str(ep))
                .replace("__LANG__", json.dumps(lang)))
        self._send(200, html, "text/html; charset=utf-8")

    def _proxy(self, query):
        url = (query.get("url") or [""])[0].strip()
        if not url.startswith(("http://", "https://")):
            return self._send(400, "invalid url")
        upstream_headers = {
            "User-Agent": USER_AGENT,
            "Referer": MEGAPLAY_BASE + "/",
            "Origin": MEGAPLAY_BASE,
            "Accept": "*/*",
        }
        if self.headers.get("Range"):
            upstream_headers["Range"] = self.headers["Range"]
        try:
            r = HTTP.get(url, headers=upstream_headers, stream=True,
                         timeout=30, allow_redirects=True)
        except Exception as e:
            return self._send(502, f"upstream error: {e}")
        ct  = (r.headers.get("content-type") or "").lower()
        low = url.lower()
        is_playlist = "mpegurl" in ct or low.endswith(".m3u8") or ".m3u8?" in low
        is_vtt      = "vtt" in ct or low.endswith(".vtt") or ".vtt?" in low
        if is_playlist:
            body = _rewrite_playlist(r.text, r.url)
            return self._send(200, body, "application/vnd.apple.mpegurl",
                              {"Cache-Control": "no-cache"})
        if is_vtt:
            return self._send(r.status_code, r.content, "text/vtt; charset=utf-8")
        content = r.content
        extra = {}
        for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
            if h in r.headers:
                extra[h] = r.headers[h]
        return self._send(r.status_code, content,
                          r.headers.get("content-type", "application/octet-stream"),
                          extra)

# ═══════════════════════════════════════════════════════════════
#  CLOUDFLARE TOKEN TUNNEL
# ═══════════════════════════════════════════════════════════════
def start_token_tunnel() -> Optional[str]:
    """Run cloudflared using the Zero Trust tunnel token.

    The token embeds the tunnel ID + secret. All ingress routes
    (hostname → local service) come from the Zero Trust dashboard.

    Readiness is determined by cloudflared's own log output —
    when it prints "Registered tunnel connection", the tunnel is up.
    We do NOT rely on the local machine's DNS resolver being able to
    reach the public hostname, because that's frequently broken
    (stale cache, split-horizon DNS, DoH-only browsers, etc.) even
    when the tunnel itself works perfectly.
    """
    global _CLOUDFLARED_PROC

    if not shutil.which("cloudflared"):
        print("  \033[90m│\033[0m  cloudflared  \033[31mnot installed\033[0m")
        print("  \033[90m│\033[0m    install: winget install Cloudflare.cloudflared")
        print("  \033[90m│\033[0m    or:      brew install cloudflared")
        return None

    if not TUNNEL_TOKEN or not TUNNEL_TOKEN.strip():
        print("  \033[90m│\033[0m  cloudflared  \033[31mno TUNNEL_TOKEN set\033[0m")
        return None

    # Reset readiness state from any previous run
    _CLOUDFLARED_CONNECTED.clear()
    _CLOUDFLARED_CONN_COUNT["value"] = 0

    # cloudflared reads the token via env var so it never appears in argv
    # (which is visible in `ps` / Task Manager to other users).
    env = os.environ.copy()
    env["TUNNEL_TOKEN"] = TUNNEL_TOKEN

    cmd = ["cloudflared", "tunnel", "--no-autoupdate", "run"]
    print(f"  \033[90m│\033[0m  cloudflared  \033[90mrunning: cloudflared tunnel run (token)\033[0m")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
    except Exception as e:
        print(f"  \033[90m│\033[0m  cloudflared  \033[31mspawn failed: {e}\033[0m")
        return None

    # Wait a moment and check it didn't immediately die
    time.sleep(3)
    if proc.poll() is not None:
        out = ""
        try:
            out = proc.stdout.read()[:800]
        except Exception:
            pass
        print(f"  \033[90m│\033[0m  cloudflared  \033[31mexited early\033[0m")
        if out:
            for line in out.strip().splitlines()[:6]:
                print(f"  \033[90m│\033[0m    {line}")
        return None

    def _drain(p):
        try:
            for line in p.stdout:
                low = line.lower()
                # The single definitive signal that the tunnel is live:
                if "registered tunnel connection" in low:
                    _CLOUDFLARED_CONN_COUNT["value"] += 1
                    _CLOUDFLARED_CONNECTED.set()
                    sys.stderr.write(f"  \033[90m│\033[0m  cloudflared  "
                                     f"\033[32m{line.strip()}\033[0m\n")
                elif "error" in low or "fatal" in low or "failed" in low:
                    sys.stderr.write(f"  \033[90m│\033[0m  cloudflared  "
                                     f"\033[31m{line.strip()}\033[0m\n")
        except Exception:
            pass
    threading.Thread(target=_drain, args=(proc,), daemon=True).start()
    _CLOUDFLARED_PROC = proc

    return f"https://{PUBLIC_HOSTNAME}" if PUBLIC_HOSTNAME else None

# ── optional HTTP verification (best-effort, non-blocking) ──
def verify_tunnel(url: str, retries: int = 5, delay: int = 3) -> bool:
    """Try to reach the tunnel's /__thy endpoint.

    This is BEST-EFFORT and non-blocking for readiness — the tunnel
    is considered up as soon as cloudflared reports a registered
    connection. This function exists purely to give an extra green
    checkmark when the local machine's DNS happens to cooperate.
    """
    fp = url.rstrip("/") + "/__thy"
    for i in range(retries):
        try:
            r = requests.get(
                fp,
                timeout=8,
                headers={"User-Agent": "ThyAnime-verify/1.0"},
            )
            if r.status_code == 200 and '"server"' in r.text and '"thy"' in r.text:
                return True
        except Exception:
            pass
        time.sleep(delay)
    return False

def tunnel_watchdog(url: str):
    """Periodically check the tunnel; restart if cloudflared dies.

    Health is judged by whether the cloudflared process is still
    alive, NOT by outbound HTTP — DNS from this machine may be
    broken while the tunnel is perfectly fine.
    """
    while True:
        time.sleep(90)
        if _CLOUDFLARED_PROC is None or _CLOUDFLARED_PROC.poll() is not None:
            print(f"  \033[90m│\033[0m  watchdog  cloudflared died — restarting…")
            try:
                new_url = start_token_tunnel()
                if new_url:
                    PUBLIC_URL["value"] = new_url
                    if _CLOUDFLARED_CONNECTED.wait(timeout=40):
                        print(f"  \033[90m│\033[0m  watchdog  "
                              f"\033[32mreconnected ✓\033[0m  {new_url}")
                        url = new_url
                        continue
            except Exception as e:
                print(f"  \033[90m│\033[0m  watchdog  restart failed: {e}")

# ─────────────────────────── main ───────────────────────────
def banner():
    P = "\033[38;5;141m"; D = "\033[38;5;99m"; R = "\033[0m"
    print()
    print(f"  {D}╭──────────────────────────────────────────────────────────╮{R}")
    print(f"  {D}│{R}  {P}████████╗██╗  ██╗██╗   ██╗ █████╗ ███╗   ██╗██╗███╗   ███╗███████╗{R}")
    print(f"  {D}│{R}  {P}╚══██╔══╝██║  ██║╚██╗ ██╔╝██╔══██╗████╗  ██║██║████╗ ████║██╔════╝{R}")
    print(f"  {D}│{R}  {P}   ██║   ███████║ ╚████╔╝ ███████║██╔██╗ ██║██║██╔████╔██║█████╗  {R}")
    print(f"  {D}│{R}  {P}   ██║   ██╔══██║  ╚██╔╝  ██╔══██║██║╚██╗██║██║██║╚██╔╝██║██╔══╝  {R}")
    print(f"  {D}│{R}  {P}   ██║   ██║  ██║   ██║   ██║  ██║██║ ╚████║██║██║ ╚═╝ ██║███████╗{R}")
    print(f"  {D}│{R}  {P}   ╚═╝   ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝╚═╝     ╚═╝╚══════╝{R}")
    print(f"  {D}│{R}")
    print(f"  {D}│{R}  \033[90mMegaPlay proxy  ·  custom HLS player  ·  AniList + AniSkip\033[0m")
    print(f"  {D}│{R}  \033[90mTunnel: Cloudflare token-based Named Tunnel\033[0m")
    print(f"  {D}╰──────────────────────────────────────────────────────────╯{R}")
    print()

def main():
    banner()
    if not _HAS_CRYPTO:
        print("  \033[33m!\033[0m  pycryptodome missing — encrypted streams will fail")
        print("     pip install pycryptodome\n")

    warm_session()
    print()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"  \033[90m│\033[0m  docs   \033[36mhttp://localhost:{PORT}/docs\033[0m")
    print(f"  \033[90m│\033[0m  root   \033[90m(blank by design)\033[0m")
    print()

    if TUNNEL_MODE == "none":
        print("  \033[90m│\033[0m  tunnel disabled (TUNNEL=none)")
    else:
        print(f"  \033[90m│\033[0m  tunnel starting (token-based)…")
        public_url = start_token_tunnel()
        if public_url:
            PUBLIC_URL["value"] = public_url
            docs_url = public_url.rstrip("/") + "/docs"
            print()
            print(f"  \033[90m│\033[0m  \033[1;36m{PUBLIC_HOSTNAME}\033[0m  →  \033[1;36m{docs_url}\033[0m")

            # Wait for cloudflared to report a registered connection.
            # This is the real readiness signal — it does not depend
            # on the local machine's DNS resolver.
            print(f"  \033[90m│\033[0m  waiting for cloudflared to register…")
            connected = _CLOUDFLARED_CONNECTED.wait(timeout=60)

            if connected:
                # Bonus: try an HTTP round-trip so we can print an
                # extra ✓ when the local resolver cooperates. Never
                # blocks readiness.
                if verify_tunnel(public_url, retries=3, delay=2):
                    print(f"  \033[90m│\033[0m  tunnel verified \033[32m✓\033[0m")
                else:
                    print(f"  \033[90m│\033[0m  \033[90mlocal DNS can't reach the hostname, "
                          f"but cloudflared is connected\033[0m")
                print(f"  \033[90m│\033[0m  \033[32mready\033[0m — open \033[1m{docs_url}\033[0m")
                threading.Thread(target=tunnel_watchdog,
                                 args=(public_url,),
                                 daemon=True).start()
            else:
                print(f"  \033[90m│\033[0m  \033[33m!\033[0m cloudflared hasn't reported a "
                      f"connection yet.")
                print(f"  \033[90m│\033[0m    The process is running — it may still connect.")
                print(f"  \033[90m│\033[0m    Open \033[1m{docs_url}\033[0m in a browser to test.")
                print(f"  \033[90m│\033[0m    Local access: http://localhost:{PORT}/docs")
                threading.Thread(target=tunnel_watchdog,
                                 args=(public_url,),
                                 daemon=True).start()
        else:
            print(f"  \033[90m│\033[0m  \033[33mtunnel failed to start — localhost only\033[0m")
            print(f"  \033[90m│\033[0m    Check that:")
            print(f"  \033[90m│\033[0m      1. cloudflared is installed: cloudflared --version")
            print(f"  \033[90m│\033[0m      2. TUNNEL_TOKEN is set correctly")
            print(f"  \033[90m│\033[0m      3. Dashboard route maps {PUBLIC_HOSTNAME} → http://127.0.0.1:{PORT}")

    print(f"  \033[90m╰─\033[0m  \033[90mCtrl+C to stop\033[0m\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  \033[90m│\033[0m  shutting down…")
        try: server.shutdown()
        except Exception: pass
        if _CLOUDFLARED_PROC:
            try:
                _CLOUDFLARED_PROC.terminate()
                _CLOUDFLARED_PROC.wait(timeout=5)
            except Exception: pass
        print("  \033[90m╰─\033[0m  \033[32mdone\033[0m\n")

if __name__ == "__main__":
    main()
