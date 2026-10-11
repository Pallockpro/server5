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
_CLOUDFLARED_CONNECTED = threading.Event()
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
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>ThyAnime</title>
<style>
  :root {
    --accent:#f5b324;
    --menu-bg:rgba(20,20,22,.95);
    --line:rgba(255,255,255,.10);
    --dim:rgba(255,255,255,.62);
    --ease:cubic-bezier(.2,.8,.2,1);
    --sat:env(safe-area-inset-top, 0px);
    --sab:env(safe-area-inset-bottom, 0px);
    --sal:env(safe-area-inset-left, 0px);
    --sar:env(safe-area-inset-right, 0px);
  }
  *, *::before, *::after { box-sizing:border-box; }
  html, body { margin:0; padding:0; height:100%; background:#000; overflow:hidden;
               font-family:'Inter','Segoe UI',system-ui,-apple-system,sans-serif;
               color:#fff; -webkit-font-smoothing:antialiased;
               -webkit-tap-highlight-color:transparent;
               overscroll-behavior:none; }
  @supports (height: 100dvh) {
    html, body { height:100dvh; }
  }
  button { font-family:inherit; }
  #stage { position:fixed; inset:0; background:#000; }
  #stage.cursor-hidden, #stage.cursor-hidden * { cursor:none !important; }
  video { width:100%; height:100%; display:block; background:#000; outline:none;
          object-fit:contain; }
  video::cue { background:rgba(0,0,0,.65); color:#fff; font-family:inherit; }

  /* ── CSS-based fullscreen fallback (iOS Safari & others without the Fullscreen API) ── */
  #stage.fake-fs {
    position:fixed !important;
    inset:0 !important;
    width:100vw !important;
    height:100vh !important;
    height:100dvh !important;
    z-index:999999 !important;
    background:#000 !important;
  }
  #stage.fake-fs video { width:100% !important; height:100% !important; object-fit:contain; }

  /* ── watermark ───────────────────────────────────────────── */
  .brand {
    position:absolute; top:calc(18px + var(--sat)); right:calc(22px + var(--sar));
    z-index:8;
    display:flex; align-items:center; gap:9px;
    pointer-events:none; user-select:none;
    opacity:.55; transition:opacity .6s ease;
    text-shadow:0 1px 8px rgba(0,0,0,.8);
  }
  .brand.idle { opacity:.28; }
  .brand img {
    width:26px; height:26px; border-radius:50%; object-fit:cover; display:block;
    border:1.5px solid rgba(255,255,255,.35); background:#111;
  }
  .brand span { font-size:15px; font-weight:800; letter-spacing:.4px; }
  .brand span b { color:var(--accent); font-weight:800; }

  /* ── top bar (title) ─────────────────────────────────────── */
  .topbar {
    position:absolute; left:0; right:0; top:0; z-index:9;
    padding:calc(20px + var(--sat)) calc(150px + var(--sar)) 70px calc(24px + var(--sal));
    background:linear-gradient(180deg, rgba(0,0,0,.75), rgba(0,0,0,.35) 55%, transparent);
    opacity:0; transition:opacity .3s ease; pointer-events:none;
  }
  .topbar.visible { opacity:1; }
  .title { font-size:18px; font-weight:700; line-height:1.25;
           white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .subtitle { font-size:13px; color:var(--dim); margin-top:3px; font-weight:500; }

  /* ── loading / error overlay ─────────────────────────────── */
  .overlay {
    position:absolute; inset:0; z-index:30;
    display:flex; flex-direction:column; align-items:center; justify-content:center; gap:18px;
    background:#000; transition:opacity .4s ease;
  }
  .overlay.hidden { opacity:0; pointer-events:none; }
  .spinner {
    width:46px; height:46px; border-radius:50%;
    border:3px solid rgba(255,255,255,.14); border-top-color:var(--accent);
    animation:spin .8s linear infinite;
  }
  @keyframes spin { to { transform:rotate(360deg); } }
  .overlay p { margin:0; font-size:14px; color:var(--dim); letter-spacing:.3px; text-align:center; max-width:80%; line-height:1.5; }
  .err-actions { display:none; gap:10px; margin-top:4px; }
  .overlay.error .spinner { display:none; }
  .overlay.error p { color:#ffb4a8; font-size:15px; }
  .overlay.error .err-actions { display:flex; }
  .pill-btn {
    padding:9px 18px; border-radius:999px; font-size:13px; font-weight:600; cursor:pointer;
    background:rgba(255,255,255,.1); color:#fff; border:1px solid var(--line);
    transition:background .15s;
  }
  .pill-btn:hover { background:rgba(255,255,255,.18); }
  .pill-btn.primary { background:var(--accent); color:#111; border-color:transparent; }
  .pill-btn.primary:hover { background:#ffc83d; }

  /* ── skip button ─────────────────────────────────────────── */
  .skip-btn {
    position:absolute; right:calc(24px + var(--sar));
    bottom:calc(112px + var(--sab)); z-index:12;
    display:inline-flex; align-items:center; gap:8px;
    padding:11px 18px; border-radius:10px; cursor:pointer;
    background:rgba(20,20,22,.88); color:#fff; font-size:14px; font-weight:700;
    border:1px solid rgba(255,255,255,.22);
    backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
    opacity:0; transform:translateY(8px); pointer-events:none;
    transition:opacity .25s ease, transform .3s var(--ease), background .15s, color .15s;
  }
  .skip-btn svg { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:2.2;
                  stroke-linecap:round; stroke-linejoin:round; }
  .skip-btn.show { opacity:1; transform:none; pointer-events:auto; }
  .skip-btn:hover { background:#fff; color:#111; }

  /* ── controls ────────────────────────────────────────────── */
  .controls {
    position:absolute; left:0; right:0; bottom:0; z-index:10;
    padding:70px calc(22px + var(--sar)) calc(14px + var(--sab)) calc(22px + var(--sal));
    background:linear-gradient(0deg, rgba(0,0,0,.9), rgba(0,0,0,.55) 50%, transparent);
    opacity:0; transform:translateY(6px); pointer-events:none;
    transition:opacity .3s ease, transform .3s var(--ease);
  }
  .controls.visible { opacity:1; transform:none; pointer-events:auto; }

  .seek-row { display:flex; align-items:center; gap:18px; margin-bottom:4px; }
  .progress-wrap {
    position:relative; flex:1; height:22px; display:flex; align-items:center;
    cursor:pointer; touch-action:none;
  }
  .progress-bg {
    position:relative; width:100%; height:4px; border-radius:999px;
    background:rgba(255,255,255,.22); transition:height .15s ease;
  }
  .progress-wrap:hover .progress-bg, .progress-wrap.dragging .progress-bg { height:6px; }
  .progress-buffer, .progress-fill, .seg {
    position:absolute; top:0; height:100%; border-radius:999px;
  }
  .progress-buffer { left:0; width:0; background:rgba(255,255,255,.28); }
  .seg { background:var(--accent); opacity:.9; border-radius:2px; }
  .progress-fill { left:0; width:0; background:#fff; }
  .progress-thumb {
    position:absolute; top:50%; left:0; width:14px; height:14px; border-radius:50%;
    background:#fff; transform:translate(-50%,-50%) scale(0); pointer-events:none;
    transition:transform .15s var(--ease); box-shadow:0 0 0 4px rgba(255,255,255,.18);
  }
  .progress-wrap:hover .progress-thumb, .progress-wrap.dragging .progress-thumb {
    transform:translate(-50%,-50%) scale(1);
  }
  .time-tip {
    position:absolute; bottom:26px; left:0; padding:4px 9px; border-radius:6px;
    background:rgba(20,20,22,.95); border:1px solid var(--line);
    font-size:12px; font-weight:600; font-variant-numeric:tabular-nums;
    pointer-events:none; opacity:0; white-space:nowrap;
    transform:translateX(-50%); transition:opacity .12s ease;
  }
  .progress-wrap:hover .time-tip { opacity:1; }
  .time {
    font-size:14px; font-weight:600; color:#fff; white-space:nowrap; user-select:none;
    font-variant-numeric:tabular-nums; letter-spacing:.2px;
  }
  .time .dur { color:var(--dim); }

  .btn-row { display:flex; align-items:center; gap:2px; }
  .btn-row .spacer { flex:1; }
  .ctrl {
    position:relative; display:flex; align-items:center; justify-content:center; gap:7px;
    min-width:42px; height:42px; padding:0 9px; border:none; border-radius:10px;
    background:transparent; color:rgba(255,255,255,.92); cursor:pointer;
    transition:background .15s, color .15s, transform .15s;
  }
  .ctrl:hover { background:rgba(255,255,255,.12); color:#fff; }
  .ctrl:active { transform:scale(.92); }
  .ctrl.on { background:rgba(255,255,255,.16); color:#fff; }
  .ctrl svg { width:24px; height:24px; fill:none; stroke:currentColor; stroke-width:2;
              stroke-linecap:round; stroke-linejoin:round; display:block; pointer-events:none; flex-shrink:0; }
  .ctrl.play svg { width:28px; height:28px; }
  .ctrl .lbl { font-size:13px; font-weight:700; letter-spacing:.2px; }
  .ctrl.hidden { display:none; }
  .tag {
    font-size:11px; font-weight:800; letter-spacing:.8px; padding:3px 7px; border-radius:5px;
    background:var(--accent); color:#111;
  }

  .vol-group { display:flex; align-items:center; }
  .vol-slider-wrap {
    width:0; overflow:hidden; display:flex; align-items:center; height:34px;
    transition:width .25s var(--ease);
  }
  .vol-group:hover .vol-slider-wrap, .vol-group:focus-within .vol-slider-wrap { width:96px; }
  .vol-slider-wrap input[type=range] {
    -webkit-appearance:none; appearance:none; width:84px; height:4px; border-radius:999px;
    margin:0 6px 0 4px; outline:none; cursor:pointer;
    background:linear-gradient(90deg, #fff var(--vol,100%), rgba(255,255,255,.25) var(--vol,100%));
  }
  .vol-slider-wrap input[type=range]::-webkit-slider-thumb {
    -webkit-appearance:none; width:13px; height:13px; border-radius:50%; background:#fff; border:none;
  }
  .vol-slider-wrap input[type=range]::-moz-range-thumb {
    width:13px; height:13px; border-radius:50%; background:#fff; border:none;
  }

  /* ── center flash ────────────────────────────────────────── */
  .center-flash {
    position:absolute; top:50%; left:50%; width:78px; height:78px; border-radius:50%;
    background:rgba(0,0,0,.55); display:flex; align-items:center; justify-content:center;
    opacity:0; pointer-events:none; z-index:9; transform:translate(-50%,-50%) scale(.8);
  }
  .center-flash.show { animation:flash-pop .45s ease-out forwards; }
  @keyframes flash-pop {
    0%   { opacity:0; transform:translate(-50%,-50%) scale(.8); }
    35%  { opacity:1; transform:translate(-50%,-50%) scale(1); }
    100% { opacity:0; transform:translate(-50%,-50%) scale(1.25); }
  }
  .center-flash svg { width:36px; height:36px; fill:#fff; stroke:none; }

  /* ── popup menus (audio / episodes / captions / settings) ── */
  .menu {
    position:absolute; bottom:calc(84px + var(--sab)); right:calc(22px + var(--sar)); z-index:15;
    width:270px; max-width:calc(100vw - 16px); max-height:min(62vh, 440px); overflow-y:auto;
    background:var(--menu-bg);
    backdrop-filter:blur(20px) saturate(1.3); -webkit-backdrop-filter:blur(20px) saturate(1.3);
    border:1px solid var(--line); border-radius:14px; padding:6px;
    box-shadow:0 18px 50px rgba(0,0,0,.6);
    opacity:0; transform:translateY(8px) scale(.97); pointer-events:none;
    transform-origin:bottom right;
    transition:opacity .18s ease, transform .22s var(--ease);
    scrollbar-width:thin; scrollbar-color:rgba(255,255,255,.25) transparent;
  }
  .menu.open { opacity:1; transform:none; pointer-events:auto; }
  .menu::-webkit-scrollbar { width:6px; }
  .menu::-webkit-scrollbar-thumb { background:rgba(255,255,255,.22); border-radius:3px; }
  .menu-head {
    font-size:11px; font-weight:800; letter-spacing:1.4px; text-transform:uppercase;
    color:var(--dim); padding:10px 12px 6px;
  }
  .menu-sec + .menu-sec { border-top:1px solid var(--line); margin-top:4px; padding-top:2px; }
  .m-item {
    display:flex; align-items:center; gap:10px; padding:10px 12px; border-radius:9px;
    font-size:14px; font-weight:500; cursor:pointer; user-select:none; color:rgba(255,255,255,.9);
    transition:background .12s;
  }
  .m-item:hover { background:rgba(255,255,255,.1); }
  .m-item .grow { flex:1; min-width:0; }
  .m-item .name { font-weight:600; }
  .m-item .sub { font-size:12px; color:var(--dim); margin-top:1px; }
  .m-item .val { font-size:12px; color:var(--dim); font-variant-numeric:tabular-nums; }
  .m-item .chk { width:18px; height:18px; flex-shrink:0; stroke:var(--accent); fill:none;
                 stroke-width:3; stroke-linecap:round; stroke-linejoin:round; opacity:0; }
  .m-item.active { color:#fff; }
  .m-item.active .chk { opacity:1; }
  .m-item.disabled { opacity:.45; pointer-events:none; }

  .ep-grid { display:grid; grid-template-columns:repeat(5, 1fr); gap:6px; padding:4px 6px 8px; }
  .ep {
    height:38px; border-radius:8px; border:1px solid var(--line); background:rgba(255,255,255,.05);
    color:#fff; font-size:13px; font-weight:600; cursor:pointer; font-variant-numeric:tabular-nums;
    transition:background .12s, border-color .12s;
  }
  .ep:hover { background:rgba(255,255,255,.14); }
  .ep.active { background:var(--accent); border-color:var(--accent); color:#111; }

  /* ── toast ───────────────────────────────────────────────── */
  .toast {
    position:absolute; bottom:calc(120px + var(--sab)); left:50%; padding:9px 18px; border-radius:10px;
    background:rgba(20,20,22,.92); border:1px solid var(--line);
    font-size:13px; font-weight:600; white-space:nowrap;
    opacity:0; pointer-events:none; z-index:25; transform:translateX(-50%);
    transition:opacity .25s ease;
  }
  .toast.show { opacity:1; }

  /* ── mobile ──────────────────────────────────────────────── */
  @media (max-width:700px) {
    .controls { padding:60px calc(10px + var(--sar)) calc(8px + var(--sab)) calc(10px + var(--sal)); }
    .topbar { padding:calc(14px + var(--sat)) calc(110px + var(--sar)) 56px calc(14px + var(--sal)); }
    .title { font-size:15px; }
    .subtitle { font-size:12px; }
    .brand { top:calc(12px + var(--sat)); right:calc(14px + var(--sar)); }
    .brand img { width:22px; height:22px; }
    .brand span { font-size:13px; }
    .seek-row { gap:10px; }
    .time { font-size:12px; }
    .ctrl { min-width:38px; height:40px; padding:0 6px; }
    .ctrl svg { width:22px; height:22px; }
    .ctrl.play svg { width:26px; height:26px; }
    .skip-btn { right:calc(14px + var(--sar)); bottom:calc(100px + var(--sab)); padding:9px 14px; font-size:13px; }
    .vol-slider-wrap { display:none; }
    .toast { bottom:calc(104px + var(--sab)); }
    #btnPip { display:none; }
    .ctrl .lbl { display:none; }
    .menu { bottom:calc(76px + var(--sab)); right:calc(10px + var(--sar)); }
  }
  @media (max-width:420px) {
    #btnBack, #btnFwd { display:none; }
    .time .dur, .time .sep { display:none; }
  }
</style>
</head>
<body>
<div id="stage">
  <video id="video" playsinline webkit-playsinline x5-playsinline crossorigin="anonymous"></video>

  <div class="topbar" id="topbar">
    <div class="title" id="title">Loading…</div>
    <div class="subtitle" id="subtitle"></div>
  </div>

  <div class="brand" id="brand">
    <img src="https://media1.tenor.com/m/fSskkWtbhHIAAAAC/delta-eminence-in-shadow.gif" alt="">
    <span>Thy<b>Anime</b></span>
  </div>

  <button class="skip-btn" id="skipBtn">
    <svg viewBox="0 0 24 24"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19"/></svg>
    <span id="skipLabel">Skip Intro</span>
  </button>

  <div class="center-flash" id="flash"><svg viewBox="0 0 24 24" id="flashSvg"></svg></div>

  <div class="overlay" id="overlay">
    <div class="spinner"></div>
    <p id="overlayMsg">Resolving stream…</p>
    <div class="err-actions">
      <button class="pill-btn primary" id="errRetry">Retry</button>
      <button class="pill-btn" id="errOther">Try dub</button>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <!-- Audio: Sub / Dub -->
  <div class="menu" id="menuAudio">
    <div class="menu-head">Audio</div>
    <div id="audioList"></div>
  </div>

  <!-- Episodes -->
  <div class="menu" id="menuEps" style="width:318px">
    <div class="menu-head" id="epsHead">Episodes</div>
    <div class="ep-grid" id="epGrid"></div>
  </div>

  <!-- Captions -->
  <div class="menu" id="menuSubs">
    <div class="menu-head">Subtitles</div>
    <div id="subList"></div>
  </div>

  <!-- Settings -->
  <div class="menu" id="menuSet">
    <div class="menu-sec">
      <div class="menu-head">Quality</div>
      <div id="qualityList"></div>
    </div>
    <div class="menu-sec">
      <div class="menu-head">Speed</div>
      <div id="speedList"></div>
    </div>
  </div>

  <div class="controls" id="controls">
    <div class="seek-row">
      <div class="progress-wrap" id="progressWrap">
        <div class="progress-bg" id="progressBg">
          <div class="progress-buffer" id="progressBuffer"></div>
          <div id="segLayer"></div>
          <div class="progress-fill" id="progressFill"></div>
          <div class="progress-thumb" id="progressThumb"></div>
        </div>
        <div class="time-tip" id="timeTip">0:00</div>
      </div>
      <div class="time"><span id="curTime">0:00</span><span class="sep"> / </span><span class="dur" id="durTime">0:00</span></div>
    </div>

    <div class="btn-row">
      <button class="ctrl play" id="btnPlay" title="Play / Pause (Space)"><svg viewBox="0 0 24 24" id="iconPlay"></svg></button>
      <button class="ctrl" id="btnBack" title="Back 10s (←)">
        <svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><text x="12" y="15.4" font-size="7.2" font-weight="800" text-anchor="middle" fill="currentColor" stroke="none">10</text></svg>
      </button>
      <button class="ctrl" id="btnFwd" title="Forward 10s (→)">
        <svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><text x="12" y="15.4" font-size="7.2" font-weight="800" text-anchor="middle" fill="currentColor" stroke="none">10</text></svg>
      </button>
      <div class="vol-group" id="volGroup">
        <button class="ctrl" id="btnMute" title="Mute (M)"><svg viewBox="0 0 24 24" id="iconVol"></svg></button>
        <div class="vol-slider-wrap"><input type="range" id="volSlider" min="0" max="1" step="0.02" value="1"></div>
      </div>

      <div class="spacer"></div>

      <button class="ctrl hidden" id="btnNext" title="Next episode (N)">
        <svg viewBox="0 0 24 24"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19"/></svg>
      </button>
      <button class="ctrl hidden" id="btnEps" title="Episodes">
        <svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
        <span class="lbl">Episodes</span>
      </button>
      <button class="ctrl" id="btnAudio" title="Sub / Dub">
        <svg viewBox="0 0 24 24"><path d="M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a9 9 0 0 1 18 0v7a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3"/></svg>
        <span class="tag" id="audioTag">SUB</span>
      </button>
      <button class="ctrl hidden" id="btnCc" title="Subtitles (C)">
        <svg viewBox="0 0 24 24"><rect x="2" y="5" width="20" height="14" rx="2.5"/><path d="M7 11h4M13 11h4M7 15h2.5M12.5 15H17"/></svg>
      </button>
      <button class="ctrl" id="btnSettings" title="Quality &amp; speed">
        <svg viewBox="0 0 24 24"><line x1="21" y1="4" x2="14" y2="4"/><line x1="10" y1="4" x2="3" y2="4"/><line x1="21" y1="12" x2="12" y2="12"/><line x1="8" y1="12" x2="3" y2="12"/><line x1="21" y1="20" x2="16" y2="20"/><line x1="12" y1="20" x2="3" y2="20"/><line x1="14" y1="2" x2="14" y2="6"/><line x1="8" y1="10" x2="8" y2="14"/><line x1="16" y1="18" x2="16" y2="22"/></svg>
      </button>
      <button class="ctrl" id="btnPip" title="Picture-in-Picture (P)">
        <svg viewBox="0 0 24 24"><path d="M21 9V6a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4"/><rect x="12" y="13" width="10" height="7" rx="1.5"/></svg>
      </button>
      <button class="ctrl" id="btnFs" title="Fullscreen (F)"><svg viewBox="0 0 24 24" id="iconFs"></svg></button>
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
const OTHER      = LANG === 'sub' ? 'dub' : 'sub';
const START_AT   = (function(){
  const v = parseFloat(new URLSearchParams(location.search).get('t'));
  return isFinite(v) && v > 0 ? v : 0;
})();

const $ = id => document.getElementById(id);
const video = $('video'), stageEl = $('stage'), overlay = $('overlay'), overlayMsg = $('overlayMsg');
const controls = $('controls'), topbar = $('topbar'), brandEl = $('brand');
const progressW = $('progressWrap'), pFill = $('progressFill'), pBuf = $('progressBuffer');
const pThumb = $('progressThumb'), timeTip = $('timeTip'), segLayer = $('segLayer');
const curTime = $('curTime'), durTime = $('durTime');
const btnPlay = $('btnPlay'), btnMute = $('btnMute'), volSlider = $('volSlider');
const btnFs = $('btnFs'), btnNext = $('btnNext'), btnEps = $('btnEps');
const btnAudio = $('btnAudio'), btnCc = $('btnCc'), btnSettings = $('btnSettings');
const flash = $('flash'), toastEl = $('toast'), skipBtn = $('skipBtn'), skipLabel = $('skipLabel');

const IS_TOUCH = (function(){
  return (window.matchMedia && window.matchMedia('(hover:none) and (pointer:coarse)').matches)
      || ('ontouchstart' in window && navigator.maxTouchPoints > 0);
})();
const IS_IOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
               (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

/* ── icons ── */
const I = {
  play:  '<polygon points="7 4 20 12 7 20 7 4" fill="currentColor" stroke="none"/>',
  pause: '<rect x="6" y="4" width="4.5" height="16" rx="1.2" fill="currentColor" stroke="none"/><rect x="13.5" y="4" width="4.5" height="16" rx="1.2" fill="currentColor" stroke="none"/>',
  volHigh: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>',
  volLow:  '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>',
  volMute: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="22" y1="9" x2="16" y2="15"/><line x1="16" y1="9" x2="22" y2="15"/>',
  fsEnter: '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
  fsExit:  '<path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/><path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/>',
  chk: '<svg class="chk" viewBox="0 0 24 24"><polyline points="4 12 10 18 20 6"/></svg>'
};
function setIcon(el, key){ el.innerHTML = I[key]; }
setIcon($('iconPlay'), 'play'); setIcon($('iconVol'), 'volHigh'); setIcon($('iconFs'), 'fsEnter');

let hls = null, hideTimer = null, dragging = false, subTracks = [], toastTimer = null;
let skipSegments = [], activeSkip = null;
let totalEps = 0, animeTitle = '';

function fmt(s){
  if(!isFinite(s) || s < 0) s = 0; s = Math.floor(s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  return h > 0 ? h+':'+String(m).padStart(2,'0')+':'+String(sec).padStart(2,'0')
               : m+':'+String(sec).padStart(2,'0');
}
function showToast(msg){
  toastEl.textContent = msg; toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>toastEl.classList.remove('show'), 1400);
}
function esc(s){ return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function go(ep, lang, t){
  let u = '/embed/' + ANILIST_ID + '/' + ep + '/' + lang;
  if(t && t > 5) u += '?t=' + Math.floor(t);
  location.href = u;
}

function fail(msg){
  overlay.classList.remove('hidden');
  overlay.classList.add('error');
  overlayMsg.textContent = msg;
}
$('errRetry').onclick = ()=> location.reload();
$('errOther').textContent = 'Try ' + OTHER;
$('errOther').onclick = ()=> go(EPISODE, OTHER, 0);

/* ── title / meta ── */
function paintTitle(){
  $('title').textContent = animeTitle || 'ThyAnime';
  $('subtitle').textContent = 'Episode ' + EPISODE + (totalEps ? ' of ' + totalEps : '') + '  ·  ' + LANG.toUpperCase();
  document.title = (animeTitle ? animeTitle + ' — ' : '') + 'Episode ' + EPISODE;
}
$('audioTag').textContent = LANG.toUpperCase();
paintTitle();

async function loadMeta(){
  try{
    const r = await fetch('/api/anime/' + ANILIST_ID);
    if(!r.ok) return;
    const m = await r.json();
    const t = m.title || {};
    animeTitle = t.english || t.romaji || t.native || '';
    totalEps = m.episodes || (m.nextAiringEpisode && m.nextAiringEpisode.episode ? m.nextAiringEpisode.episode - 1 : 0) || 0;
    paintTitle();
    buildEpisodes();
  }catch(e){ console.warn('[meta]', e.message); }
}

/* ── controls visibility ── */
function anyMenuOpen(){ return !!document.querySelector('.menu.open'); }
function showControls(){
  controls.classList.add('visible'); topbar.classList.add('visible');
  brandEl.classList.remove('idle'); stageEl.classList.remove('cursor-hidden');
  clearTimeout(hideTimer); hideTimer = setTimeout(hideControls, 3000);
}
function hideControls(){
  if(video.paused || anyMenuOpen() || dragging) return;
  controls.classList.remove('visible'); topbar.classList.remove('visible');
  brandEl.classList.add('idle'); stageEl.classList.add('cursor-hidden');
}

/* ── menus ── */
const MENUS = {
  audio:{ btn:btnAudio,   el:$('menuAudio') },
  eps:  { btn:btnEps,     el:$('menuEps') },
  subs: { btn:btnCc,      el:$('menuSubs') },
  set:  { btn:btnSettings, el:$('menuSet') }
};
function closeMenus(){
  Object.values(MENUS).forEach(m => { m.el.classList.remove('open'); m.btn.classList.remove('on'); });
}
function toggleMenu(key){
  const m = MENUS[key], wasOpen = m.el.classList.contains('open');
  closeMenus();
  if(wasOpen) return;
  const r = m.btn.getBoundingClientRect();
  if(window.innerWidth <= 700){ m.el.style.left = '8px'; m.el.style.right = '8px'; m.el.style.width = 'auto'; }
  else {
    m.el.style.left = 'auto';
    m.el.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
    if(key !== 'eps') m.el.style.width = '';
  }
  m.el.classList.add('open'); m.btn.classList.add('on');
  clearTimeout(hideTimer);
}
Object.keys(MENUS).forEach(k => MENUS[k].btn.addEventListener('click', e => { e.stopPropagation(); toggleMenu(k); }));
document.addEventListener('click', e => {
  if(!e.target.closest('.menu')) closeMenus();
});

/* Audio (Sub / Dub) */
function buildAudio(){
  const list = $('audioList'); list.innerHTML = '';
  [['sub','Sub','Original audio'], ['dub','Dub','English audio']].forEach(([key, name, desc])=>{
    const it = document.createElement('div');
    it.className = 'm-item' + (key === LANG ? ' active' : '');
    it.innerHTML = '<div class="grow"><div class="name">' + name + '</div><div class="sub">' + desc + '</div></div>' +
                   '<span class="val">MegaPlay</span>' + I.chk;
    it.onclick = ()=>{
      if(key === LANG){ closeMenus(); return; }
      showToast('Switching to ' + name + '…');
      go(EPISODE, key, video.currentTime || 0);
    };
    list.appendChild(it);
  });
}
buildAudio();

/* Episodes + Next */
function buildEpisodes(){
  if(!totalEps || totalEps < 2) return;
  btnEps.classList.remove('hidden');
  $('epsHead').textContent = 'Episodes · ' + totalEps;
  const grid = $('epGrid'); grid.innerHTML = '';
  for(let n = 1; n <= totalEps; n++){
    const b = document.createElement('button');
    b.className = 'ep' + (n === EPISODE ? ' active' : '');
    b.textContent = n;
    b.onclick = ()=>{ if(n !== EPISODE) go(n, LANG, 0); else closeMenus(); };
    grid.appendChild(b);
  }
  if(EPISODE < totalEps) btnNext.classList.remove('hidden');
  btnEps.addEventListener('click', ()=>{
    const a = grid.querySelector('.active'); if(a) setTimeout(()=>a.scrollIntoView({block:'center'}), 30);
  });
}
btnNext.addEventListener('click', ()=>{ if(EPISODE < totalEps) go(EPISODE + 1, LANG, 0); });

/* ── play / pause ── */
function togglePlay(){ if(video.paused) video.play().catch(()=>{}); else video.pause(); }
function flashIcon(key){
  $('flashSvg').innerHTML = I[key];
  flash.classList.remove('show'); void flash.offsetWidth; flash.classList.add('show');
}
function syncPlayIcon(){ setIcon($('iconPlay'), video.paused ? 'play' : 'pause'); }
btnPlay.addEventListener('click', togglePlay);

/* ── tap / click behaviour ──────────────────────────────────
   Desktop: single-click = play/pause, double-click = fullscreen
   Touch:   single-tap  = show controls (or play/pause),
            double-tap  = fullscreen (matches native mobile UX) */
if(IS_TOUCH){
  let _tapStartX = 0, _tapStartY = 0, _tapTimer = null, _tapCount = 0;
  video.addEventListener('touchstart', e=>{
    const t = e.touches[0];
    _tapStartX = t.clientX; _tapStartY = t.clientY;
  }, {passive:true});
  video.addEventListener('touchend', e=>{
    const t = e.changedTouches[0];
    if(!t) return;
    // Was this a swipe? Ignore.
    if(Math.abs(t.clientX - _tapStartX) > 15 || Math.abs(t.clientY - _tapStartY) > 15) return;
    _tapCount++;
    if(_tapCount === 1){
      _tapTimer = setTimeout(()=>{
        _tapCount = 0;
        if(anyMenuOpen()){ closeMenus(); return; }
        // First tap when controls are hidden → reveal controls (don't toggle play)
        if(!controls.classList.contains('visible') && !video.paused){ showControls(); return; }
        // Otherwise toggle play/pause
        togglePlay(); flashIcon(video.paused ? 'play' : 'pause');
        showControls();
      }, 260);
    } else {
      clearTimeout(_tapTimer);
      _tapCount = 0;
      // Double-tap → fullscreen (custom handler covers iOS Safari)
      toggleFs();
    }
  }, {passive:true});
} else {
  let clickTimer = null;
  video.addEventListener('click', ()=>{
    if(anyMenuOpen()){ closeMenus(); return; }
    clearTimeout(clickTimer);
    clickTimer = setTimeout(()=>{ togglePlay(); flashIcon(video.paused ? 'play' : 'pause'); }, 220);
  });
  video.addEventListener('dblclick', ()=>{ clearTimeout(clickTimer); toggleFs(); });
}

video.addEventListener('play',  ()=>{ syncPlayIcon(); showControls(); });
video.addEventListener('pause', ()=>{ syncPlayIcon(); showControls(); });
video.addEventListener('playing', syncPlayIcon);
video.addEventListener('ended', syncPlayIcon);

/* ── seek bar ── */
function pctFromEvent(e){
  const r = progressW.getBoundingClientRect();
  const x = (e.touches ? e.touches[0].clientX : e.clientX) - r.left;
  return Math.min(1, Math.max(0, x / r.width));
}
function seekTo(p){ if(video.duration) video.currentTime = p * video.duration; }
function paintProgress(p){ pFill.style.width = (p*100)+'%'; pThumb.style.left = (p*100)+'%'; }
progressW.addEventListener('mousedown', e=>{
  dragging = true; progressW.classList.add('dragging');
  const p = pctFromEvent(e); seekTo(p); paintProgress(p);
});
window.addEventListener('mousemove', e=>{
  if(dragging){ const p = pctFromEvent(e); seekTo(p); paintProgress(p); curTime.textContent = fmt(p*(video.duration||0)); }
  else if(progressW.contains(e.target)){
    const p = pctFromEvent(e);
    timeTip.style.left = (p*100)+'%';
    timeTip.textContent = fmt(p*(video.duration||0));
  }
});
window.addEventListener('mouseup', ()=>{ if(dragging){ dragging = false; progressW.classList.remove('dragging'); } });
progressW.addEventListener('touchstart', e=>{ dragging = true; const p = pctFromEvent(e); seekTo(p); paintProgress(p); }, {passive:true});
progressW.addEventListener('touchmove',  e=>{ if(dragging){ const p = pctFromEvent(e); seekTo(p); paintProgress(p); } }, {passive:true});
progressW.addEventListener('touchend',   ()=>{ dragging = false; });

function updateProgress(){
  if(dragging) return;
  const d = video.duration || 0, c = video.currentTime || 0;
  paintProgress(d ? c/d : 0);
  curTime.textContent = fmt(c);
}
video.addEventListener('timeupdate', updateProgress);
video.addEventListener('durationchange', ()=>{ durTime.textContent = fmt(video.duration||0); renderSegs(); });
video.addEventListener('progress', ()=>{
  if(!video.buffered.length || !video.duration) return;
  pBuf.style.width = (video.buffered.end(video.buffered.length-1) / video.duration * 100) + '%';
});

/* ── skip intro / outro (+ amber markers on the bar) ── */
function skipLabelFor(type){
  if(type === 'op' || type === 'mixed-op') return 'Skip Intro';
  if(type === 'ed' || type === 'mixed-ed') return 'Skip Outro';
  if(type === 'recap') return 'Skip Recap';
  return 'Skip';
}
function renderSegs(){
  segLayer.innerHTML = '';
  const d = video.duration;
  if(!d || !isFinite(d)) return;
  skipSegments.forEach(s=>{
    const el = document.createElement('div');
    el.className = 'seg';
    el.style.left  = (s.start / d * 100) + '%';
    el.style.width = (Math.max(0, s.end - s.start) / d * 100) + '%';
    segLayer.appendChild(el);
  });
}
function checkSkip(){
  const t = video.currentTime;
  const seg = skipSegments.find(s => t >= s.start && t < s.end);
  if(seg){
    activeSkip = seg; skipLabel.textContent = skipLabelFor(seg.type); skipBtn.classList.add('show');
  } else { activeSkip = null; skipBtn.classList.remove('show'); }
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
    if(video.duration && isFinite(video.duration) && video.duration > 0) return resolve(video.duration);
    const onDur = () => {
      if(video.duration && isFinite(video.duration) && video.duration > 0){
        video.removeEventListener('durationchange', onDur); resolve(video.duration);
      }
    };
    video.addEventListener('durationchange', onDur);
    setTimeout(()=>{
      video.removeEventListener('durationchange', onDur);
      resolve(video.duration && isFinite(video.duration) ? video.duration : 0);
    }, 8000);
  });
}
async function loadSkipTimes(){
  try{
    const dur = await whenDurationKnown();
    if(!dur || dur <= 0 || !isFinite(dur)) return;
    const r = await fetch('/api/skip/' + ANILIST_ID + '/' + EPISODE + '?length=' + dur.toFixed(3));
    if(!r.ok) return;
    const data = await r.json();
    if(data && data.found && Array.isArray(data.results)){
      skipSegments = data.results
        .filter(x => x && x.interval && typeof x.interval.startTime === 'number')
        .map(x => ({ start:x.interval.startTime, end:x.interval.endTime, type:x.skipType || 'op' }));
      renderSegs();
    }
  }catch(e){ console.warn('[skip] unavailable:', e.message); }
}

/* ── volume ── */
let lastVol = 1;
function loadVol(){ try{ const v = parseFloat(localStorage.getItem('thy_vol')); if(isFinite(v) && v >= 0 && v <= 1) return v; }catch(e){} return 1; }
function applyVol(v){
  video.volume = v; video.muted = v === 0;
  volSlider.value = v; volSlider.style.setProperty('--vol', (v*100)+'%');
  updateVolIcon();
  try{ localStorage.setItem('thy_vol', String(v)); }catch(e){}
}
function updateVolIcon(){
  const v = video.muted ? 0 : video.volume;
  setIcon($('iconVol'), v === 0 ? 'volMute' : (v < 0.5 ? 'volLow' : 'volHigh'));
}
volSlider.addEventListener('input', ()=>{ lastVol = parseFloat(volSlider.value); applyVol(lastVol); });
btnMute.addEventListener('click', ()=>{
  if(video.muted || video.volume === 0) applyVol(lastVol || 0.6);
  else { lastVol = video.volume; applyVol(0); }
});
video.addEventListener('volumechange', updateVolIcon);
lastVol = loadVol() || 1;
applyVol(loadVol());

/* ── fullscreen (desktop + Android + iOS Safari) ──────────────
   Desktop & Android Chrome: use the standard Fullscreen API on #stage.
   iOS Safari: requestFullscreen is undefined on elements, so we fall
   back to a CSS "fake fullscreen" that keeps our custom controls
   (including the Skip Intro button) working.
   If the browser exposes the API but rejects the request (user
   gesture lost, Permissions Policy, etc.) we also drop to the CSS
   fallback instead of throwing. */
let _fakeFs = false;
function _nativeFsActive(){
  return !!(document.fullscreenElement || document.webkitFullscreenElement ||
            document.mozFullScreenElement || document.msFullscreenElement);
}
function _anyFsActive(){ return _fakeFs || _nativeFsActive(); }
function _applyFsIcon(){ setIcon($('iconFs'), _anyFsActive() ? 'fsExit' : 'fsEnter'); }
function _enterFakeFs(){
  if(_fakeFs) return;
  _fakeFs = true;
  stageEl.classList.add('fake-fs');
  document.documentElement.style.overflow = 'hidden';
  // Nudge mobile browsers to hide their address bar where possible.
  try { window.scrollTo(0, 1); } catch(e){}
  _applyFsIcon();
  showToast('Fullscreen');
  // Reflow video size (in case the element doesn't automatically fill the new stage).
  try { window.dispatchEvent(new Event('resize')); } catch(e){}
}
function _exitFakeFs(){
  if(!_fakeFs) return;
  _fakeFs = false;
  stageEl.classList.remove('fake-fs');
  document.documentElement.style.overflow = '';
  _applyFsIcon();
  try { window.dispatchEvent(new Event('resize')); } catch(e){}
}
function toggleFs(){
  // Exit from any active fullscreen mode
  if(_nativeFsActive()){
    const exit = document.exitFullscreen || document.webkitExitFullscreen ||
                 document.mozCancelFullScreen || document.msExitFullscreen;
    if(typeof exit === 'function'){ try { exit.call(document); } catch(e){} }
    if(_fakeFs) _exitFakeFs();
    return;
  }
  if(_fakeFs){ _exitFakeFs(); return; }

  // Try the native Fullscreen API on the stage element first
  const el = stageEl;
  const req = el.requestFullscreen || el.webkitRequestFullscreen ||
              el.mozRequestFullScreen || el.msRequestFullscreen;
  if(typeof req === 'function'){
    try {
      const ret = req.call(el);
      if(ret && typeof ret.then === 'function'){
        ret.then(_applyFsIcon).catch(()=> _enterFakeFs());
      } else {
        setTimeout(_applyFsIcon, 60);
      }
      return;
    } catch(e){
      _enterFakeFs();
      return;
    }
  }

  // No native API (iOS Safari): fall back to CSS fake fullscreen
  _enterFakeFs();
}
btnFs.addEventListener('click', toggleFs);

// Keep the icon in sync with native fullscreen events + clear fake fs
['fullscreenchange','webkitfullscreenchange','mozfullscreenchange','MSFullscreenChange'].forEach(ev=>{
  document.addEventListener(ev, ()=>{
    if(!_nativeFsActive() && _fakeFs){
      // Native fs just ended — nothing to clear, fake fs was already off if native was on
    }
    _applyFsIcon();
  });
});

$('btnPip').addEventListener('click', async ()=>{
  try{
    if(document.pictureInPictureElement) await document.exitPictureInPicture();
    else await video.requestPictureInPicture();
  }catch(e){ showToast('PiP unavailable'); }
});

/* ── quality / speed ── */
function row(label, val, active, onclick){
  const it = document.createElement('div');
  it.className = 'm-item' + (active ? ' active' : '');
  it.innerHTML = '<div class="grow"><div class="name">' + esc(label) + '</div></div>' +
                 (val ? '<span class="val">' + esc(val) + '</span>' : '') + I.chk;
  it.onclick = onclick;
  return it;
}
function buildQualityList(){
  const list = $('qualityList'); list.innerHTML = '';
  if(!hls || !hls.levels || hls.levels.length < 2){
    list.innerHTML = '<div class="m-item disabled"><div class="grow"><div class="name">Single quality</div></div></div>';
    return;
  }
  list.appendChild(row('Auto', 'ABR', hls.currentLevel === -1, ()=>{ hls.currentLevel = -1; buildQualityList(); showToast('Quality: Auto'); }));
  hls.levels.map((l,i)=>({l,i})).sort((a,b)=>(b.l.height||0)-(a.l.height||0)).forEach(({l,i})=>{
    const name = l.height ? l.height+'p' : 'Level '+i;
    list.appendChild(row(name, Math.round((l.bitrate||0)/1000)+'k', hls.currentLevel === i,
      ()=>{ hls.currentLevel = i; buildQualityList(); showToast('Quality: ' + name); }));
  });
}
const SPEEDS = [0.25,0.5,0.75,1,1.25,1.5,1.75,2];
function buildSpeedList(){
  const list = $('speedList'); list.innerHTML = '';
  SPEEDS.forEach(s=>{
    list.appendChild(row(s + '×', s === 1 ? 'Normal' : '', Math.abs(video.playbackRate - s) < 0.01,
      ()=>{ video.playbackRate = s; buildSpeedList(); showToast('Speed: ' + s + '×'); }));
  });
}

/* ── subtitles ── */
function _onlyOneShowing(active){
  subTracks.forEach(t => { if(t !== active && t.mode === 'showing'){ try{ t.mode = 'disabled'; }catch(e){} } });
}
function buildSubList(){
  const list = $('subList');
  if(!subTracks.length){ btnCc.classList.add('hidden'); return; }
  btnCc.classList.remove('hidden');
  list.innerHTML = '';
  list.appendChild(row('Off', '', subTracks.every(t=>t.mode !== 'showing'), ()=>{
    subTracks.forEach(t=>{ try{ t.mode = 'disabled'; }catch(e){} });
    buildSubList(); showToast('Subtitles: Off');
  }));
  subTracks.forEach((t,i)=>{
    list.appendChild(row(t.label || ('Track '+(i+1)), t.language || '', t.mode === 'showing', ()=>{
      subTracks.forEach(x=>{ try{ x.mode = 'disabled'; }catch(e){} });
      try{ t.mode = 'showing'; }catch(e){}
      _onlyOneShowing(t); buildSubList(); showToast('Subtitles: ' + (t.label || 'Track'));
    }));
  });
}
function addSubtitles(subs){
  video.querySelectorAll('track').forEach(t=>t.remove());
  subTracks = [];
  if(!subs || !subs.length){ buildSubList(); return; }
  subs.forEach((s,i)=>{
    const t = document.createElement('track');
    t.kind = 'subtitles';
    t.label = s.lang || ('Track '+(i+1));
    t.srclang = ((s.lang||'und').slice(0,2)||'und').toLowerCase();
    t.src = s.url; t.default = false;
    video.appendChild(t);
  });
  const want = subs.length; let tries = 0;
  (function waitTracks(){
    tries++;
    const list = Array.from(video.textTracks);
    if(list.length >= want || tries > 40) finalize(list); else setTimeout(waitTracks, 50);
  })();
  function finalize(list){
    subTracks = list;
    subTracks.forEach(t=>{ try{ t.mode = 'disabled'; }catch(e){} });
    subTracks.forEach(t=> t.addEventListener('change', ()=>{ if(t.mode === 'showing') _onlyOneShowing(t); }));
    const isEng = t => {
      const lab = (t.label||'').toLowerCase(), lng = (t.language||'').toLowerCase();
      return lng.startsWith('en') || lab.includes('english') || lab.includes('eng');
    };
    const preferred = subTracks.find(isEng) || subTracks[0];
    if(preferred){ try{ preferred.mode = 'showing'; _onlyOneShowing(preferred); }catch(e){} }
    buildSubList();
  }
}

/* ── keyboard ── */
document.addEventListener('keydown', e=>{
  if(['INPUT','TEXTAREA'].includes(document.activeElement.tagName) && document.activeElement.type !== 'range') return;
  switch(e.key){
    case ' ': case 'k': case 'K':
      e.preventDefault(); togglePlay(); flashIcon(video.paused ? 'pause' : 'play'); break;
    case 'ArrowLeft':  e.preventDefault(); video.currentTime = Math.max(0, video.currentTime-10); showToast('−10s'); break;
    case 'ArrowRight': e.preventDefault(); video.currentTime = Math.min(video.duration||0, video.currentTime+10); showToast('+10s'); break;
    case 'ArrowUp':    e.preventDefault(); applyVol(Math.min(1, video.volume+0.05)); break;
    case 'ArrowDown':  e.preventDefault(); applyVol(Math.max(0, video.volume-0.05)); break;
    case 'm': case 'M': btnMute.click(); break;
    case 'f': case 'F': toggleFs(); break;
    case 'p': case 'P': $('btnPip').click(); break;
    case 'n': case 'N': if(!btnNext.classList.contains('hidden')) btnNext.click(); break;
    case 'c': case 'C': if(!btnCc.classList.contains('hidden')) btnCc.click(); break;
    case 'Escape':
      if(_fakeFs) _exitFakeFs();
      closeMenus();
      break;
  }
  showControls();
});
$('btnBack').addEventListener('click', ()=>{ video.currentTime = Math.max(0, video.currentTime-10); showToast('−10s'); });
$('btnFwd').addEventListener('click',  ()=>{ video.currentTime = Math.min(video.duration||0, video.currentTime+10); showToast('+10s'); });

/* ── activity ── */
let lastMove = 0;
function activity(){ const now = Date.now(); if(now - lastMove < 120) return; lastMove = now; showControls(); }
stageEl.addEventListener('mousemove', activity);
stageEl.addEventListener('touchstart', activity, {passive:true});
stageEl.addEventListener('mouseleave', ()=>{
  if(!video.paused && !anyMenuOpen()){
    controls.classList.remove('visible'); topbar.classList.remove('visible');
    brandEl.classList.add('idle'); stageEl.classList.add('cursor-hidden');
  }
});

/* ── playback ── */
function onReady(subs){
  overlay.classList.add('hidden');
  addSubtitles(subs); buildQualityList(); buildSpeedList(); loadSkipTimes();
  video.play().catch(()=>{});
  syncPlayIcon(); showControls();
}
function attach(src, subs){
  if(window.Hls && Hls.isSupported()){
    hls = new Hls({ enableWorker:true, lowLatencyMode:false, startLevel:-1, startPosition: START_AT || -1 });
    hls.loadSource(src); hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, ()=> onReady(subs));
    hls.on(Hls.Events.LEVEL_SWITCHED, ()=> buildQualityList());
    hls.on(Hls.Events.ERROR, (_,data)=>{
      if(data && data.fatal) fail('Playback error: ' + data.type + ' / ' + data.details);
    });
  } else if(video.canPlayType('application/vnd.apple.mpegurl')){
    video.src = src;
    video.addEventListener('loadedmetadata', ()=>{
      if(START_AT) video.currentTime = START_AT;
      onReady(subs);
    }, {once:true});
    video.addEventListener('error', ()=> fail('Native playback error'));
  } else {
    fail('HLS is not supported in this browser.');
  }
}

(async function init(){
  loadMeta();
  try{
    overlayMsg.textContent = 'Resolving stream…';
    const r = await fetch('/api/resolve/' + ANILIST_ID + '/' + EPISODE + '/' + LANG);
    if(!r.ok){
      const err = await r.json().catch(()=>({}));
      return fail((err.error || ('Resolve failed (' + r.status + ')')) + (LANG === 'dub' ? ' — a dub may not be available for this episode.' : ''));
    }
    const data = await r.json();
    overlayMsg.textContent = 'Loading…';
    attach(data.m3u8, data.subtitles || []);
  }catch(e){ fail('Network error: ' + e.message); }
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

  <h2>Mobile Gestures</h2>
  <p>
    <b>Single tap</b> — show controls (or play/pause when controls are already visible)<br>
    <b>Double tap</b> — toggle fullscreen<br>
    <b>Fullscreen</b> uses the native Fullscreen API on desktop and Android.
    On iOS Safari (where the API isn't available on elements) the player
    switches to a CSS-based fullscreen that keeps the custom controls and
    Skip Intro button working.
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
    global _CLOUDFLARED_PROC

    if not shutil.which("cloudflared"):
        print("  \033[90m│\033[0m  cloudflared  \033[31mnot installed\033[0m")
        print("  \033[90m│\033[0m    install: winget install Cloudflare.cloudflared")
        print("  \033[90m│\033[0m    or:      brew install cloudflared")
        return None

    if not TUNNEL_TOKEN or not TUNNEL_TOKEN.strip():
        print("  \033[90m│\033[0m  cloudflared  \033[31mno TUNNEL_TOKEN set\033[0m")
        return None

    _CLOUDFLARED_CONNECTED.clear()
    _CLOUDFLARED_CONN_COUNT["value"] = 0

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

def verify_tunnel(url: str, retries: int = 5, delay: int = 3) -> bool:
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

            print(f"  \033[90m│\033[0m  waiting for cloudflared to register…")
            connected = _CLOUDFLARED_CONNECTED.wait(timeout=60)

            if connected:
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
