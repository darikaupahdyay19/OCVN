import asyncio
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
import math
import re
import anitopy
from curl_cffi.requests import AsyncSession
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv()

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Telegram Vars
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

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

# Global queue management
active_tasks = {}
task_queue = {}
anilist_cache = {}

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
    
    # Remove tags like [Premium]
    filename = re.sub(r'\[Premium\]', '', filename, flags=re.IGNORECASE)
    
    # Remove empty brackets [] or ()
    filename = filename.replace("[]", "").replace("()", "")
    
    # Remove leading/trailing special chars that might be artifacts
    filename = filename.strip(" -_[]")
    
    # Clean up extra spaces
    filename = re.sub(r'\s+', ' ', filename).strip()
    
    return filename

def create_short_filename(series_name, episode_num, suffix):
    """Create filename: 'Title ～ Subtitle… - ## [Type].mp4'"""
    series_name = clean_filename(series_name)
    
    # Keep the full name with subtitle, just truncate if too long
    max_series_len = 32
    
    if len(series_name) > max_series_len:
        series_name = series_name[:max_series_len-1] + "…"
    
    # Remove [Dub] or [Sub] from series name if present to avoid duplication
    series_name = re.sub(r'\[Dub\]|\[Sub\]|\[Dual\]', '', series_name, flags=re.IGNORECASE).strip()
    
    # Double check for empty brackets after removal
    series_name = series_name.replace("[]", "").strip()
    
    filename = f"{series_name} - {episode_num} {suffix}.mp4"
    
    if len(filename) > MAX_FILENAME_LENGTH:
        max_series_len = MAX_FILENAME_LENGTH - 20
        series_name = series_name[:max_series_len] + "…"
        filename = f"{series_name} - {episode_num} {suffix}.mp4"
    
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
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"

class ProgressTracker:
    def __init__(self, message, total_items, action="Processing"):
        self.message = message
        self.total_items = total_items
        self.action = action
        self.current_item = 0
        self.start_time = time.time()
        self.last_update = 0
        self.item_start_time = time.time()
        
    async def update_item(self, item_num, item_name):
        self.current_item = item_num
        self.item_start_time = time.time()
        
    async def update_progress(self, current, total):
        now = time.time()
        
        if now - self.last_update < 2:  # Update every 2 seconds
            return
            
        self.last_update = now
        elapsed = now - self.start_time
        item_elapsed = now - self.item_start_time
        
        if current > 0 and item_elapsed > 0:
            speed = current / item_elapsed
            eta = (total - current) / speed if speed > 0 else 0
        else:
            speed = 0
            eta = 0
            
        percentage = (current / total * 100) if total > 0 else 0
        filled = int(percentage / 10)
        bar = "█" * filled + "░" * (10 - filled)
        
        overall_pct = (self.current_item / self.total_items * 100) if self.total_items > 0 else 0
        
        progress_text = (
            f"🎬 **{self.action}**\n"
            f"📊 Overall: {self.current_item}/{self.total_items} ({overall_pct:.0f}%)\n\n"
            f"📁 Current Item Progress:\n"
            f"[{bar}] {percentage:.1f}%\n\n"
            f"📦 Size: {format_bytes(current)} / {format_bytes(total)}\n"
            f"⚡ Speed: {format_bytes(speed)}/s\n"
            f"⏱️ ETA: {format_time(eta)}\n"
            f"⏳ Total Time: {format_time(elapsed)}"
        )
        
        try:
            await self.message.edit(progress_text)
        except Exception as e:
            logger.debug(f"Progress update error: {e}")

class OCEANVEIL:
    def __init__(self):
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.session = None
        self.auth_header = None
        self.cookies = None

    async def get_session(self):
        if self.session is None:
            self.session = AsyncSession(
                impersonate="chrome", 
                headers={"User-Agent": self.user_agent}
            )
        return self.session

    async def login(self):
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
            res = await session.get(
                url, headers={"authorization": self.auth_header}, cookies=self.cookies
            )
            if res.status_code != 200:
                logger.error(f"Failed to fetch episodes: {res.status_code}")
                return [], None, None, None, None

            json_resp = res.json()
            series_name = json_resp['data']["attributes"]["name"]
            
            series_name = clean_filename(series_name)
            
            is_dub = "dub" in series_name.lower()
            lang_code = "eng" if is_dub else "jpn"
            lang_name = "English" if is_dub else "Japanese"
            
            if 'included' in json_resp:
                for i in json_resp['included']:
                    if i.get('type') == "animeEpisode":
                        ep_name = i['attributes']['name']
                        ep_num = i['attributes']['displayNumber']
                        ep_id = i.get('id')
                        
                        full_name = f"{series_name} - Episode {ep_num} - {ep_name}"
                        full_name = clean_filename(full_name)
                        
                        data.append({
                            'name': full_name,
                            'url': f"https://oceanveil.net/api/v1/anime_episodes/{ep_id}/video.m3u8",
                            'ep_id': ep_id, 
                            'anime_id': id,
                            'ep_num': str(ep_num),
                            'ep_title': ep_name,
                            'series_name': series_name,
                            'lang_code': lang_code,
                            'lang_name': lang_name
                        })
            
            logger.info(f"Found {len(data)} episodes for {series_name} ({lang_name}).")
            return data, self.auth_header, self.cookies, self.user_agent, lang_code
        except Exception as e:
            logger.error(f"Error fetching ID {id}: {e}")
            return [], None, None, None, None

async def setup_tool():
    if os.path.exists(TOOL_BINARY): 
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

async def download_file(semaphore, ep_data, auth, cookies, ua, save_dir, cancel_event=None):
    """Downloads to a temp folder"""
    async with semaphore:
        if cancel_event and cancel_event.is_set():
            return None
            
        url = ep_data['url']
        temp_filename = f"temp_{ep_data['anime_id']}_{ep_data['ep_num']}"
        
        referer = f"https://oceanveil.net/anime_titles/{ep_data['anime_id']}?episode={ep_data['ep_id']}"
        cookies_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
        binary = os.path.abspath(TOOL_BINARY)
        
        os.makedirs(save_dir, exist_ok=True)
        final_path = os.path.join(save_dir, f"{temp_filename}.mp4")
        
        if os.path.exists(final_path):
            return final_path

        logger.info(f"[DOWNLOADING] Ep {ep_data['ep_num']}...")

        cmd = [
            binary, url,
            "-H", f"Cookie: {cookies_str}",
            "-H", f"Authorization: {auth}",
            "-H", f"User-Agent: {ua}",
            "-H", f"Referer: {referer}",
            "--auto-select", "--save-dir", save_dir, "--save-name", temp_filename, "--no-log"
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, 
            stdout=asyncio.subprocess.PIPE, 
            stderr=asyncio.subprocess.PIPE
        )
        
        while proc.returncode is None:
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                await proc.wait()
                return None
            await asyncio.sleep(0.5)
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
        
        if proc.returncode == 0:
            return final_path
        else:
            logger.error(f"[ERROR] Download failed for {ep_data['name']}")
            return None

def mux_dual_audio(audio_file, video_file, output_path, audio_lang, video_lang, video_has_primary_audio=False):
    """Muxes Audio from File 1 + Video from File 2"""
    logger.info(f"   -> Muxing: {os.path.basename(output_path)}")

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

# --- Bot Logic ---

# Optimize Client for speed
app = Client(
    "ocean_bot", 
    api_id=int(API_ID), 
    api_hash=API_HASH, 
    bot_token=BOT_TOKEN,
    ipv6=False # Sometimes fixes connection issues
)

# Increase chunk size for faster uploads
Client.UPLOAD_CHUNK_SIZE = 1024 * 1024 * 4  # 4MB chunks (Max is usually best)

ocean = None
sem = None

@app.on_message(filters.command(["start", "Start", "START"]))
async def start_cmd(client, message):
    await message.reply_text(
        "🌊 **OceanVeil Bot Ready!**\n\n"
        "📥 **Commands:**\n"
        "/dl <id> [ep] - Download episodes\n"
        "/engdl <id> [ep] - Download English dub\n"
        "/dual <id1> <id2> [ep] - Dual Audio (Sub Video)\n"
        "/engvdiddual <id1> <id2> [ep] - Dual Audio (Dub Video)\n\n"
        "✨ **Features:**\n"
        "• Episode ranges (1-5)\n"
        "• Specific episodes (5)\n"
        "• AniList thumbnails\n"
        "• Clean filenames\n"
        "• Queue system"
    )

@app.on_message(filters.command(["queue"]))
async def queue_cmd(client, message: Message):
    user_id = message.from_user.id
    
    if user_id not in active_tasks and user_id not in task_queue:
        await message.reply_text("📭 No active tasks.")
        return
    
    status_text = "📊 **Queue Status**\n\n"
    
    if user_id in active_tasks:
        task = active_tasks[user_id]
        status_text += f"🔄 **Active:**\n{task.get('description', 'Processing...')}\n\n"
    
    if user_id in task_queue and task_queue[user_id]:
        status_text += f"⏳ **Queued:** {len(task_queue[user_id])} tasks\n"
    
    await message.reply_text(status_text)

@app.on_message(filters.command(["cancel"]))
async def cancel_cmd(client, message: Message):
    user_id = message.from_user.id
    
    if user_id not in active_tasks:
        await message.reply_text("❌ No active task to cancel.")
        return
    
    task = active_tasks[user_id]
    if 'cancel_event' in task:
        task['cancel_event'].set()
        await message.reply_text("🛑 Cancelling...")
    else:
        await message.reply_text("❌ Cannot cancel this task.")

@app.on_message(filters.command(["dl", "engdl"]))
async def dl_cmd(client, message: Message):
    user_id = message.from_user.id
    args = message.command
    
    if len(args) < 2:
        await message.reply_text("Usage: /dl <id> [ep or range]")
        return
    
    if user_id in active_tasks:
        await message.reply_text("⚠️ You have an active task. Use /cancel first.")
        return
    
    aid = args[1]
    ep_filter = None
    
    if len(args) >= 3:
        ep_arg = args[2]
        if '-' in ep_arg:
            try:
                start, end = map(int, ep_arg.split('-'))
                ep_filter = ('range', start, end)
            except:
                await message.reply_text("❌ Invalid range. Use: 1-5")
                return
        else:
            try:
                ep_num = int(ep_arg)
                ep_filter = ('single', ep_num)
            except:
                await message.reply_text("❌ Invalid episode number.")
                return
    
    status = await message.reply_text(f"🔍 Fetching info for {aid}...")
    
    await setup_tool()

    episodes, auth, cookies, ua, lang_code = await ocean.get_episodes(aid)
    if not episodes:
        await status.edit("❌ Failed to fetch episodes.")
        return
    
    if ep_filter:
        if ep_filter[0] == 'single':
            episodes = [ep for ep in episodes if int(ep['ep_num']) == ep_filter[1]]
        elif ep_filter[0] == 'range':
            start, end = ep_filter[1], ep_filter[2]
            episodes = [ep for ep in episodes if start <= int(ep['ep_num']) <= end]
    
    if not episodes:
        await status.edit("❌ No episodes found.")
        return
    
    series_clean = episodes[0]['series_name']
    suffix = "[Dub]" if lang_code == "eng" else "[Sub]"
    
    await status.edit(f"🔍 Searching AniList...")
    anilist_data = await search_anilist(series_clean)
    
    thumbnail_path = None
    if anilist_data and anilist_data.get('thumbnail'):
        thumb_file = f"thumb_{uuid.uuid4().hex[:8]}.jpg"
        thumbnail_path = await download_thumbnail(anilist_data['thumbnail'], thumb_file)
    
    unique_id = str(uuid.uuid4())[:8]
    temp_dir = os.path.join("temp_work", unique_id)
    
    cancel_event = asyncio.Event()
    active_tasks[user_id] = {
        'task_id': unique_id,
        'cancel_event': cancel_event,
        'status_msg': status,
        'description': f"Downloading {series_clean}"
    }

    await status.edit(f"✅ Found {len(episodes)} episodes\n⬇️ Starting...")
    
    progress = ProgressTracker(status, len(episodes), f"Downloading & Uploading {suffix}")
    
    try:
        for idx, ep in enumerate(episodes, 1):
            if cancel_event.is_set():
                await status.edit("🛑 Cancelled.")
                break
            
            await progress.update_item(idx, ep['name'])
            
            temp_path = await download_file(sem, ep, auth, cookies, ua, temp_dir, cancel_event)
            if not temp_path or not os.path.exists(temp_path):
                continue
            
            final_name = create_short_filename(series_clean, ep['ep_num'], suffix)
            
            final_path = os.path.join(temp_dir, final_name)
            if os.path.exists(final_path):
                os.remove(final_path)
            shutil.move(temp_path, final_path)
            
            try:
                file_size = os.path.getsize(final_path)
                
                await status.edit(
                    f"📤 **Uploading {idx}/{len(episodes)}**\n"
                    f"📁 {final_name}\n"
                    f"📦 {format_bytes(file_size)}"
                )
                
                clean_series = clean_filename(series_clean)
                
                caption = (
                    f"🎬 **{clean_series}** - Episode {ep['ep_num']}\n"
                    f"📝 {ep['ep_title']}\n\n"
                    f"📦 Size: {format_bytes(file_size)}\n"
                    f"🎭 Type: {suffix}\n"
                    f"🌊 Source: OceanVeil"
                )
                
                if thumbnail_path and os.path.exists(thumbnail_path):
                    await message.reply_document(
                        document=final_path,
                        thumb=thumbnail_path,
                        caption=caption,
                        progress=progress.update_progress
                    )
                else:
                    await message.reply_document(
                        document=final_path,
                        caption=caption,
                        progress=progress.update_progress
                    )
                
                if os.path.exists(final_path):
                    os.remove(final_path)
                    
            except Exception as e:
                logger.error(f"Upload error: {e}")
        
        if not cancel_event.is_set():
            await status.edit(f"✅ **Completed!**\n📊 {len(episodes)} episodes")
        
    finally:
        if user_id in active_tasks:
            del active_tasks[user_id]
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        if thumbnail_path and os.path.exists(thumbnail_path):
            os.remove(thumbnail_path)

@app.on_message(filters.command(["dual", "engvdiddual"]))
async def dual_cmd(client, message: Message):
    user_id = message.from_user.id
    args = message.command
    
    if len(args) < 3:
        await message.reply_text("Usage: /dual <id1> <id2> [ep or range]")
        return
    
    if user_id in active_tasks:
        await message.reply_text("⚠️ You have an active task. Use /cancel first.")
        return

    await setup_tool()
    
    if shutil.which("ffmpeg") is None:
        await message.reply_text("❌ FFmpeg not installed.")
        return

    id1, id2 = args[1], args[2]
    ep_filter = None
    
    # Parse episode selection (3rd argument)
    if len(args) >= 4:
        ep_arg = args[3]
        if '-' in ep_arg:
            try:
                start, end = map(int, ep_arg.split('-'))
                ep_filter = ('range', start, end)
            except:
                await message.reply_text("❌ Invalid range. Use: 1-5")
                return
        else:
            try:
                ep_num = int(ep_arg)
                ep_filter = ('single', ep_num)
            except:
                await message.reply_text("❌ Invalid episode number.")
                return

    status = await message.reply_text("🔍 Fetching info...")
    
    eps1, auth1, cookies1, ua1, lang1 = await ocean.get_episodes(id1)
    eps2, auth2, cookies2, ua2, lang2 = await ocean.get_episodes(id2)
    
    if not eps1 or not eps2:
        await status.edit("❌ Failed to fetch info.")
        return

    is_engvdiddual = message.command[0] == "engvdiddual"
    
    if lang1 == "eng" and lang2 != "eng":
        dub_eps, sub_eps = eps1, eps2
        dub_id, sub_id = id1, id2
        dub_auth, dub_cookies, dub_ua = auth1, cookies1, ua1
        sub_auth, sub_cookies, sub_ua = auth2, cookies2, ua2
    elif lang2 == "eng" and lang1 != "eng":
        dub_eps, sub_eps = eps2, eps1
        dub_id, sub_id = id2, id1
        dub_auth, dub_cookies, dub_ua = auth2, cookies2, ua2
        sub_auth, sub_cookies, sub_ua = auth1, cookies1, ua1
    else:
        dub_eps, sub_eps = eps1, eps2
        dub_id, sub_id = id1, id2
        dub_auth, dub_cookies, dub_ua = auth1, cookies1, ua1
        sub_auth, sub_cookies, sub_ua = auth2, cookies2, ua2

    if is_engvdiddual:
        video_eps, audio_eps = dub_eps, sub_eps
        video_auth, video_cookies, video_ua = dub_auth, dub_cookies, dub_ua
        audio_auth, audio_cookies, audio_ua = sub_auth, sub_cookies, sub_ua
        video_has_primary = True
        video_id, audio_id = dub_id, sub_id
    else:
        video_eps, audio_eps = sub_eps, dub_eps
        video_auth, video_cookies, video_ua = sub_auth, sub_cookies, sub_ua
        audio_auth, audio_cookies, audio_ua = dub_auth, dub_cookies, dub_ua
        video_has_primary = False
        video_id, audio_id = sub_id, dub_id

    # Filter episodes
    if ep_filter:
        if ep_filter[0] == 'single':
            video_eps = [ep for ep in video_eps if int(ep['ep_num']) == ep_filter[1]]
            audio_eps = [ep for ep in audio_eps if int(ep['ep_num']) == ep_filter[1]]
        elif ep_filter[0] == 'range':
            start, end = ep_filter[1], ep_filter[2]
            video_eps = [ep for ep in video_eps if start <= int(ep['ep_num']) <= end]
            audio_eps = [ep for ep in audio_eps if start <= int(ep['ep_num']) <= end]

    if not video_eps or not audio_eps:
        await status.edit("❌ No episodes found matching filter.")
        return

    series_clean = video_eps[0]['series_name']
    
    await status.edit(f"🔍 Searching AniList...")
    anilist_data = await search_anilist(series_clean)
    
    thumbnail_path = None
    if anilist_data and anilist_data.get('thumbnail'):
        thumb_file = f"thumb_{uuid.uuid4().hex[:8]}.jpg"
        thumbnail_path = await download_thumbnail(anilist_data['thumbnail'], thumb_file)

    unique_id = str(uuid.uuid4())[:8]
    task_root = os.path.join("temp_work", unique_id)
    audio_temp = os.path.join(task_root, "audio")
    video_temp = os.path.join(task_root, "video")
    
    cancel_event = asyncio.Event()
    active_tasks[user_id] = {
        'task_id': unique_id,
        'cancel_event': cancel_event,
        'status_msg': status,
        'description': f"Dual Audio"
    }
    
    progress = ProgressTracker(status, len(video_eps), "Dual Audio")
    
    try:
        await status.edit("⬇️ Downloading...")
        
        tasks = []
        for ep in audio_eps: 
            tasks.append(download_file(sem, ep, audio_auth, audio_cookies, audio_ua, audio_temp, cancel_event))
        for ep in video_eps: 
            tasks.append(download_file(sem, ep, video_auth, video_cookies, video_ua, video_temp, cancel_event))
        await asyncio.gather(*tasks)
        
        if cancel_event.is_set():
            await status.edit("🛑 Cancelled.")
            return
        
        audio_map = {}
        for ep in audio_eps:
            path = os.path.join(audio_temp, f"temp_{audio_id}_{ep['ep_num']}.mp4")
            if os.path.exists(path): 
                audio_map[str(ep['ep_num'])] = path

        await status.edit("🔄 Muxing...")
        
        for idx, ep in enumerate(video_eps, 1):
            if cancel_event.is_set():
                break
                
            await progress.update_item(idx, ep['name'])
            
            ep_num = str(ep['ep_num'])
            video_path = os.path.join(video_temp, f"temp_{video_id}_{ep_num}.mp4")
            
            if not os.path.exists(video_path): 
                continue
                
            if ep_num in audio_map:
                audio_path = audio_map[ep_num]
                
                final_filename = create_short_filename(series_clean, ep['ep_num'], "[Dual]")
                output_path = os.path.join(task_root, final_filename)
                
                a_lang = audio_eps[0]['lang_code']
                v_lang = video_eps[0]['lang_code']
                
                if mux_dual_audio(audio_path, video_path, output_path, a_lang, v_lang, video_has_primary):
                    try:
                        file_size = os.path.getsize(output_path)
                        
                        await status.edit(
                            f"📤 **Uploading {idx}/{len(video_eps)}**\n"
                            f"📁 {final_filename}\n"
                            f"📦 {format_bytes(file_size)}"
                        )
                        
                        clean_series = clean_filename(series_clean)
                        
                        caption = (
                            f"🎬 **{clean_series}** - Episode {ep['ep_num']}\n"
                            f"📝 {ep['ep_title']}\n\n"
                            f"📦 Size: {format_bytes(file_size)}\n"
                            f"🎭 Type: [Dual Audio]\n"
                            f"🌊 Source: OceanVeil"
                        )
                        
                        if thumbnail_path and os.path.exists(thumbnail_path):
                            await message.reply_document(
                                document=output_path,
                                thumb=thumbnail_path,
                                caption=caption,
                                progress=progress.update_progress
                            )
                        else:
                            await message.reply_document(
                                document=output_path,
                                caption=caption,
                                progress=progress.update_progress
                            )
                        
                        if os.path.exists(output_path):
                            os.remove(output_path)
                    except Exception as e:
                        logger.error(f"Upload error: {e}")
        
        if not cancel_event.is_set():
            await status.edit(f"✅ **Complete!**\n📊 {len(video_eps)} episodes")

    finally:
        if user_id in active_tasks:
            del active_tasks[user_id]
        if os.path.exists(task_root):
            shutil.rmtree(task_root, ignore_errors=True)
        if thumbnail_path and os.path.exists(thumbnail_path):
            os.remove(thumbnail_path)

if __name__ == "__main__":
    async def main():
        global ocean, sem
        ocean = OCEANVEIL()
        sem = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        
        print("Starting Bot...")
        await app.start()
        print("Bot running. Press Ctrl+C to stop.")
        await idle()
        await app.stop()

    app.run(main())
