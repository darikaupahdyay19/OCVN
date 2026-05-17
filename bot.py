import asyncio
import json
import os
import shutil
import sys
import zipfile
import tarfile
import subprocess
import logging
import uuid
import aiohttp
import time
import re
import psutil
from curl_cffi.requests import AsyncSession
from pyrogram import Client, filters, idle
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv()

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Log capture & error reporting setup ---
from collections import deque

LOG_FILE = "bot.log"
_log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# 1) Persistent log file
try:
    _file_handler = logging.FileHandler(LOG_FILE)
    _file_handler.setFormatter(_log_fmt)
    logging.getLogger().addHandler(_file_handler)
except Exception as _e:
    logger.warning(f"Could not attach file log handler: {_e}")

# 2) In-memory ring buffer for /logs command
log_buffer = deque(maxlen=500)

class _MemoryLogHandler(logging.Handler):
    def emit(self, record):
        try:
            log_buffer.append(self.format(record))
        except Exception:
            pass

_mem_handler = _MemoryLogHandler()
_mem_handler.setFormatter(_log_fmt)
logging.getLogger().addHandler(_mem_handler)

# 3) Telegram error notifier (queued, drained by background task)
_pending_errors = deque(maxlen=50)

class _TelegramErrorHandler(logging.Handler):
    """Queues ERROR+ log records to be DM'd to OWNER_ID."""
    def emit(self, record):
        if record.levelno < logging.ERROR:
            return
        try:
            _pending_errors.append(self.format(record))
        except Exception:
            pass

_tg_err_handler = _TelegramErrorHandler()
_tg_err_handler.setFormatter(_log_fmt)
logging.getLogger().addHandler(_tg_err_handler)

# Telegram Vars
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
# Optional: Telegram user ID that receives error DMs and can use /logs
_owner_raw = os.environ.get("OWNER_ID", "").strip()
OWNER_ID = int(_owner_raw) if _owner_raw.isdigit() else None
if OWNER_ID is None:
    logger.warning("OWNER_ID not set: error DMs and /logs are disabled.")

# OceanVeil Vars
OV_EMAIL = os.environ.get("OV_EMAIL")
OV_PASSWORD = os.environ.get("OV_PASSWORD")

# Check Config
if not all([API_ID, API_HASH, BOT_TOKEN, OV_EMAIL, OV_PASSWORD]):
    logger.error("Missing variables in .env!")
    sys.exit(1)

OUTPUT_ROOT = "downloads"
MAX_CONCURRENT_DOWNLOADS = 3
MAX_FILENAME_LENGTH = 50

# Tool Config
TOOL_BINARY_NAME_WIN = "N_m3u8DL-RE.exe"
TOOL_BINARY_NAME_LINUX = "N_m3u8DL-RE"
TOOL_URL_WIN = "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.2.0/N_m3u8DL-RE_v0.2.0_windows-x64.zip"
TOOL_URL_LINUX = "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.2.0/N_m3u8DL-RE_v0.2.0_linux-x64.tar.gz"

IS_WINDOWS = sys.platform == 'win32'
TOOL_BINARY = TOOL_BINARY_NAME_WIN if IS_WINDOWS else TOOL_BINARY_NAME_LINUX
TOOL_URL = TOOL_URL_WIN if IS_WINDOWS else TOOL_URL_LINUX

def get_tool_path():
    """Get the path to the N_m3u8DL-RE binary"""
    if IS_WINDOWS:
        # Check current directory
        if os.path.exists(TOOL_BINARY_NAME_WIN):
            return os.path.abspath(TOOL_BINARY_NAME_WIN)
        # Check PATH
        path = shutil.which("N_m3u8DL-RE")
        if path:
            return path
        return TOOL_BINARY_NAME_WIN
    else:
        # Check /usr/local/bin
        if os.path.exists("/usr/local/bin/N_m3u8DL-RE"):
            return "/usr/local/bin/N_m3u8DL-RE"
        # Check PATH
        path = shutil.which("N_m3u8DL-RE")
        if path:
            return path
        # Check local directory
        if os.path.exists(TOOL_BINARY_NAME_LINUX):
            return os.path.abspath(TOOL_BINARY_NAME_LINUX)
        return TOOL_BINARY_NAME_LINUX

# Apply nest_asyncio
try:
    import nest_asyncio
    nest_asyncio.apply()
except ImportError:
    pass

# Try to import TgCrypto for speed
try:
    import tgcrypto
    logger.info("TgCrypto detected! Encryption will be faster.")
except ImportError:
    logger.warning("TgCrypto not found. Install it for faster speeds: pip install TgCrypto")

# Global Registry
# Tasks: Dict[task_id, TaskObject]
active_tasks = {}
task_queue = {}
anilist_cache = {}
global_status_message = None
tool_lock = asyncio.Lock()
start_time = time.time()

# --- Helper Functions ---

async def search_anilist(title):
    """Search AniList for anime and get thumbnail"""
    clean_title = re.sub(r'\[.*?\]', '', title)
    clean_title = re.sub(r'[～~].*', '', clean_title)
    clean_title = clean_title.strip()
    
    if clean_title in anilist_cache:
        return anilist_cache[clean_title]
    
    query = '''
    query ($search: String) {
        Media (search: $search, type: ANIME) {
            id
            title {
                romaji
                english
                native
            }
            coverImage {
                large
                extraLarge
            }
            bannerImage
        }
    }
    '''
    
    variables = {'search': clean_title}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                'https://graphql.anilist.co',
                json={'query': query, 'variables': variables},
                headers={'Content-Type': 'application/json'}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('data', {}).get('Media'):
                        media = data['data']['Media']
                        result = {
                            'title': media['title'].get('romaji', clean_title),
                            'thumbnail': media['coverImage'].get('extraLarge') or media['coverImage'].get('large'),
                            'banner': media.get('bannerImage')
                        }
                        anilist_cache[clean_title] = result
                        return result
    except Exception as e:
        logger.error(f"AniList search error: {e}")
    
    return None

async def download_thumbnail(url, save_path):
    """Download thumbnail from URL"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    with open(save_path, 'wb') as f:
                        f.write(await resp.read())
                    return save_path
    except Exception as e:
        logger.error(f"Thumbnail download error: {e}")
    return None

def clean_filename(filename):
    """Remove unwanted strings and artifacts"""
    # Remove specific unwanted terms
    unwanted = ["Premium", "DubPremium", "Dub Premium", "dub premium", "dubpremium"]
    for word in unwanted:
        filename = re.sub(re.escape(word), "", filename, flags=re.IGNORECASE)
    
    # Remove tags like [Premium], [Dub], [Sub], [Dual]
    # We do this before stripping to ensure we catch the full tag
    filename = re.sub(r'\[(Premium|Dub|Sub|Dual)\]', '', filename, flags=re.IGNORECASE)
    
    # Remove empty brackets [] or ()
    filename = filename.replace("[]", "").replace("()", "")
    
    # Remove leading/trailing special chars that might be artifacts
    filename = filename.strip(" -_[]")
    
    # Clean up extra spaces
    filename = re.sub(r'\s+', ' ', filename).strip()
    
    # Remove characters that might confuse parsers or look weird
    filename = filename.replace('…', '').replace('～', '-')

    return filename

def create_short_filename(series_name, episode_num, suffix, ext: str = "mp4"):
    """Create filename: 'Title - Subtitle - ## [Type].<ext>'"""
    series_name = clean_filename(series_name)

    # Keep the full name with subtitle, just truncate if too long
    max_series_len = 32

    if len(series_name) > max_series_len:
        series_name = series_name[:max_series_len-1]

    # Remove [Dub] or [Sub] from series name if present to avoid duplication (just in case clean_filename didn't catch it somehow)
    series_name = re.sub(r'\[(Dub|Sub|Dual)\]', '', series_name, flags=re.IGNORECASE).strip()

    # Double check for empty brackets after removal
    series_name = series_name.replace("[]", "").strip()

    ext = (ext or "mp4").lstrip(".").lower()
    filename = f"{series_name} - {episode_num} {suffix}.{ext}"

    if len(filename) > MAX_FILENAME_LENGTH:
        max_series_len = MAX_FILENAME_LENGTH - 20
        series_name = series_name[:max_series_len]
        filename = f"{series_name} - {episode_num} {suffix}.{ext}"

    return filename

def format_bytes(size):
    """Convert bytes to human readable format"""
    if size == 0:
        return "0 B"
    power = 2**10
    n = 0
    labels = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size > power and n < 4:
        size /= power
        n += 1
    return f"{size:.2f} {labels[n]}"

def format_time(seconds):
    """Convert seconds to human readable time"""
    if seconds < 0: seconds = 0
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"

class GlobalProgressTracker:
    def __init__(self):
        self.last_update = 0

    async def update_ui(self):
        global global_status_message
        now = time.time()
        if now - self.last_update < 3: # Update every 3 seconds
            return

        self.last_update = now
        
        if not active_tasks:
            return

        text = ""
        idx = 1
        
        # Sort by start time or just list
        sorted_tasks = list(active_tasks.values())
        
        for task in sorted_tasks:
            # Calculate progress bar
            pct = task.get('percentage', 0)
            filled = int(pct / 10) # 0-10
            # Custom hexagons as requested: ⬢ / ⬡
            bar = "⬢" * filled + "⬡" * (10 - filled)

            processed = task.get('processed_bytes', 0)
            total = task.get('total_bytes', 0)
            speed = task.get('speed', 0)
            elapsed = now - task.get('start_time', now)
            eta = task.get('eta', 0)

            # Status line
            user_name = task.get('user_name', 'Unknown')
            task_id = task.get('task_id', 'Unknown')
            name = task.get('name', 'Unknown Task')
            status = task.get('status', 'Processing')

            text += f"{idx}. {name}\n\n"
            text += f"Task By {user_name} ( `#{task_id}` )\n"
            text += f"┟ [{bar}] {pct:.2f}%\n"
            text += f"┠ Processed → {format_bytes(processed)} of {format_bytes(total)}\n"
            text += f"┠ Status → {status}\n"
            text += f"┠ Speed → {format_bytes(speed)}/s\n"
            text += f"┠ Time → {format_time(elapsed)} ( ETA: {format_time(eta)} )\n"
            text += f"┖ Stop → /cancel {task_id}\n\n"

            idx += 1

        # Bot Stats
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        uptime = format_time(time.time() - start_time)
        disk_free = psutil.disk_usage('.').free
        
        text += f"⌬ Bot Stats\n"
        text += f"┟ CPU → {cpu}% | F → {format_bytes(disk_free)}\n"
        text += f"┖ RAM → {ram}% | UP → {uptime}"

        for t in active_tasks.values():
            msg = t.get('status_msg')
            if msg:
                try:
                    if msg.text != text: # Only edit if changed
                        await msg.edit(text)
                except Exception as e:
                    logger.debug(f"Failed to update message: {e}")

class OCEANVEIL:
    def __init__(self):
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.session = None
        self.auth_header = None
        self.cookies = None
        self.lock = asyncio.Lock()
        # Cookie override: when set (via /setcookies), skip email/password
        # login and use these cookies directly for OceanVeil API + downloader.
        self.cookie_override = None  # dict[str, str] or None
        self.auth_header_override = None  # str or None (e.g. "Bearer ...")
        self._load_cookie_override()

    # --- Cookie override helpers ---

    def _cookie_store_path(self):
        return os.path.join(SESSION_DIR, "ov_cookies.json")

    def _load_cookie_override(self):
        path = self._cookie_store_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cookies = data.get("cookies") or {}
            if isinstance(cookies, dict) and cookies:
                self.cookie_override = cookies
                self.cookies = dict(cookies)
                auth = data.get("auth_header")
                if auth:
                    self.auth_header_override = auth
                    self.auth_header = auth
                logger.info(
                    f"Loaded cookie override from {path} "
                    f"({len(cookies)} cookies, auth={'yes' if auth else 'no'})."
                )
        except Exception as e:
            logger.warning(f"Failed to load cookie override: {e}")

    def _save_cookie_override(self):
        path = self._cookie_store_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "cookies": self.cookie_override or {},
                        "auth_header": self.auth_header_override,
                    },
                    f,
                )
        except Exception as e:
            logger.warning(f"Failed to save cookie override: {e}")

    async def set_cookie_override(self, cookies: dict, auth_header: str = None):
        """Replace the active OceanVeil session with user-supplied cookies."""
        async with self.lock:
            self.cookie_override = dict(cookies)
            self.cookies = dict(cookies)
            self.auth_header_override = auth_header
            # Force using the override for any subsequent API calls.
            self.auth_header = auth_header
            # Drop the existing curl_cffi session so new cookies/headers apply.
            try:
                if self.session is not None:
                    await self.session.close()
            except Exception:
                pass
            self.session = None
            self._save_cookie_override()

    async def clear_cookie_override(self):
        """Remove cookie override and fall back to email/password login."""
        async with self.lock:
            self.cookie_override = None
            self.auth_header_override = None
            self.auth_header = None
            self.cookies = None
            try:
                if self.session is not None:
                    await self.session.close()
            except Exception:
                pass
            self.session = None
            path = self._cookie_store_path()
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

    async def get_session(self):
        if self.session is None:
            self.session = AsyncSession(
                impersonate="chrome", 
                headers={"User-Agent": self.user_agent}
            )
        return self.session

    async def login(self):
        async with self.lock:
            # If a cookie override is active, treat ourselves as already logged in.
            if self.cookie_override:
                self.cookies = dict(self.cookie_override)
                self.auth_header = self.auth_header_override
                return True
            if self.auth_header:
                return True
            session = await self.get_session()
            logger.info(f"Logging in as {OV_EMAIL}...")
            try:
                res = await session.post(
                    "https://oceanveil.net/api/v1/users/login",
                    json={"user": {"email": OV_EMAIL, "password": OV_PASSWORD}}
                )
                if res.status_code not in (200, 201):
                    logger.error(f"Login failed: {res.status_code}")
                    return False

                self.auth_header = res.headers.get('authorization')
                self.cookies = res.cookies.get_dict()
                logger.info("Login successful.")
                return True
            except Exception as e:
                logger.error(f"Login error: {e}")
                return False

    async def get_episodes(self, id):
        if not self.auth_header:
            if not await self.login(): 
                return [], None, None, None, None
        
        session = await self.get_session()
        logger.info(f"Fetching data for Anime ID: {id}...")
        data = []
        url = f"https://oceanveil.net/api/v1/anime_titles/{id}?include%5B%5D=anime_episodes"
        
        try:
            req_headers = {}
            if self.auth_header:
                req_headers["authorization"] = self.auth_header
            res = await session.get(
                url, headers=req_headers, cookies=self.cookies
            )
            if res.status_code != 200:
                logger.error(f"Failed to fetch episodes: {res.status_code}")
                return [], None, None, None, None

            json_resp = res.json()
            attributes = json_resp['data'].get("attributes", {})
            series_name = attributes.get("name", "Unknown")

            # Robust Language Detection
            # Strategy:
            #   1. Trust the API if it tells us the audio language directly
            #      (some titles expose `audioLanguage` / `language`).
            #   2. Otherwise look at the RAW title for an explicit [Dub] tag
            #      or a standalone "dub" word. We must do this BEFORE
            #      clean_filename() runs, because that helper strips
            #      [Dub]/[Sub]/[Premium] tags and would erase our signal.
            series_name_clean = clean_filename(series_name)
            api_audio = (
                attributes.get("audioLanguage")
                or attributes.get("language")
                or ""
            )
            api_audio_lower = str(api_audio).lower()
            raw_lower = (series_name or "").lower()

            if any(x in api_audio_lower for x in ("en", "eng", "english", "dub")):
                is_dub = True
            elif any(x in api_audio_lower for x in ("ja", "jp", "jpn", "japanese", "sub")):
                is_dub = False
            else:
                is_dub = bool(
                    re.search(r"\[\s*dub", raw_lower)   # explicit [Dub] tag
                    or re.search(r"\bdub\b", raw_lower) # standalone "dub" word
                )

            lang_code = "eng" if is_dub else "jpn"
            lang_name = "English" if is_dub else "Japanese"
            
            if 'included' in json_resp:
                for i in json_resp['included']:
                    if i.get('type') == "animeEpisode":
                        ep_name = i['attributes']['name']
                        ep_num = i['attributes']['displayNumber']
                        ep_id = i.get('id')
                        
                        full_name = f"{series_name_clean} - Episode {ep_num} - {ep_name}"
                        full_name = clean_filename(full_name)
                        
                        data.append({
                            'name': full_name,
                            'url': f"https://oceanveil.net/api/v1/anime_episodes/{ep_id}/video.m3u8",
                            'ep_id': ep_id, 
                            'anime_id': id,
                            'ep_num': str(ep_num),
                            'ep_title': ep_name,
                            'series_name': series_name_clean,
                            'lang_code': lang_code,
                            'lang_name': lang_name
                        })
            
            logger.info(f"Found {len(data)} episodes for {series_name} ({lang_name}).")
            return data, self.auth_header, self.cookies, self.user_agent, lang_code
        except Exception as e:
            logger.error(f"Error fetching ID {id}: {e}")
            return [], None, None, None, None

    async def search(self, query, limit: int = 15):
        """Search OceanVeil anime titles by name.

        Returns a list of dicts: [{id, name, lang_code, lang_name, is_nsfw}, ...]

        OceanVeil's real search endpoint is
            /api/v1/anime_titles/search?q=<term>&is_mature=<true|false>
        and the `is_mature` flag SEGREGATES the index — NSFW titles are only
        returned when `is_mature=true`. To surface both SFW and NSFW hits in
        one /search response we hit the endpoint twice (mature=true and
        mature=false) and merge by id. We also fall back to the older JSON:API
        filter shapes in case the backend changes.
        """
        if not self.auth_header:
            if not await self.login():
                return []

        session = await self.get_session()
        req_headers = {}
        if self.auth_header:
            req_headers["authorization"] = self.auth_header

        from urllib.parse import quote_plus
        q = quote_plus(query.strip())
        ql = query.strip().lower()

        # Per-call limit. Ask for `limit` rows from each (mature/non-mature)
        # bucket so the merge can still surface up to `limit` total.
        per_call = max(limit, 15)

        merged: dict = {}

        async def _fetch(url: str, force_nsfw: bool | None = None):
            try:
                res = await session.get(url, headers=req_headers, cookies=self.cookies)
            except Exception as e:
                logger.debug(f"search GET failed for {url}: {e}")
                return
            if res.status_code != 200:
                logger.debug(f"search non-200 ({res.status_code}) for {url}")
                return
            try:
                payload = res.json()
            except Exception:
                return
            items = payload.get("data") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                attrs = item.get("attributes") or {}
                name = attrs.get("name") or ""
                rid = str(item.get("id", ""))
                if not rid:
                    continue
                api_nsfw = bool(
                    attrs.get("nsfw")
                    or attrs.get("isNsfw")
                    or attrs.get("isAdult")
                    or attrs.get("isMature")
                    or attrs.get("is_mature")
                    or attrs.get("ageRating") in ("R18", "NSFW", "Adult")
                )
                # If the API doesn't tag NSFW directly, trust the bucket
                # the result came from (is_mature=true => NSFW).
                is_nsfw = api_nsfw if api_nsfw or force_nsfw is None else bool(force_nsfw)
                if force_nsfw is True:
                    is_nsfw = True
                merged[rid] = {
                    "id": rid,
                    "name": name,
                    "name_clean": clean_filename(name),
                    "raw_name": name,
                    "lang_code": "eng" if (
                        re.search(r"\[\s*dub", name.lower())
                        or re.search(r"\bdub\b", name.lower())
                    ) else "jpn",
                    "is_nsfw": is_nsfw,
                }

        # 1) Primary endpoint: /anime_titles/search?q=...&is_mature=...
        #    Hit both mature buckets so NSFW + SFW are both represented.
        primary_urls = [
            (
                f"https://oceanveil.net/api/v1/anime_titles/search?q={q}"
                f"&include%5B%5D=genre&is_mature=true&page%5Blimit%5D={per_call}",
                True,
            ),
            (
                f"https://oceanveil.net/api/v1/anime_titles/search?q={q}"
                f"&include%5B%5D=genre&is_mature=false&page%5Blimit%5D={per_call}",
                False,
            ),
        ]
        for url, force_nsfw in primary_urls:
            await _fetch(url, force_nsfw=force_nsfw)

        # 2) Fallbacks: if the primary endpoint returned nothing at all
        #    (schema change, etc), try the older JSON:API filter shapes.
        if not merged:
            fallback_urls = [
                f"https://oceanveil.net/api/v1/anime_titles?filter%5Bname%5D={q}&page%5Blimit%5D={per_call}",
                f"https://oceanveil.net/api/v1/anime_titles?filter%5Btext%5D={q}&page%5Blimit%5D={per_call}",
                f"https://oceanveil.net/api/v1/anime_titles?q={q}&page%5Blimit%5D={per_call}",
            ]
            for url in fallback_urls:
                await _fetch(url, force_nsfw=None)
                if merged:
                    break

        if not merged:
            return []

        results = list(merged.values())
        # Rank: exact-substring matches first, then alphabetical.
        results.sort(
            key=lambda r: (0 if ql in r["name"].lower() else 1, r["name"].lower())
        )
        return results[:limit]


# ---- Variant grouping & interactive flow helpers --------------------------

# When a user starts an interactive download we stash the in-progress state
# here keyed by a short token. Callback buttons reference that token.
# Entries are dicts with whatever the next step needs (chat_id, owner_id,
# title, candidate variants, message id, accumulated choices, etc).
_pending_actions: dict = {}
_PENDING_TTL_SEC = 30 * 60  # 30 minutes


def _new_pending(payload: dict) -> str:
    token = uuid.uuid4().hex[:10]
    payload["created_at"] = time.time()
    _pending_actions[token] = payload
    # Cheap GC: drop expired entries.
    cutoff = time.time() - _PENDING_TTL_SEC
    for k in [k for k, v in _pending_actions.items() if v.get("created_at", 0) < cutoff]:
        _pending_actions.pop(k, None)
    return token


def _norm_variant_name(name: str) -> str:
    """Strip rating/audio tags from a title so siblings collapse to one key."""
    s = name or ""
    # Drop any [..] bracket group (handles [Dub], [18+], [Dub/18+], [Premium]...).
    s = re.sub(r"\[.*?\]", "", s)
    # Drop free-floating "Dub" / "18+" / "NSFW" markers.
    s = re.sub(r"\b(?:dub|sub|18\+|nsfw)\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip(" -_:")
    return s.lower()


def _classify_variant(row: dict) -> tuple[str, str]:
    """Return (rating, audio) for a search row.

    rating: "nsfw" | "sfw"
    audio:  "dub"  | "sub"
    """
    rating = "nsfw" if row.get("is_nsfw") else "sfw"
    audio = "dub" if row.get("lang_code") == "eng" else "sub"
    return rating, audio


async def find_title_variants(query: str) -> tuple[str, dict]:
    """Search OceanVeil and group hits for the best-matching base title.

    Returns (display_name, variants) where variants is:
      { "nsfw": {"sub": row_or_None, "dub": row_or_None},
        "sfw":  {"sub": row_or_None, "dub": row_or_None} }
    `display_name` is the cleaned base title we picked. Empty variants
    means no matches at all.
    """
    rows = await ocean.search(query, limit=25)
    empty = {"nsfw": {"sub": None, "dub": None}, "sfw": {"sub": None, "dub": None}}
    if not rows:
        return "", empty

    # Bucket every row by its normalized base name.
    groups: dict = {}
    order: list = []
    for r in rows:
        key = _norm_variant_name(r["name"])
        if not key:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)

    if not groups:
        return "", empty

    ql = query.strip().lower()
    norm_ql = _norm_variant_name(query)

    # Pick the group that best matches the user's query: exact normalized
    # match first, otherwise substring match, otherwise the first group
    # the search returned (already ranked by relevance upstream).
    chosen_key = None
    if norm_ql and norm_ql in groups:
        chosen_key = norm_ql
    if chosen_key is None:
        for k in order:
            if ql and ql in k:
                chosen_key = k
                break
    if chosen_key is None:
        chosen_key = order[0]

    variants = {"nsfw": {"sub": None, "dub": None}, "sfw": {"sub": None, "dub": None}}
    for r in groups[chosen_key]:
        rating, audio = _classify_variant(r)
        # If two rows collide (same rating+audio bucket), keep the lower id.
        cur = variants[rating][audio]
        if cur is None or int(r["id"]) < int(cur["id"]):
            variants[rating][audio] = r

    # Display name = clean version of any row in the group.
    sample = groups[chosen_key][0]
    display = sample.get("name_clean") or clean_filename(sample.get("name", ""))
    return display, variants


def _ratings_available(variants: dict) -> list:
    out = []
    for rating in ("nsfw", "sfw"):
        if any(variants.get(rating, {}).get(a) for a in ("sub", "dub")):
            out.append(rating)
    return out


def _audios_available(variants: dict, rating: str) -> list:
    """Returns ordered list of audio modes available for a rating.

    "dual" is included if BOTH sub and dub variants exist for that rating.
    """
    bucket = variants.get(rating, {})
    out = []
    if bucket.get("sub"):
        out.append("sub")
    if bucket.get("dub"):
        out.append("dub")
    if bucket.get("sub") and bucket.get("dub"):
        out.append("dual")
    return out


def _kb_rating(token: str, variants: dict, default_rating: str = "nsfw") -> InlineKeyboardMarkup:
    available = _ratings_available(variants)
    row = []
    for r in ("nsfw", "sfw"):
        if r not in available:
            continue
        label = "🔞 NSFW" if r == "nsfw" else "🟢 SFW"
        if r == default_rating:
            label = f"• {label} •"
        row.append(InlineKeyboardButton(label, callback_data=f"v:rating:{token}:{r}"))
    cancel = InlineKeyboardButton("✖ Cancel", callback_data=f"v:cancel:{token}")
    return InlineKeyboardMarkup([row, [cancel]])


def _kb_audio(token: str, variants: dict, rating: str) -> InlineKeyboardMarkup:
    available = _audios_available(variants, rating)
    labels = {"sub": "🇯🇵 Sub", "dub": "🇺🇸 Dub", "dual": "🎚 Dual"}
    row = [
        InlineKeyboardButton(labels[a], callback_data=f"v:audio:{token}:{a}")
        for a in available
    ]
    rows = [row] if row else []
    rows.append([
        InlineKeyboardButton("⬅ Back", callback_data=f"v:back:{token}:rating"),
        InlineKeyboardButton("✖ Cancel", callback_data=f"v:cancel:{token}"),
    ])
    return InlineKeyboardMarkup(rows)


def _kb_vsrc(token: str) -> InlineKeyboardMarkup:
    """Pick which source carries the video stream in a Dual mux."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📺 Sub video", callback_data=f"v:vsrc:{token}:sub"),
            InlineKeyboardButton("🎬 Dub video", callback_data=f"v:vsrc:{token}:dub"),
        ],
        [
            InlineKeyboardButton("⬅ Back", callback_data=f"v:back:{token}:audio"),
            InlineKeyboardButton("✖ Cancel", callback_data=f"v:cancel:{token}"),
        ],
    ])


def _kb_container(token: str, can_back: bool = True, back_step: str = "audio") -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("MP4", callback_data=f"v:cont:{token}:mp4"),
        InlineKeyboardButton("MKV", callback_data=f"v:cont:{token}:mkv"),
    ]]
    bottom = []
    if can_back:
        bottom.append(InlineKeyboardButton("⬅ Back", callback_data=f"v:back:{token}:{back_step}"))
    bottom.append(InlineKeyboardButton("✖ Cancel", callback_data=f"v:cancel:{token}"))
    rows.append(bottom)
    return InlineKeyboardMarkup(rows)


async def setup_tool():
    tool_path = get_tool_path()
    if os.path.exists(tool_path):
        return

    # Lock to prevent race conditions during download
    async with tool_lock:
        if os.path.exists(tool_path):
            return

        logger.info(f"Downloading N_m3u8DL-RE tool ({TOOL_BINARY})...")
        async with aiohttp.ClientSession() as session:
            async with session.get(TOOL_URL) as resp:
                if resp.status == 200:
                    archive_name = "tool.zip" if IS_WINDOWS else "tool.tar.gz"
                    with open(archive_name, 'wb') as f:
                        f.write(await resp.read())

                    if IS_WINDOWS:
                        with zipfile.ZipFile(archive_name, 'r') as z:
                            for f in z.namelist():
                                if f.endswith(".exe"):
                                    with open(TOOL_BINARY, "wb") as t:
                                        shutil.copyfileobj(z.open(f), t)
                    else:
                        with tarfile.open(archive_name, "r:gz") as tar:
                            for member in tar.getmembers():
                                if "N_m3u8DL-RE" in member.name and not member.name.endswith("/"):
                                    f = tar.extractfile(member)
                                    with open(TOOL_BINARY, "wb") as t:
                                        shutil.copyfileobj(f, t)
                                    os.chmod(TOOL_BINARY, 0o755)
                                    break

                    os.remove(archive_name)
                    logger.info("Tool setup complete.")
                else:
                    logger.error("Failed to download tool.")

async def download_file(semaphore, ep_data, auth, cookies, ua, save_dir, task_id, cancel_event=None):
    """Downloads to a temp folder"""
    async with semaphore:
        if cancel_event and cancel_event.is_set():
            return None
            
        url = ep_data['url']
        temp_filename = f"temp_{ep_data['anime_id']}_{ep_data['ep_num']}"
        
        referer = f"https://oceanveil.net/anime_titles/{ep_data['anime_id']}?episode={ep_data['ep_id']}"
        cookies_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
        binary = get_tool_path()
        
        os.makedirs(save_dir, exist_ok=True)
        final_path = os.path.join(save_dir, f"{temp_filename}.mp4")
        
        if os.path.exists(final_path):
            return final_path

        logger.info(f"[DOWNLOADING] Ep {ep_data['ep_num']}...")

        # Update Task Status
        if task_id in active_tasks:
            active_tasks[task_id]['status'] = f"Downloading Ep {ep_data['ep_num']}"
            active_tasks[task_id]['name'] = ep_data['name']

        cmd = [
            binary, url,
            "-H", f"Cookie: {cookies_str}",
        ]
        if auth:
            cmd += ["-H", f"Authorization: {auth}"]
        cmd += [
            "-H", f"User-Agent: {ua}",
            "-H", f"Referer: {referer}",
            "--auto-select", "--save-dir", save_dir, "--save-name", temp_filename, "--no-log"
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, 
            stdout=asyncio.subprocess.PIPE, 
            stderr=asyncio.subprocess.PIPE
        )
        
        start_t = time.time()
        while proc.returncode is None:
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                await proc.wait()
                return None

            # Simple progress simulation or check file size
            try:
                if os.path.exists(final_path):
                    size = os.path.getsize(final_path)
                    if task_id in active_tasks:
                        active_tasks[task_id]['processed_bytes'] = size
                        active_tasks[task_id]['total_bytes'] = size * 1.5 # Estimate? Hard without total.
                        # N_m3u8DL-RE doesn't expose total easily until end or parsing log.
                        # We'll just show downloaded size.
                        active_tasks[task_id]['speed'] = size / (time.time() - start_t)
            except:
                pass

            await progress_tracker.update_ui()
            await asyncio.sleep(1)
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
        
        if proc.returncode == 0:
            return final_path
        else:
            # Capture stderr/stdout from the downloader so we know WHY it failed
            # instead of just logging a generic "Download failed" message.
            try:
                stdout_data, stderr_data = await proc.communicate()
            except Exception:
                stdout_data, stderr_data = b"", b""
            stderr_text = (stderr_data or b"").decode("utf-8", errors="replace").strip()
            stdout_text = (stdout_data or b"").decode("utf-8", errors="replace").strip()
            tail = stderr_text or stdout_text or "(no output)"
            # Keep the log line readable; cap to last 800 chars.
            if len(tail) > 800:
                tail = "..." + tail[-800:]
            logger.error(
                f"[ERROR] Download failed for {ep_data['name']} "
                f"(exit={proc.returncode}): {tail}"
            )
            return None

def mux_dual_audio(audio_file, video_file, output_path, audio_lang, video_lang, video_has_primary_audio=False):
    """Muxes Audio from File 1 + Video from File 2"""
    logger.info(f"   -> Muxing: {os.path.basename(output_path)}")

    # Safety net: dual-audio output must NEVER have two tracks with the same
    # language label. If detection upstream returned the same lang for both
    # sources, force the secondary track to the opposite language so the
    # final file still has distinguishable [eng] / [jpn] streams.
    if audio_lang == video_lang:
        logger.warning(
            f"[MUX] Both sources reported lang={audio_lang}; "
            f"forcing secondary track to opposite language."
        )
        if video_lang == "eng":
            audio_lang = "jpn"
        else:
            audio_lang = "eng"

    if video_has_primary_audio:
        lang1_code = "eng" if video_lang == "eng" else "jpn"
        lang1_title = "English" if video_lang == "eng" else "Japanese"
        lang2_code = "jpn" if audio_lang == "jpn" else "eng"
        lang2_title = "Japanese" if audio_lang == "jpn" else "English"
    else:
        lang1_code = "eng" if audio_lang == "eng" else "jpn"
        lang1_title = "English" if audio_lang == "eng" else "Japanese"
        lang2_code = "jpn" if video_lang == "jpn" else "eng"
        lang2_title = "Japanese" if video_lang == "jpn" else "English"

    cmd = [
        "ffmpeg", "-y",
        "-i", video_file,
        "-i", audio_file,
        "-map", "0:v",
    ]

    if video_has_primary_audio:
        cmd.extend([
            "-map", "0:a",
            "-map", "1:a",
            f"-metadata:s:a:0", f"language={lang1_code}",
            f"-metadata:s:a:0", f"title={lang1_title}",
            f"-disposition:a:0", "default",
            f"-metadata:s:a:1", f"language={lang2_code}",
            f"-metadata:s:a:1", f"title={lang2_title}",
        ])
    else:
        cmd.extend([
            "-map", "1:a",
            "-map", "0:a",
            f"-metadata:s:a:0", f"language={lang1_code}",
            f"-metadata:s:a:0", f"title={lang1_title}",
            f"-disposition:a:0", "default",
            f"-metadata:s:a:1", f"language={lang2_code}",
            f"-metadata:s:a:1", f"title={lang2_title}",
        ])

    cmd.extend(["-c", "copy", "-shortest", "-loglevel", "error", output_path])
    
    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"[MUX ERROR] {e}")
        return False
    except FileNotFoundError:
        logger.error("[CRITICAL] FFmpeg not found.")
        return False

def remux_to_mkv(src_path: str, dst_path: str) -> bool:
    """Stream-copy src into a .mkv container (no re-encode).

    Used after a single-source download when the user picked MKV. dst_path
    is expected to already have a .mkv extension. Returns True on success.
    """
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", src_path,
                "-c", "copy", "-loglevel", "error",
                dst_path,
            ],
            check=True,
        )
        return True
    except FileNotFoundError:
        logger.error("[CRITICAL] FFmpeg not found.")
        return False
    except subprocess.CalledProcessError as e:
        logger.error(f"[REMUX ERROR] {e}")
        return False


# --- Bot Logic ---

# Session directory must be writable by the runtime user (see Dockerfile).
SESSION_DIR = os.environ.get("SESSION_DIR", ".")
os.makedirs(SESSION_DIR, exist_ok=True)

app = Client(
    "ocean_bot",
    api_id=int(API_ID),
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=SESSION_DIR,
    ipv6=False
)
Client.UPLOAD_CHUNK_SIZE = 1024 * 1024 * 4

ocean = None
sem = None
progress_tracker = GlobalProgressTracker()

@app.on_message(filters.command(["start", "Start", "START"]))
async def start_cmd(client, message):
    await message.reply_text(
        "🌊 **OceanVeil Bot Ready!**\n\n"
        "🔎 **Find a title:**\n"
        "/search <name> - Search by name (returns ids; SFW + NSFW)\n\n"
        "📥 **Download (interactive prompts):**\n"
        "/dl <id|url|name> [-e ep|range]\n"
        "    • id/URL → asks container (MP4/MKV)\n"
        "    • name → asks rating (NSFW default), audio (Sub/Dub/Dual), and container\n"
        "/sdl <name> [-e ep|range]\n"
        "    • Same as /dl but auto-picks NSFW (falls back to SFW if no NSFW variant)\n"
        "/engdl <id|url|name> [-e ep|range]\n"
        "    • Shortcut: forces Dub; only asks container\n"
        "/dual <id1|url1> <id2|url2> [-e ep|range]\n"
        "/dual <name> [-e ep|range]\n"
        "    • Asks which source carries the video, then container\n"
        "/engvdiddual <id1|url1> <id2|url2> [-e ep|range]\n"
        "    • Shortcut: video=Dub source, asks container only\n\n"
        "⚙️ **Misc:**\n"
        "/queue - Show all tasks\n"
        "/cancel <id> - Cancel a task\n"
        "/logs [N] - Show last N log lines (owner only)\n"
        "/setcookies - Load cookies.txt as OceanVeil session (owner, DM)\n"
        "/clearcookies - Drop cookie override (owner, DM)\n"
        "/cookiesinfo - Show active cookie override (owner, DM)"
    )

@app.on_message(filters.command(["queue", "status"]))
async def queue_cmd(client, message: Message):
    if not active_tasks:
        await message.reply_text("📭 No active tasks.")
        return
    
    # Force update
    await progress_tracker.update_ui()
    # If the user wants a new message for status, we can send one.
    # But update_ui updates the existing 'status_msg' of tasks.
    # We'll send a new dashboard here.
    # For now, just let the auto-update handle it, or reply with "See above".
    await message.reply_text("🔄 Check active task messages for dashboard updates.")

@app.on_message(filters.command(["cancel"]))
async def cancel_cmd(client, message: Message):
    args = message.command
    if len(args) < 2:
        await message.reply_text("❌ Usage: /cancel <task_id>")
        return
    
    target_id = args[1]

    if target_id in active_tasks:
        task = active_tasks[target_id]
        if 'cancel_event' in task:
            task['cancel_event'].set()
            await message.reply_text(f"🛑 Cancelling task `{target_id}`...")
        else:
            await message.reply_text("❌ Cannot cancel this task.")
    else:
        await message.reply_text(f"❌ Task ID `{target_id}` not found.")

@app.on_message(filters.command(["logs", "log"]))
async def logs_cmd(client, message: Message):
    """Owner-only. Usage: /logs [N]  (default 50, max 500)."""
    if OWNER_ID is None or message.from_user.id != OWNER_ID:
        await message.reply_text("Not authorized.")
        return

    args = message.command
    n = 50
    if len(args) >= 2:
        try:
            n = max(1, min(int(args[1]), 500))
        except ValueError:
            await message.reply_text("Usage: /logs [N]  (1-500)")
            return

    lines = list(log_buffer)[-n:]
    if not lines:
        await message.reply_text("No log entries in buffer yet.")
        return

    text = "\n".join(lines)
    # Telegram message limit is 4096 chars; send as file if larger.
    if len(text) <= 3800:
        await message.reply_text(f"```\n{text}\n```")
    else:
        snapshot_path = f"logs_{int(time.time())}.txt"
        try:
            with open(snapshot_path, "w", encoding="utf-8") as f:
                f.write(text)
            await message.reply_document(
                document=snapshot_path,
                caption=f"Last {len(lines)} log lines",
            )
        finally:
            if os.path.exists(snapshot_path):
                try:
                    os.remove(snapshot_path)
                except Exception:
                    pass


def _parse_netscape_cookies(content: str) -> dict:
    """Parse Netscape-format cookies.txt content into a name->value dict.

    Format per line: domain<TAB>flag<TAB>path<TAB>secure<TAB>expires<TAB>name<TAB>value
    Lines starting with '#' are ignored, except for '#HttpOnly_' prefix
    which is stripped (some browsers add it).
    """
    cookies: dict = {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            # Some exporters use whitespace instead of tabs.
            parts = line.split()
            if len(parts) < 7:
                continue
        name = parts[5]
        value = parts[6] if len(parts) == 7 else "\t".join(parts[6:])
        if name:
            cookies[name] = value
    return cookies


@app.on_message(filters.command(["setcookies", "setcookie"]) & filters.private)
async def setcookies_cmd(client, message: Message):
    """Owner-only. Load a Netscape-format cookies.txt as the OceanVeil session.

    Send the cookies.txt file with /setcookies as the caption, OR reply to
    a previously-sent cookies.txt with /setcookies.
    Optional: append a Bearer/JWT auth header after the command, e.g.
        /setcookies Bearer eyJhbGciOi...
    """
    if OWNER_ID is None or message.from_user.id != OWNER_ID:
        await message.reply_text("Not authorized.")
        return

    target = message if message.document else message.reply_to_message
    if not target or not target.document:
        await message.reply_text(
            "Attach a cookies.txt (Netscape format) with /setcookies as the "
            "caption, or reply to one with /setcookies."
        )
        return

    doc = target.document
    if doc.file_size and doc.file_size > 512 * 1024:
        await message.reply_text("File too large (max 512 KB).")
        return

    tmp_path = await client.download_media(
        target, file_name=f"cookies_{int(time.time())}.txt"
    )
    try:
        with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    cookies = _parse_netscape_cookies(content)
    if not cookies:
        await message.reply_text(
            "Couldn't parse any cookies. Use Netscape cookies.txt format "
            "(e.g. from the 'Get cookies.txt LOCALLY' browser extension)."
        )
        return

    # Optional auth header passed inline: /setcookies Bearer xxx...
    auth_header = None
    if len(message.command) >= 2:
        auth_header = " ".join(message.command[1:]).strip() or None

    await ocean.set_cookie_override(cookies, auth_header=auth_header)

    sample = ", ".join(list(cookies.keys())[:5])
    more = "" if len(cookies) <= 5 else f" (+{len(cookies) - 5} more)"
    await message.reply_text(
        f"Loaded {len(cookies)} cookies. OceanVeil is now using the override.\n"
        f"Keys: {sample}{more}\n"
        f"Auth header: {'yes' if auth_header else 'no'}"
    )


@app.on_message(filters.command(["clearcookies", "clearcookie"]) & filters.private)
async def clearcookies_cmd(client, message: Message):
    if OWNER_ID is None or message.from_user.id != OWNER_ID:
        await message.reply_text("Not authorized.")
        return
    await ocean.clear_cookie_override()
    await message.reply_text(
        "Cookie override cleared. Falling back to email/password login."
    )


@app.on_message(filters.command(["cookiesinfo", "cookieinfo"]) & filters.private)
async def cookiesinfo_cmd(client, message: Message):
    if OWNER_ID is None or message.from_user.id != OWNER_ID:
        await message.reply_text("Not authorized.")
        return
    if not ocean.cookie_override:
        await message.reply_text("No cookie override active.")
        return
    keys = list(ocean.cookie_override.keys())
    preview = ", ".join(keys[:10])
    more = "" if len(keys) <= 10 else f" (+{len(keys) - 10} more)"
    await message.reply_text(
        f"Cookie override active.\n"
        f"Count: {len(keys)}\n"
        f"Auth header: {'yes' if ocean.auth_header_override else 'no'}\n"
        f"Keys: {preview}{more}"
    )


_OV_URL_ID_RE = re.compile(
    r"oceanveil\.net/(?:anime_titles|api/v1/anime_titles)/(\d+)",
    re.IGNORECASE,
)


async def _resolve_anime_ref(token: str):
    """Turn a user-supplied token into an OceanVeil anime id.

    Accepts:
      - bare numeric id, e.g. "274"
      - oceanveil.net URL, e.g.
        "https://oceanveil.net/anime_titles/274?..."
      - free-text name; we run /search and use the top hit.

    Returns (id_str, display_name) or (None, None) if nothing matched.
    """
    token = (token or "").strip()
    if not token:
        return None, None

    # 1) Bare numeric id.
    if token.isdigit():
        return token, None

    # 2) URL with /anime_titles/<id>.
    m = _OV_URL_ID_RE.search(token)
    if m:
        return m.group(1), None

    # 3) Otherwise treat it as a free-text name.
    results = await ocean.search(token, limit=5)
    if not results:
        return None, None
    top = results[0]
    return top["id"], top["name"]


# ---- Shared download executors -------------------------------------------
#
# Both the slash-command handlers (when given a numeric id / URL) and the
# inline-button callback router converge on these two helpers. They take
# fully-resolved choices and own the entire download → mux → upload →
# cleanup lifecycle.
#
# A note on ep_filter: tuple ('single', n) or ('range', a, b) or None.


async def _execute_single_dl(
    *,
    chat_message,            # Pyrogram Message we reply to (downloads attach to it)
    status_message,          # Pyrogram Message used as live status board
    user_id: int,
    user_name: str,
    anime_id: str,
    audio_choice: str,       # "sub" | "dub"  (drives the [Sub]/[Dub] suffix)
    ext: str,                # "mp4" | "mkv"
    ep_filter,
):
    """Run a single-source download (no muxing) for `anime_id`."""
    await setup_tool()

    episodes, auth, cookies, ua, lang_code = await ocean.get_episodes(anime_id)
    if not episodes:
        await status_message.edit("❌ Failed to fetch episodes.")
        return

    if ep_filter:
        if ep_filter[0] == "single":
            episodes = [ep for ep in episodes if int(ep["ep_num"]) == ep_filter[1]]
        elif ep_filter[0] == "range":
            start, end = ep_filter[1], ep_filter[2]
            episodes = [ep for ep in episodes if start <= int(ep["ep_num"]) <= end]

    if not episodes:
        await status_message.edit("❌ No episodes found.")
        return

    series_clean = episodes[0]["series_name"]
    # Suffix follows the user's audio choice. We trust the upstream
    # resolution: callers pass audio="dub" only when the variant they
    # picked is genuinely a dub.
    suffix = "[Dub]" if audio_choice == "dub" else "[Sub]"

    unique_id = str(uuid.uuid4())[:8]
    temp_dir = os.path.join("temp_work", unique_id)
    cancel_event = asyncio.Event()

    active_tasks[unique_id] = {
        "task_id": unique_id,
        "user_id": user_id,
        "user_name": user_name,
        "cancel_event": cancel_event,
        "status_msg": status_message,
        "name": f"{series_clean} {suffix}",
        "status": "Starting...",
        "percentage": 0,
        "processed_bytes": 0,
        "total_bytes": 0,
        "speed": 0,
        "start_time": time.time(),
        "eta": 0,
    }

    try:
        await search_anilist(series_clean)

        for idx, ep in enumerate(episodes, 1):
            if cancel_event.is_set():
                break

            active_tasks[unique_id]["name"] = f"{series_clean} - Ep {ep['ep_num']}"
            active_tasks[unique_id]["percentage"] = (idx / len(episodes)) * 100

            temp_path = await download_file(
                sem, ep, auth, cookies, ua, temp_dir, unique_id, cancel_event
            )
            if not temp_path or not os.path.exists(temp_path):
                continue

            # Always land in mp4 first, then remux to mkv if asked. The
            # downloader writes mp4; remux is stream-copy so it's cheap.
            mp4_name = create_short_filename(series_clean, ep["ep_num"], suffix, "mp4")
            mp4_path = os.path.join(temp_dir, mp4_name)
            if os.path.exists(mp4_path):
                os.remove(mp4_path)
            shutil.move(temp_path, mp4_path)

            if ext == "mkv":
                active_tasks[unique_id]["status"] = f"Remuxing Ep {ep['ep_num']}"
                await progress_tracker.update_ui()
                mkv_name = create_short_filename(series_clean, ep["ep_num"], suffix, "mkv")
                mkv_path = os.path.join(temp_dir, mkv_name)
                if remux_to_mkv(mp4_path, mkv_path):
                    if os.path.exists(mp4_path):
                        os.remove(mp4_path)
                    final_path = mkv_path
                else:
                    # Remux failed — fall back to the mp4 we already have.
                    final_path = mp4_path
            else:
                final_path = mp4_path

            active_tasks[unique_id]["status"] = "Uploading"
            await progress_tracker.update_ui()

            try:
                caption = (
                    f"🎬 **{clean_filename(series_clean)}** - Episode {ep['ep_num']}\n"
                    f"📝 {ep['ep_title']}\n"
                    f"🎭 Type: {suffix}"
                )

                thumb_path = None
                if (
                    series_clean in anilist_cache
                    and anilist_cache[series_clean].get("thumbnail")
                ):
                    thumb_path = await download_thumbnail(
                        anilist_cache[series_clean]["thumbnail"],
                        f"thumb_{unique_id}.jpg",
                    )

                async def progress_hook(current, total):
                    if unique_id in active_tasks:
                        active_tasks[unique_id]["status"] = "Uploading"
                        active_tasks[unique_id]["processed_bytes"] = current
                        active_tasks[unique_id]["total_bytes"] = total
                        await progress_tracker.update_ui()

                await chat_message.reply_document(
                    document=final_path,
                    thumb=thumb_path,
                    caption=caption,
                    progress=progress_hook,
                )

                if thumb_path and os.path.exists(thumb_path):
                    os.remove(thumb_path)
                if os.path.exists(final_path):
                    os.remove(final_path)

            except Exception as e:
                logger.error(f"Upload error: {e}")

        if not cancel_event.is_set():
            active_tasks[unique_id]["status"] = "Completed"
            await progress_tracker.update_ui()
            await asyncio.sleep(2)

    finally:
        if unique_id in active_tasks:
            del active_tasks[unique_id]
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        await progress_tracker.update_ui()
        try:
            await status_message.delete()
        except Exception:
            pass


async def _execute_dual_dl(
    *,
    chat_message,
    status_message,
    user_id: int,
    user_name: str,
    sub_id: str,
    dub_id: str,
    video_src: str,          # "sub" | "dub"  (which file carries the video stream)
    ext: str,                # "mp4" | "mkv"
    ep_filter,
):
    """Run a dual-audio download: pull both sources, mux them together."""
    await setup_tool()

    eps_sub, auth_sub, cookies_sub, ua_sub, lang_sub = await ocean.get_episodes(sub_id)
    eps_dub, auth_dub, cookies_dub, ua_dub, lang_dub = await ocean.get_episodes(dub_id)
    if not eps_sub or not eps_dub:
        await status_message.edit("❌ Failed to fetch info.")
        return

    if video_src == "dub":
        video_eps, audio_eps = eps_dub, eps_sub
        video_auth, video_cookies, video_ua = auth_dub, cookies_dub, ua_dub
        audio_auth, audio_cookies, audio_ua = auth_sub, cookies_sub, ua_sub
        video_id, audio_id = dub_id, sub_id
        video_has_primary = True
    else:
        video_eps, audio_eps = eps_sub, eps_dub
        video_auth, video_cookies, video_ua = auth_sub, cookies_sub, ua_sub
        audio_auth, audio_cookies, audio_ua = auth_dub, cookies_dub, ua_dub
        video_id, audio_id = sub_id, dub_id
        video_has_primary = False

    if ep_filter:
        if ep_filter[0] == "single":
            n = ep_filter[1]
            video_eps = [ep for ep in video_eps if int(ep["ep_num"]) == n]
            audio_eps = [ep for ep in audio_eps if int(ep["ep_num"]) == n]
        elif ep_filter[0] == "range":
            a, b = ep_filter[1], ep_filter[2]
            video_eps = [ep for ep in video_eps if a <= int(ep["ep_num"]) <= b]
            audio_eps = [ep for ep in audio_eps if a <= int(ep["ep_num"]) <= b]

    if not video_eps or not audio_eps:
        await status_message.edit("❌ No episodes found matching filter.")
        return

    series_clean = video_eps[0]["series_name"]
    unique_id = str(uuid.uuid4())[:8]
    task_root = os.path.join("temp_work", unique_id)
    audio_temp = os.path.join(task_root, "audio")
    video_temp = os.path.join(task_root, "video")
    cancel_event = asyncio.Event()

    active_tasks[unique_id] = {
        "task_id": unique_id,
        "user_id": user_id,
        "user_name": user_name,
        "cancel_event": cancel_event,
        "status_msg": status_message,
        "name": f"{series_clean} [Dual]",
        "status": "Starting...",
        "percentage": 0,
        "processed_bytes": 0,
        "total_bytes": 0,
        "speed": 0,
        "start_time": time.time(),
        "eta": 0,
    }

    try:
        await search_anilist(series_clean)

        active_tasks[unique_id]["status"] = "Downloading Sources"
        await progress_tracker.update_ui()

        tasks = []
        for ep in audio_eps:
            tasks.append(download_file(
                sem, ep, audio_auth, audio_cookies, audio_ua,
                audio_temp, unique_id, cancel_event,
            ))
        for ep in video_eps:
            tasks.append(download_file(
                sem, ep, video_auth, video_cookies, video_ua,
                video_temp, unique_id, cancel_event,
            ))
        await asyncio.gather(*tasks)

        if cancel_event.is_set():
            return

        active_tasks[unique_id]["status"] = "Muxing & Uploading"

        audio_map = {}
        for ep in audio_eps:
            path = os.path.join(audio_temp, f"temp_{audio_id}_{ep['ep_num']}.mp4")
            if os.path.exists(path):
                audio_map[str(ep["ep_num"])] = path

        for ep in video_eps:
            if cancel_event.is_set():
                break

            ep_num = str(ep["ep_num"])
            video_path = os.path.join(video_temp, f"temp_{video_id}_{ep_num}.mp4")
            if not os.path.exists(video_path) or ep_num not in audio_map:
                continue

            final_filename = create_short_filename(series_clean, ep["ep_num"], "[Dual]", ext)
            output_path = os.path.join(task_root, final_filename)

            active_tasks[unique_id]["status"] = f"Muxing Ep {ep_num}"
            await progress_tracker.update_ui()

            a_lang = audio_eps[0]["lang_code"]
            v_lang = video_eps[0]["lang_code"]

            if not mux_dual_audio(
                audio_map[ep_num], video_path, output_path,
                a_lang, v_lang, video_has_primary,
            ):
                continue

            active_tasks[unique_id]["status"] = f"Uploading Ep {ep_num}"
            await progress_tracker.update_ui()

            try:
                caption = (
                    f"🎬 **{clean_filename(series_clean)}** - Episode {ep_num}\n"
                    f"📝 {ep['ep_title']}\n"
                    f"🎭 Type: [Dual Audio]"
                )

                thumb_path = None
                if (
                    series_clean in anilist_cache
                    and anilist_cache[series_clean].get("thumbnail")
                ):
                    thumb_path = await download_thumbnail(
                        anilist_cache[series_clean]["thumbnail"],
                        f"thumb_{unique_id}.jpg",
                    )

                async def progress_hook(current, total):
                    if unique_id in active_tasks:
                        active_tasks[unique_id]["status"] = "Uploading"
                        active_tasks[unique_id]["processed_bytes"] = current
                        active_tasks[unique_id]["total_bytes"] = total
                        await progress_tracker.update_ui()

                await chat_message.reply_document(
                    document=output_path,
                    thumb=thumb_path,
                    caption=caption,
                    progress=progress_hook,
                )
                if os.path.exists(output_path):
                    os.remove(output_path)
                if thumb_path and os.path.exists(thumb_path):
                    os.remove(thumb_path)
            except Exception as e:
                logger.error(f"Dual upload error: {e}")

    finally:
        if unique_id in active_tasks:
            del active_tasks[unique_id]
        if os.path.exists(task_root):
            shutil.rmtree(task_root, ignore_errors=True)
        await progress_tracker.update_ui()
        try:
            await status_message.delete()
        except Exception:
            pass


@app.on_message(filters.command(["search", "find"]))
async def search_cmd(client, message: Message):
    """/search <query> -- list matching titles with their ids."""
    args = message.command
    if len(args) < 2:
        await message.reply_text("Usage: /search <name>")
        return
    query = " ".join(args[1:]).strip()
    if not query:
        await message.reply_text("Usage: /search <name>")
        return

    status = await message.reply_text(f"🔍 Searching for `{query}`...")
    try:
        results = await ocean.search(query, limit=15)
    except Exception as e:
        logger.error(f"search error: {e}")
        await status.edit("Search failed (see logs).")
        return

    if not results:
        await status.edit(f"No results for `{query}`.")
        return

    lines = [f"🔎 **Results for** `{query}`:\n"]
    for r in results:
        flags = []
        if r["lang_code"] == "eng":
            flags.append("Dub")
        if r["is_nsfw"]:
            flags.append("18+")
        tag = f" [{'/'.join(flags)}]" if flags else ""
        lines.append(f"`{r['id']}` — {r['name_clean']}{tag}")
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n…(truncated)"
    await status.edit(text)


async def _error_dm_drainer():
    """Background task: forward queued ERROR logs to OWNER_ID."""
    if OWNER_ID is None:
        return
    while True:
        try:
            if _pending_errors:
                msg = _pending_errors.popleft()
                # Trim to Telegram-safe size and wrap in code block.
                snippet = msg if len(msg) <= 3500 else msg[:3500] + "\n...[truncated]"
                try:
                    await app.send_message(
                        OWNER_ID,
                        f"⚠️ Bot error:\n```\n{snippet}\n```",
                    )
                except Exception as e:
                    # Avoid an error->DM->error loop: only debug-log this.
                    logging.getLogger(__name__).debug(
                        f"Failed to deliver error DM: {e}"
                    )
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(2)


def _parse_ep_arg(args, key="-e"):
    """Pull '-e <ep|range>' out of a command's tokens.

    Returns (ep_filter, error_msg, tokens_without_eparg).
    ep_filter: None | ('single', n) | ('range', a, b)
    """
    if key not in args:
        return None, None, args[1:]
    try:
        idx = args.index(key)
    except ValueError:
        return None, None, args[1:]
    raw = args[idx + 1] if idx + 1 < len(args) else None
    rest = args[1:idx]
    if not raw:
        return None, "❌ -e needs an episode number or range (e.g. 1-5).", rest
    if "-" in raw:
        try:
            start, end = map(int, raw.split("-", 1))
            return ("range", start, end), None, rest
        except Exception:
            return None, "❌ Invalid range. Use: 1-5", rest
    try:
        return ("single", int(raw)), None, rest
    except Exception:
        return None, "❌ Invalid episode number.", rest


def _looks_like_ref(tok: str) -> bool:
    return bool(tok and (tok.isdigit() or _OV_URL_ID_RE.search(tok)))


def _summary_line(variants: dict, rating: str, audio: str = None) -> str:
    """Pretty-print which sub-variant we're about to use."""
    bucket = variants.get(rating, {})
    if audio == "dual":
        sub_id = (bucket.get("sub") or {}).get("id")
        dub_id = (bucket.get("dub") or {}).get("id")
        return f"Sub id `{sub_id}` + Dub id `{dub_id}` (Dual)"
    if audio:
        row = bucket.get(audio)
        if row:
            return f"`{row['id']}` — {row['name_clean']}"
    # Just rating: list whatever sub-variants exist.
    parts = []
    for a in ("sub", "dub"):
        if bucket.get(a):
            parts.append(f"`{bucket[a]['id']}` ({a})")
    return ", ".join(parts) or "(none)"


@app.on_message(filters.command(["dl", "engdl"]))
async def dl_cmd(client, message: Message):
    """/dl <id|url|name> [-e <ep|range>].

    - id / URL: skip rating + audio prompts (the variant is already picked)
      and ask only for container.
    - name: show rating prompt (NSFW default), then audio, then optionally
      vsrc (for Dual), then container.
    - /engdl: shortcut that forces audio=Dub. Still asks for container.
    """
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    args = message.command

    if len(args) < 2:
        await message.reply_text(
            "Usage:\n"
            "  /dl <id|url|name> [-e <ep|range>]\n"
            "  /engdl <id|url|name> [-e <ep|range>]\n"
            "Examples:\n"
            "  /dl 274\n"
            "  /dl https://oceanveil.net/anime_titles/274\n"
            "  /dl Guilty Hole -e 1-3"
        )
        return

    ep_filter, err, ref_tokens = _parse_ep_arg(args, "-e")
    if err:
        await message.reply_text(err)
        return
    if not ref_tokens:
        await message.reply_text("Need an id, URL, or name. See /dl for usage.")
        return

    is_engdl = args[0].lower() == "engdl"

    # If the first token is an id/URL, skip the search/rating/audio prompts.
    first = ref_tokens[0]
    if _looks_like_ref(first):
        aid, _ = await _resolve_anime_ref(first)
        if not aid:
            await message.reply_text(f"❌ Couldn't resolve `{first}`.")
            return
        # Audio: derive from caller's choice (engdl forces Dub) or the
        # title's metadata. We don't pre-fetch get_episodes here; the
        # executor will handle it. Just guess from the title for the
        # filename suffix and let the user override container.
        token = _new_pending({
            "kind": "single",
            "owner_id": user_id,
            "user_name": user_name,
            "chat_id": message.chat.id,
            "anime_id": aid,
            "audio": "dub" if is_engdl else None,  # None => detect at run
            "ep_filter": ep_filter,
        })
        kb = _kb_container(token, can_back=False)
        await message.reply_text(
            f"📦 Pick container for id `{aid}`:",
            reply_markup=kb,
        )
        return

    # Free-text name: search and group variants.
    query = " ".join(ref_tokens).strip()
    status = await message.reply_text(f"🔍 Searching `{query}`...")

    display, variants = await find_title_variants(query)
    if not display or not _ratings_available(variants):
        await status.edit(
            f"❌ No matches for `{query}`. Try /search to refine."
        )
        return

    # If /engdl was used, lock audio=dub up front. Pick the rating that
    # actually has a Dub variant; prefer NSFW where possible.
    if is_engdl:
        ratings = _ratings_available(variants)
        chosen_rating = None
        for r in ("nsfw", "sfw"):
            if r in ratings and variants[r].get("dub"):
                chosen_rating = r
                break
        if not chosen_rating:
            await status.edit(
                f"❌ No Dub variant found for `{display}`."
            )
            return
        token = _new_pending({
            "kind": "single",
            "owner_id": user_id,
            "user_name": user_name,
            "chat_id": message.chat.id,
            "display": display,
            "variants": variants,
            "rating": chosen_rating,
            "audio": "dub",
            "ep_filter": ep_filter,
        })
        await status.edit(
            f"🎬 **{display}** — {chosen_rating.upper()} / Dub\n"
            f"📦 Pick container:",
            reply_markup=_kb_container(token, can_back=False),
        )
        return

    # Normal /dl flow: rating prompt first, NSFW pre-highlighted.
    token = _new_pending({
        "kind": "single",
        "owner_id": user_id,
        "user_name": user_name,
        "chat_id": message.chat.id,
        "display": display,
        "variants": variants,
        "ep_filter": ep_filter,
    })

    available = _ratings_available(variants)
    # If only one rating exists, skip straight to audio.
    if len(available) == 1:
        rating = available[0]
        _pending_actions[token]["rating"] = rating
        audios = _audios_available(variants, rating)
        if len(audios) == 1:
            # Only one audio mode too -- skip audio prompt.
            _pending_actions[token]["audio"] = audios[0]
            if audios[0] == "dual":
                await status.edit(
                    f"🎬 **{display}** — {rating.upper()} / Dual\n"
                    f"🎞 Which source carries the video?",
                    reply_markup=_kb_vsrc(token),
                )
            else:
                await status.edit(
                    f"🎬 **{display}** — {rating.upper()} / {audios[0].title()}\n"
                    f"📦 Pick container:",
                    reply_markup=_kb_container(token, can_back=False),
                )
            return
        await status.edit(
            f"🎬 **{display}** — {rating.upper()}\n"
            f"🎙 Pick audio:",
            reply_markup=_kb_audio(token, variants, rating),
        )
        return

    await status.edit(
        f"🎬 **{display}**\n"
        f"🔞 Pick rating (NSFW default):",
        reply_markup=_kb_rating(token, variants, default_rating="nsfw"),
    )


@app.on_message(filters.command(["sdl"]))
async def sdl_cmd(client, message: Message):
    """/sdl <name> [-e <ep|range>] -- search-download with NSFW default.

    Always picks NSFW silently; falls back to SFW only if the title has
    no NSFW variant. After that the audio + container prompts are the
    same as /dl. Names only — id / URL inputs go through /dl.
    """
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    args = message.command

    if len(args) < 2:
        await message.reply_text(
            "Usage: /sdl <name> [-e <ep|range>]\n"
            "Auto-picks NSFW (falls back to SFW if no NSFW variant exists)."
        )
        return

    ep_filter, err, ref_tokens = _parse_ep_arg(args, "-e")
    if err:
        await message.reply_text(err)
        return
    if not ref_tokens:
        await message.reply_text("Usage: /sdl <name>")
        return

    query = " ".join(ref_tokens).strip()
    status = await message.reply_text(f"🔍 Searching `{query}`...")

    display, variants = await find_title_variants(query)
    available = _ratings_available(variants)
    if not display or not available:
        await status.edit(
            f"❌ No matches for `{query}`. Try /search to refine."
        )
        return

    # NSFW preferred, SFW only as fallback.
    rating = "nsfw" if "nsfw" in available else "sfw"
    audios = _audios_available(variants, rating)
    if not audios:
        await status.edit(f"❌ No usable variant for `{display}`.")
        return

    token = _new_pending({
        "kind": "single",
        "owner_id": user_id,
        "user_name": user_name,
        "chat_id": message.chat.id,
        "display": display,
        "variants": variants,
        "rating": rating,
        "ep_filter": ep_filter,
    })

    # If only one audio is available, skip the audio prompt.
    if len(audios) == 1:
        _pending_actions[token]["audio"] = audios[0]
        if audios[0] == "dual":
            await status.edit(
                f"🎬 **{display}** — {rating.upper()} / Dual\n"
                f"🎞 Which source carries the video?",
                reply_markup=_kb_vsrc(token),
            )
        else:
            await status.edit(
                f"🎬 **{display}** — {rating.upper()} / {audios[0].title()}\n"
                f"📦 Pick container:",
                reply_markup=_kb_container(token, can_back=False),
            )
        return

    await status.edit(
        f"🎬 **{display}** — {rating.upper()} (auto)\n"
        f"🎙 Pick audio:",
        reply_markup=_kb_audio(token, variants, rating),
    )


@app.on_message(filters.command(["dual", "engvdiddual"]))
async def dual_cmd(client, message: Message):
    """/dual <id1|url1> <id2|url2> [-e <ep|range>]   (two ids: pair them)
       /dual <name>            [-e <ep|range>]       (one name: auto-pair)

    For id/URL pairs we skip the variant prompts and only ask for
    video-source + container. For a name we resolve a NSFW-preferred
    pair via /search + variant grouping, then ask the same two questions.

    /engvdiddual is a shortcut: id pair + force video_src=dub, ask
    container only.
    """
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    args = message.command

    if len(args) < 2:
        await message.reply_text(
            "Usage:\n"
            "  /dual <id1|url1> <id2|url2> [-e <ep|range>]\n"
            "  /dual <name> [-e <ep|range>]\n"
            "  /engvdiddual <id1|url1> <id2|url2> [-e <ep|range>]"
        )
        return

    ep_filter, err, rest = _parse_ep_arg(args, "-e")
    if err:
        await message.reply_text(err)
        return
    if not rest:
        await message.reply_text("Need an id pair or name. See /dual for usage.")
        return

    is_engvdid = args[0].lower() == "engvdiddual"

    # Two refs (id|URL pair).
    if len(rest) >= 2 and _looks_like_ref(rest[0]) and _looks_like_ref(rest[1]):
        id1, _ = await _resolve_anime_ref(rest[0])
        id2, _ = await _resolve_anime_ref(rest[1])
        if not id1 or not id2:
            await message.reply_text("Couldn't resolve one of the references.")
            return

        # We don't know which id is sub vs dub yet -- detect from API.
        status = await message.reply_text("🔍 Inspecting both ids...")
        eps1, _, _, _, lang1 = await ocean.get_episodes(id1)
        eps2, _, _, _, lang2 = await ocean.get_episodes(id2)
        if not eps1 or not eps2:
            await status.edit("❌ Failed to fetch info for one of the ids.")
            return

        # Map to (sub_id, dub_id). If both look the same, treat first as
        # sub and second as dub -- mux_dual_audio's safety net forces
        # distinct language tags.
        if lang1 == "eng" and lang2 != "eng":
            sub_id, dub_id = id2, id1
        elif lang2 == "eng" and lang1 != "eng":
            sub_id, dub_id = id1, id2
        else:
            sub_id, dub_id = id1, id2

        token = _new_pending({
            "kind": "dual",
            "owner_id": user_id,
            "user_name": user_name,
            "chat_id": message.chat.id,
            "sub_id": sub_id,
            "dub_id": dub_id,
            "ep_filter": ep_filter,
        })

        if is_engvdid:
            _pending_actions[token]["video_src"] = "dub"
            await status.edit(
                f"🎬 Dual: sub `{sub_id}` + dub `{dub_id}` (video=Dub)\n"
                f"📦 Pick container:",
                reply_markup=_kb_container(token, can_back=False),
            )
            return

        await status.edit(
            f"🎬 Dual: sub `{sub_id}` + dub `{dub_id}`\n"
            f"🎞 Which source carries the video?",
            reply_markup=_kb_vsrc(token),
        )
        return

    # Single free-text name -- auto-pair NSFW Sub+Dub via search.
    if is_engvdid:
        await message.reply_text(
            "/engvdiddual takes two ids only. Use /dual <name> for auto-pair."
        )
        return

    query = " ".join(rest).strip()
    status = await message.reply_text(f"🔍 Searching `{query}`...")
    display, variants = await find_title_variants(query)
    available = _ratings_available(variants)
    if not display or not available:
        await status.edit(f"❌ No matches for `{query}`.")
        return

    # NSFW preferred; need both sub and dub in that bucket.
    rating = None
    for r in ("nsfw", "sfw"):
        if r in available and variants[r].get("sub") and variants[r].get("dub"):
            rating = r
            break
    if not rating:
        await status.edit(
            f"❌ `{display}` doesn't have both Sub and Dub variants for Dual."
        )
        return

    sub_id = variants[rating]["sub"]["id"]
    dub_id = variants[rating]["dub"]["id"]

    token = _new_pending({
        "kind": "dual",
        "owner_id": user_id,
        "user_name": user_name,
        "chat_id": message.chat.id,
        "display": display,
        "rating": rating,
        "sub_id": sub_id,
        "dub_id": dub_id,
        "ep_filter": ep_filter,
    })

    await status.edit(
        f"🎬 **{display}** — {rating.upper()} / Dual\n"
        f"sub `{sub_id}` + dub `{dub_id}`\n"
        f"🎞 Which source carries the video?",
        reply_markup=_kb_vsrc(token),
    )


# ---- Callback router for the inline keyboards ---------------------------


@app.on_callback_query(filters.regex(r"^v:"))
async def variant_cb(client, cq: CallbackQuery):
    """Routes v:rating / v:audio / v:vsrc / v:cont / v:back / v:cancel."""
    try:
        parts = (cq.data or "").split(":")
        if len(parts) < 3 or parts[0] != "v":
            await cq.answer("Stale button.", show_alert=False)
            return
        action = parts[1]
        token = parts[2]
        value = parts[3] if len(parts) > 3 else None

        pending = _pending_actions.get(token)
        if not pending:
            await cq.answer("This menu expired. Run the command again.", show_alert=True)
            try:
                await cq.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        # Only the user who started the flow may click.
        if cq.from_user.id != pending.get("owner_id"):
            await cq.answer("This menu isn't for you.", show_alert=True)
            return

        kind = pending.get("kind")  # "single" | "dual"
        variants = pending.get("variants")  # only for name-flows

        if action == "cancel":
            _pending_actions.pop(token, None)
            await cq.answer("Cancelled.")
            try:
                await cq.message.edit_text("✖ Cancelled.")
            except Exception:
                pass
            return

        if action == "back":
            target = value  # "rating" | "audio"
            if kind == "single" and variants:
                if target == "rating":
                    pending.pop("rating", None)
                    pending.pop("audio", None)
                    await cq.message.edit_text(
                        f"🎬 **{pending.get('display','?')}**\n"
                        f"🔞 Pick rating (NSFW default):",
                        reply_markup=_kb_rating(token, variants, "nsfw"),
                    )
                elif target == "audio":
                    pending.pop("audio", None)
                    rating = pending.get("rating", "nsfw")
                    await cq.message.edit_text(
                        f"🎬 **{pending.get('display','?')}** — {rating.upper()}\n"
                        f"🎙 Pick audio:",
                        reply_markup=_kb_audio(token, variants, rating),
                    )
            elif kind == "dual":
                if target == "audio":
                    # Dual flow only has vsrc -> container; "back" from
                    # container returns to vsrc.
                    pending.pop("video_src", None)
                    sub_id = pending.get("sub_id")
                    dub_id = pending.get("dub_id")
                    await cq.message.edit_text(
                        f"🎬 Dual: sub `{sub_id}` + dub `{dub_id}`\n"
                        f"🎞 Which source carries the video?",
                        reply_markup=_kb_vsrc(token),
                    )
            await cq.answer()
            return

        if action == "rating":
            if kind != "single" or not variants:
                await cq.answer("Not applicable.", show_alert=False)
                return
            pending["rating"] = value
            audios = _audios_available(variants, value)
            if len(audios) == 1:
                pending["audio"] = audios[0]
                if audios[0] == "dual":
                    await cq.message.edit_text(
                        f"🎬 **{pending.get('display','?')}** — {value.upper()} / Dual\n"
                        f"🎞 Which source carries the video?",
                        reply_markup=_kb_vsrc(token),
                    )
                else:
                    await cq.message.edit_text(
                        f"🎬 **{pending.get('display','?')}** — {value.upper()} / {audios[0].title()}\n"
                        f"📦 Pick container:",
                        reply_markup=_kb_container(token, can_back=True, back_step="rating"),
                    )
            else:
                await cq.message.edit_text(
                    f"🎬 **{pending.get('display','?')}** — {value.upper()}\n"
                    f"🎙 Pick audio:",
                    reply_markup=_kb_audio(token, variants, value),
                )
            await cq.answer()
            return

        if action == "audio":
            if kind != "single" or not variants:
                await cq.answer("Not applicable.", show_alert=False)
                return
            pending["audio"] = value
            rating = pending.get("rating", "nsfw")
            if value == "dual":
                await cq.message.edit_text(
                    f"🎬 **{pending.get('display','?')}** — {rating.upper()} / Dual\n"
                    f"🎞 Which source carries the video?",
                    reply_markup=_kb_vsrc(token),
                )
            else:
                await cq.message.edit_text(
                    f"🎬 **{pending.get('display','?')}** — {rating.upper()} / {value.title()}\n"
                    f"📦 Pick container:",
                    reply_markup=_kb_container(token, can_back=True, back_step="audio"),
                )
            await cq.answer()
            return

        if action == "vsrc":
            pending["video_src"] = value
            display = pending.get("display") or "Dual"
            rating = pending.get("rating")
            head = f"🎬 **{display}**"
            if rating:
                head += f" — {rating.upper()}"
            head += f" / Dual (video={value.title()})"
            await cq.message.edit_text(
                head + "\n📦 Pick container:",
                reply_markup=_kb_container(token, can_back=True, back_step="audio"),
            )
            await cq.answer()
            return

        if action == "cont":
            ext = value if value in ("mp4", "mkv") else "mp4"
            pending["ext"] = ext
            # Hand off to the appropriate executor. We finalize the
            # variant -> ids resolution here.
            try:
                await cq.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

            chat_id = pending.get("chat_id")
            user_id = pending.get("owner_id")
            user_name = pending.get("user_name") or "Unknown"
            ep_filter = pending.get("ep_filter")

            # Build a status message we can hand the executor (it will
            # delete it when done). Reuse the menu message for that --
            # avoids a second "Working..." message stacking up.
            status_msg = cq.message

            # Fetch a Message object we can call reply_document on.
            # cq.message belongs to the bot, so its replies are top-level
            # in the chat -- exactly what the previous handlers did.
            chat_message = cq.message

            _pending_actions.pop(token, None)
            await cq.answer("Starting…")

            if pending.get("kind") == "single":
                if variants:
                    rating = pending.get("rating", "nsfw")
                    audio = pending.get("audio")
                    if audio == "dual":
                        sub_id = variants[rating]["sub"]["id"]
                        dub_id = variants[rating]["dub"]["id"]
                        await _execute_dual_dl(
                            chat_message=chat_message,
                            status_message=status_msg,
                            user_id=user_id,
                            user_name=user_name,
                            sub_id=sub_id,
                            dub_id=dub_id,
                            video_src=pending.get("video_src", "sub"),
                            ext=ext,
                            ep_filter=ep_filter,
                        )
                        return
                    row = variants[rating].get(audio)
                    if not row:
                        await status_msg.edit("❌ Variant unavailable.")
                        return
                    await _execute_single_dl(
                        chat_message=chat_message,
                        status_message=status_msg,
                        user_id=user_id,
                        user_name=user_name,
                        anime_id=row["id"],
                        audio_choice=audio,
                        ext=ext,
                        ep_filter=ep_filter,
                    )
                    return

                # No variants info (id/URL path). Detect audio from API
                # if caller didn't pin one (e.g. /engdl).
                anime_id = pending.get("anime_id")
                audio = pending.get("audio")
                if not audio:
                    _, _, _, _, lang_code = await ocean.get_episodes(anime_id)
                    audio = "dub" if lang_code == "eng" else "sub"
                await _execute_single_dl(
                    chat_message=chat_message,
                    status_message=status_msg,
                    user_id=user_id,
                    user_name=user_name,
                    anime_id=anime_id,
                    audio_choice=audio,
                    ext=ext,
                    ep_filter=ep_filter,
                )
                return

            if pending.get("kind") == "dual":
                await _execute_dual_dl(
                    chat_message=chat_message,
                    status_message=status_msg,
                    user_id=user_id,
                    user_name=user_name,
                    sub_id=pending["sub_id"],
                    dub_id=pending["dub_id"],
                    video_src=pending.get("video_src", "sub"),
                    ext=ext,
                    ep_filter=ep_filter,
                )
                return

        await cq.answer()
    except Exception as e:
        logger.error(f"variant_cb error: {e}")
        try:
            await cq.answer("Something went wrong; check logs.", show_alert=True)
        except Exception:
            pass


if __name__ == "__main__":
    async def main():
        global ocean, sem
        ocean = OCEANVEIL()
        sem = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

        print("Starting Bot...")
        async with app:
            print("Bot running. Press Ctrl+C to stop.")
            drainer = asyncio.create_task(_error_dm_drainer())
            try:
                await idle()
            finally:
                drainer.cancel()

    app.run(main())
