import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urljoin, unquote

import requests
from curl_cffi import requests as curl_requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, BotCommandScopeChat
import schedule
from bs4 import BeautifulSoup

# ============ الإعدادات ============
BOT_TOKEN = "7808630939:AAE-kiAUlzvcjYhiRVUpjE7DX6_iivvBIRE"
ADMIN_CHAT_ID = "1013251619"
SECRET_KEY = "ArabFleex_2024_SecRet"

FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "https://arabfleex-86df3-default-rtdb.firebaseio.com").rstrip('/')
FIREBASE_SECRET = os.environ.get("FIREBASE_SECRET", "")

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
DATA_FILE = os.path.join(DATA_DIR, "series.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

CHECK_INTERVAL_SECONDS = 40
DOMAIN_CACHE_TTL = 300  # ثواني: مدة الاحتفاظ بنتيجة فحص الدومين
API_URL = "https://arabfleex.live/api_bot.php"

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

scan_lock = threading.Lock()
data_lock = threading.RLock()
last_scan_at = None
scan_cycles = 0
total_added = 0
bot_start_time = datetime.now(timezone.utc)

global_laroza_domain = "https://larooza.asia"
manual_domain = None
_domain_checked_at = 0
_domain_cached = None

REAL_VIDEO_HOSTS = [
    "streamtape", "uqload", "doodstream", "dood.", "mp4upload",
    "voe.sx", "filemoon", "ok.ru", "vk.com", "youtube.com",
    "dailymotion", "upvid", "vidoza", "mixdrop", "upstream",
    "supervideo", "videobin", "fembed", "jawcloud", "jetload",
    "hxfile", "vtbe", "vidbom", "vidhide", "playtube",
    "waaw", "wolfstream", "embedgram", "gdriveplayer",
    "gofile", "streamflare", "streamvid", "tubeload",
    "okhd.site", "film77.xyz", "vidoba.org", "larhu.website",
    "mp4plus.org", "anafast.org", "vidspeed.org", "abstream.to",
    "dsvplay.com", "vidbam.org", "vidmoly.to", "streamwish",
    "embedwish", "filelions", "swiftplayer", "vidsrc",
    "turbobit", "nitroflare", "1fichier", "rty"
]

BANNED_SERVERS = ['fembed', 'nitro', 'arabseed', 'wecima']


def is_trusted_server(url):
    """vidspeed / mp4plus / rty (rty1, rty2...) — بتطابق على أجزاء الدومين فقط"""
    if not url:
        return False
    labels = urlparse(url).netloc.lower().split('.')
    return any(l in ("vidspeed", "mp4plus") or re.fullmatch(r"rty\d*", l) for l in labels)


LAROZA_KNOWN_DOMAINS = [
    "https://larooza.asia",
    "https://laroza.lat",
    "https://larozza.forum",
    "https://larozza.beer",
    "https://larroza.baby",
    "https://larroza.click",
    "https://larroza.casa",
    "https://laroza.live",
    "https://laroza.site"
]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_firebase_url(path):
    url = f"{FIREBASE_DB_URL}/{path}.json"
    if FIREBASE_SECRET:
        url += f"?auth={FIREBASE_SECRET}"
    return url


def is_admin(message):
    return str(message.chat.id) == str(ADMIN_CHAT_ID)


# ============ Config ============
def load_config():
    global global_laroza_domain, manual_domain
    try:
        res = requests.get(get_firebase_url("config"), timeout=10)
        if res.status_code == 200 and res.json():
            data = res.json()
            if isinstance(data, dict):
                if data.get("laroza_domain"):
                    global_laroza_domain = data["laroza_domain"]
                if "manual_domain" in data:
                    manual_domain = data["manual_domain"]
                return
    except Exception as e:
        log(f"[FIREBASE ERROR] load_config: {e}")

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            if data.get("laroza_domain"):
                global_laroza_domain = data["laroza_domain"]
            if "manual_domain" in data:
                manual_domain = data["manual_domain"]
        except Exception as e:
            log(f"[CONFIG ERROR] {e}")


def save_config():
    payload = {"laroza_domain": global_laroza_domain, "manual_domain": manual_domain}
    firebase_ok = False
    try:
        res = requests.put(get_firebase_url("config"), json=payload, timeout=10)
        if res.status_code == 200:
            firebase_ok = True
        else:
            log(f"[FIREBASE ERROR] save_config status: {res.status_code}")
    except Exception as e:
        log(f"[FIREBASE ERROR] save_config: {e}")

    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False)
    except Exception as e:
        log(f"[CONFIG SAVE ERROR] {e}")

    return firebase_ok


load_config()


# ============ Series data ============
def load_series_data():
    try:
        res = requests.get(get_firebase_url("series"), timeout=10)
        if res.status_code == 200 and res.json():
            data = res.json()
            if isinstance(data, dict):
                try:
                    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
                    with open(DATA_FILE, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    log(f"[LOCAL SAVE ERROR] {e}")
                return data
    except Exception as e:
        log(f"[FIREBASE ERROR] load_series_data: {e}")

    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_series_data(data):
    """
    تحفظ البيانات في فايربيز وفي الملف المحلي.
    ترجع True إذا تم الحفظ في Firebase بنجاح (Status Code 200).
    """
    firebase_ok = False
    try:
        res = requests.put(get_firebase_url("series"), json=data, timeout=10)
        if res.status_code == 200:
            firebase_ok = True
        else:
            log(f"[FIREBASE SAVE FAIL] Status Code: {res.status_code}, Response: {res.text}")
    except Exception as e:
        log(f"[FIREBASE ERROR] save_series_data: {e}")

    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        temp_file = f"{DATA_FILE}.tmp"
        with open(temp_file, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(temp_file, DATA_FILE)
    except Exception as e:
        log(f"[LOCAL SAVE ERROR] {e}")

    return firebase_ok


def send_telegram_msg(msg):
    try:
        bot.send_message(ADMIN_CHAT_ID, msg, parse_mode="HTML")
    except Exception as e:
        log(f"[TELEGRAM ERROR] {e}")


# ============ Helpers ============
def fetch_html(url):
    try:
        res = curl_requests.get(url, impersonate="chrome", timeout=15, verify=False)
        if res.status_code != 200:
            return ""
        return res.text
    except Exception:
        return ""


def extract_vid(url):
    qs = parse_qs(urlparse(url).query)
    return qs.get('vid', [None])[0]


def is_real_server(url):
    if not url:
        return False
    url_lower = url.lower()
    for ban in BANNED_SERVERS:
        if ban in url_lower:
            return False
    return any(host in url_lower for host in REAL_VIDEO_HOSTS)


def extract_video_fingerprint(url):
    if not url:
        return None
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        parts = [p for p in parsed.path.strip('/').lower().split('/') if p]
        if len(parts) >= 2:
            return f"{netloc}/{parts[0]}/{parts[1]}"
        base = f"{netloc}/{parts[0]}" if parts else netloc
        if parsed.query:
            qs = parse_qs(parsed.query, keep_blank_values=False)
            id_params = ['id', 'oid', 'v', 'vid', 'file', 'i', 'key', 'uid', 'code']
            id_parts = [f"{p}={qs[p][0].lower()}" for p in id_params if p in qs]
            if id_parts:
                return f"{base}?{'&'.join(id_parts)}"
            first_key = sorted(qs.keys())[0]
            return f"{base}?{first_key}={qs[first_key][0][:40].lower()}"
        return base
    except Exception:
        return None


def collect_fingerprints(laroza_url):
    fps = []
    try:
        vid = extract_vid(laroza_url)
        if not vid:
            return fps
        play_html = fetch_html(urljoin(laroza_url, f"/play.php?vid={vid}"))
        play_soup = BeautifulSoup(play_html, 'html.parser')
        for tag in play_soup.select('[data-embed-url]'):
            u = tag.get('data-embed-url')
            if u and is_trusted_server(u):
                fp = extract_video_fingerprint(u)
                if fp and fp not in fps:
                    fps.append(fp)
    except Exception as e:
        log(f"[FP ERROR] {e}")
    return fps


def replace_domain(url, new_domain):
    if not url:
        return url
    p_url = urlparse(url)
    p_dom = urlparse(new_domain)
    return p_url._replace(scheme=p_dom.scheme, netloc=p_dom.netloc).geturl()


# ============ Domain discovery ============
LAROZA_NAME_KEYS = ["larozaa", "larozza", "laroza", "larooza", "larroza"]


def _test_domain(d):
    if not d:
        return None
    try:
        res = curl_requests.get(d, impersonate="chrome", timeout=12, verify=False)
        if res.status_code != 200:
            return None
        text = res.text
        final = urlparse(str(res.url))
        final_dom = f"{final.scheme}://{final.netloc}" if final.netloc else d.rstrip('/')

        strong = any(m in text for m in ("video.php", "view-serie", "SeasonsEpisodes", "play.php", "category.php"))
        weak = "laroza" in text.lower() or "larooza" in text.lower()
        name_ok = any(k in final.netloc.lower() for k in LAROZA_NAME_KEYS)

        if strong or (weak and name_ok):
            return final_dom
    except Exception as e:
        log(f"[DOMAIN TEST] {d}: {e}")
    return None


def _decode_ddg_href(href):
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    if 'uddg' in qs:
        return unquote(qs['uddg'][0])
    return href


def _discover_domain():
    global global_laroza_domain
    search_queries = ["موقع لاروزا الاصلي", "laroza مسلسلات", "لاروزا فيديو", "larooza"]
    exclude_domains = ["youtube.com", "dailymotion.com", "yandex.com", "facebook.com",
                       "twitter.com", "laaroza-tv.com", "cima.laaroza", "larozaa.top",
                       "duckduckgo.com"]
    tried = set()

    for query in search_queries:
        try:
            req = curl_requests.post("https://lite.duckduckgo.com/lite/", data={"q": query},
                                     timeout=15, impersonate="chrome", verify=False)
            soup = BeautifulSoup(req.text, 'html.parser')
            for a in soup.find_all('a', href=True):
                real = _decode_ddg_href(a.get('href', ''))
                parsed = urlparse(real)
                netloc = parsed.netloc.lower()
                if not netloc or not parsed.scheme.startswith('http'):
                    continue
                if any(ex in netloc for ex in exclude_domains):
                    continue
                if any(kw in netloc for kw in LAROZA_NAME_KEYS):
                    candidate = f"{parsed.scheme}://{parsed.netloc}"
                    if candidate in tried:
                        continue
                    tried.add(candidate)
                    found = _test_domain(candidate)
                    if found:
                        global_laroza_domain = found
                        save_config()
                        return found
        except Exception as e:
            log(f"[DISCOVER ERROR] {e}")
    return None


def get_working_laroza_domain(force=False):
    global global_laroza_domain, _domain_checked_at, _domain_cached

    if not force and _domain_cached and (time.time() - _domain_checked_at) < DOMAIN_CACHE_TTL:
        return _domain_cached

    candidates, seen = [], set()
    for c in [manual_domain, global_laroza_domain] + LAROZA_KNOWN_DOMAINS:
        if c and c not in seen:
            seen.add(c)
            candidates.append(c)

    result = None
    for c in candidates:
        found = _test_domain(c)
        if found:
            result = found
            break

    if not result:
        result = _discover_domain()

    if result:
        if result != global_laroza_domain:
            log(f"[DOMAIN] الدومين الشغال: {result}")
            global_laroza_domain = result
            save_config()
        _domain_cached = result
        _domain_checked_at = time.time()
    else:
        log("[DOMAIN] مفيش دومين شغال حالياً")
        _domain_cached = manual_domain or global_laroza_domain
        _domain_checked_at = time.time() - DOMAIN_CACHE_TTL + 30

    return _domain_cached


# ============ Server selection ============
def select_servers(watch_urls, down_urls):
    seen = set()
    w_pool = [x for x in watch_urls if is_real_server(x) and not (x in seen or seen.add(x))]
    seen = set()
    d_pool = [x for x in down_urls if not (x in seen or seen.add(x))]

    w_pool.sort(key=lambda u: 'vidspeed' in u.lower())

    def pop_match(pool, keywords):
        for kw in keywords:
            for url in pool:
                if kw.lower() in url.lower():
                    pool.remove(url)
                    return url
        return ""

    def pop_first(pool, exclude=None):
        for url in pool:
            if exclude and exclude in url.lower():
                continue
            pool.remove(url)
            return url
        return ""

    w1 = pop_match(w_pool, ['mp4plus', 'mp4'])
    if not w1:
        w1 = pop_first(w_pool, exclude='vidspeed')

    w2 = pop_match(w_pool, ['rty', 'uqload'])
    if not w2:
        w2 = pop_first(w_pool)

    w3 = pop_match(w_pool, ['ok.ru', 'vk.com'])
    if not w3:
        w3 = pop_first(w_pool)

    w4 = pop_match(w_pool, ['filemoon', 'dood', 'streamtape', 'voe'])
    if not w4:
        w4 = pop_first(w_pool)

    d1 = pop_match(d_pool, ['liiivideo', 'livideo'])
    if not d1:
        d1 = pop_match(d_pool, ['uqload'])
    d2 = pop_match(d_pool, ['1cloud'])
    if not d1 and d_pool: d1 = d_pool.pop(0)
    if not d2 and d_pool: d2 = d_pool.pop(0)

    return w1, w2, w3, w4, d1, d2


# ============ Scraping ============
def _abs_url(current_url, href):
    clean = href.lstrip('.')
    if not clean.startswith('/'):
        clean = '/' + clean
    p = urlparse(current_url)
    return urljoin(f"{p.scheme}://{p.netloc}", clean)


def get_laroza_ep(current_url, target_ep, seen_fps):
    html_content = fetch_html(current_url)
    if not html_content:
        return None
    if not any(m in html_content for m in ("SeasonsEpisodes", "mobileselect", "video.php", "data-embed-url")):
        return None
    soup = BeautifulSoup(html_content, 'html.parser')

    target_url = None
    active_div = None

    active_tab = soup.select_one('.SeasonsBoxUL li.active')
    if active_tab and active_tab.has_attr('data-serie'):
        active_div = soup.select_one(f'.SeasonsEpisodes[data-serie="{active_tab["data-serie"]}"]')

    if not active_div:
        for div in soup.select('.SeasonsEpisodes'):
            if 'display:none' not in div.get('style', '').replace(' ', ''):
                active_div = div
                break

    search_container = active_div if active_div else soup
    for a in search_container.select('a'):
        href = a.get('href', '')
        if 'video.php' not in href:
            continue
        em = a.find('em')
        text = a.text.strip()
        if (em and em.text.strip() == str(target_ep)) or re.search(r'(?:الحلقة|حلقة)\s*' + str(target_ep) + r'\b', text):
            target_url = _abs_url(current_url, href)
            break

    if not target_url:
        mobile_container = soup
        active_mob = soup.select_one('select#mobileselect option.mactive')
        if active_mob and active_mob.has_attr('value'):
            mobile_container = soup.select_one(f'select#{active_mob["value"]}') or soup

        for option in mobile_container.find_all('option'):
            text = option.text.strip()
            if re.search(r'(?:الحلقة|حلقة)\s*' + str(target_ep) + r'\b', text):
                val = option.get('value')
                if val and val != "select-ep":
                    target_url = _abs_url(current_url, val)
                    break

    if not target_url:
        return "WAITING"

    target_html = html_content if target_url == current_url else fetch_html(target_url)
    if not target_html:
        return "WAITING"

    vid = extract_vid(target_url)
    if not vid:
        return "WAITING"

    target_soup = BeautifulSoup(target_html, 'html.parser')
    watch_urls = [t.get('data-embed-url') for t in target_soup.select('[data-embed-url]') if t.get('data-embed-url')]

    if not watch_urls:
        play_html = fetch_html(urljoin(target_url, f"/play.php?vid={vid}"))
        play_soup = BeautifulSoup(play_html, 'html.parser')
        watch_urls = [t.get('data-embed-url') for t in play_soup.select('[data-embed-url]') if t.get('data-embed-url')]

    dl_html = fetch_html(urljoin(target_url, f"/download.php?vid={vid}"))
    dl_soup = BeautifulSoup(dl_html, 'html.parser')
    down_urls = [t.get('data-download-url') for t in dl_soup.select('[data-download-url]') if t.get('data-download-url')]

    trusted = [u for u in watch_urls if is_trusted_server(u)]
    if not trusted:
        return "FAKE"

    for link in trusted:
        fp = extract_video_fingerprint(link)
        if fp and fp in seen_fps:
            return "FAKE"

    valid_watches = [u for u in watch_urls if is_real_server(u)]
    return {"url": target_url, "watch_urls": valid_watches,
            "down_urls": down_urls, "trusted_urls": trusted}


def api_insert_ok(api_res):
    if api_res.status_code != 200:
        return False
    txt = api_res.text.lower()
    bad_words = ["error", "fail", "invalid", "unauthorized", "خطأ", "فشل"]
    return not any(w in txt for w in bad_words)


def scan_item(slug, info):
    global total_added
    target_ep = info["last_ep"] + 1

    if not info.get("laroza_url"):
        info["status_text"] = "لا يوجد رابط لاروزا"
        return False

    active_domain = get_working_laroza_domain()
    current_url = replace_domain(info["laroza_url"], active_domain)

    if "laroza_seen_fps" not in info:
        info["laroza_seen_fps"] = []

    res = get_laroza_ep(current_url, target_ep, info["laroza_seen_fps"])

    if res is None:
        new_dom = get_working_laroza_domain(force=True)
        if new_dom and new_dom != active_domain:
            current_url = replace_domain(info["laroza_url"], new_dom)
            res = get_laroza_ep(current_url, target_ep, info["laroza_seen_fps"])

    if res is None:
        info["status_text"] = "تعذر فتح صفحة لاروزا (جاري البحث عن الدومين)"
        return False
    if res == "WAITING":
        info["status_text"] = f"بانتظار ح{target_ep}"
        return False
    if res == "FAKE":
        info["status_text"] = f"ح{target_ep} وهمية — بانتظار السيرفر الحقيقي"
        return False

    if isinstance(res, dict):
        w1, w2, w3, w4, d1, d2 = select_servers(res["watch_urls"], res["down_urls"])

        if not any([w1, w2, w3, w4]):
            info["status_text"] = f"ح{target_ep} لا تحتوي سيرفرات صالحة"
            return False

        payload = {
            "secret_key": SECRET_KEY, "action": "insert", "series_id": info["series_id"],
            "title": f"الحلقة {target_ep}", "episode_number": target_ep,
            "watch_link": w1, "watch_link_2": w2, "watch_link_3": w3, "watch_link_4": w4,
            "download_link": d1, "download_link_2": d2
        }

        try:
            api_res = requests.post(API_URL, data=payload, timeout=30)
        except Exception as e:
            log(f"[API ERROR] {e}")
            info["status_text"] = f"خطأ اتصال بالـ API في ح{target_ep}"
            return False

        api_text = api_res.text.strip()
        safe_api_status = api_text[:50].replace('<', '').replace('>', '')

        if not api_insert_ok(api_res):
            info["status_text"] = f"❌ الـ API رفض ح{target_ep}: {safe_api_status}"
            send_telegram_msg(f"⚠️ <b>فشل إضافة الحلقة {target_ep}</b>\n📺 {info['title']}\n<code>{safe_api_status}</code>")
            return False

        total_added += 1
        info["last_ep"] = target_ep
        info["laroza_url"] = res["url"]
        info["status_text"] = f"✅ تم اصطياد ح{target_ep}"

        for u in res["trusted_urls"]:
            fp = extract_video_fingerprint(u)
            if fp and fp not in info["laroza_seen_fps"]:
                info["laroza_seen_fps"].append(fp)
        info["laroza_seen_fps"] = info["laroza_seen_fps"][-50:]

        msg = f"🎬 <b>تم اصطياد وتحديث الحلقة {target_ep}</b>\n"
        msg += f"📺 <b>المسلسل:</b> {info['title']}\n"
        msg += f"🌐 <b>حالة الـ API:</b> <code>{safe_api_status}</code>\n"
        send_telegram_msg(msg)
        return True

    return False


def scan_all_series_once():
    global last_scan_at, scan_cycles
    if not scan_lock.acquire(blocking=False):
        return
    try:
        scan_cycles += 1
        last_scan_at = datetime.now(timezone.utc)

        slugs = list(load_series_data().keys())

        for slug in slugs:
            try:
                data = load_series_data()
                if slug not in data:
                    continue

                info = data[slug]
                ep_before_scan = info.get("last_ep")

                scan_item(slug, info)

                with data_lock:
                    latest_data = load_series_data()
                    if slug in latest_data:
                        if latest_data[slug].get("last_ep") != ep_before_scan:
                            continue
                        latest_data[slug] = info
                        save_series_data(latest_data)
            except Exception as e:
                log(f"[ERROR] مسلسل {slug}: {e}")

    except Exception as e:
        log(f"[ERROR] Error during full scan: {e}")
    finally:
        scan_lock.release()


schedule.every(CHECK_INTERVAL_SECONDS).seconds.do(scan_all_series_once)


def run_scheduler():
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            log(f"[SCHEDULER ERROR] {e}")
        time.sleep(1)


# ============ Telegram handlers ============
def get_text(message):
    t = getattr(message, "text", None)
    return t.strip() if t else None


@bot.message_handler(commands=["start", "help"])
def welcome(message):
    if not is_admin(message): return
    bot.reply_to(message, "🤖 <b>نظام المراقبة الذكي V2 (لاروزا)</b>\n\n🔹 <code>/add</code> — إضافة مسلسل\n🔹 <code>/del</code> — حذف مسلسل\n🔹 <code>/setep</code> — تعديل رقم آخر حلقة\n🔹 <code>/status</code> — تقرير الحالة الشامل\n🔹 <code>/testfb</code> — 🧪 فحص الاتصال بـ Firebase فوراً\n🔹 <code>/setdomain</code> — تحديد دومين لاروزا يدوي 🔒\n🔹 <code>/cleardomain</code> — إلغاء الدومين اليدوي 🔓\n🔹 <code>/checkdomain</code> — فحص الدومين الآن\n🔹 <code>/backup</code> — نسخة احتياطية\n🔹 <code>/restore</code> — استعادة البيانات", parse_mode="HTML")


@bot.message_handler(commands=["testfb"])
def test_firebase_cmd(message):
    """أمر جديد لفحص الاتصال بـ Firebase والتأكد من إمكانية القراءة والكتابة."""
    if not is_admin(message): return
    bot.reply_to(message, "⏳ جاري فحص الاتصال وقواعد Firebase...")
    try:
        read_res = requests.get(get_firebase_url("config"), timeout=10)
        read_ok = read_res.status_code == 200

        test_payload = {"last_ping": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        write_res = requests.patch(get_firebase_url("test_ping"), json=test_payload, timeout=10)
        write_ok = write_res.status_code == 200

        if read_ok and write_ok:
            msg = "☁️ <b>حالة Firebase: ممتاز ومتصل بنجاح!</b>\n\n"
            msg += f"✅ <b>القراءة:</b> شغالة (200 OK)\n"
            msg += f"✅ <b>الكتابة:</b> مسموح بها وتحفظ سحابياً فوراً!"
        else:
            msg = "⚠️ <b>يوجد خلل في الاتصال بـ Firebase:</b>\n\n"
            msg += f"📖 القراءة: {'✅ 200 OK' if read_ok else f'❌ Error ({read_res.status_code})'}\n"
            msg += f"✍️ الكتابة: {'✅ 200 OK' if write_ok else f'❌ Error ({write_res.status_code})'}\n\n"
            msg += "تأكد من إعدادات Rules في موقع Firebase."
    except Exception as e:
        msg = f"❌ <b>خطأ أثناء فحص Firebase:</b> {e}"

    bot.send_message(message.chat.id, msg, parse_mode="HTML")


@bot.message_handler(commands=["add"])
def add_item_start(message):
    if not is_admin(message): return
    msg = bot.reply_to(message, "📝 <b>أرسل اسم المسلسل:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, add_step_title)


def add_step_title(message):
    if not is_admin(message): return
    title = get_text(message)
    if not title:
        return bot.reply_to(message, "❌ أرسل اسم المسلسل كنص. ابدأ من جديد بـ /add")
    msg = bot.reply_to(message, "🔢 <b>أرسل ID المسلسل في موقعك:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, add_step_id, title)


def add_step_id(message, title):
    if not is_admin(message): return
    try:
        series_id = int(get_text(message))
    except Exception:
        return bot.reply_to(message, "❌ يجب أن يكون رقماً. ابدأ من جديد بـ /add")
    msg = bot.reply_to(message, "🔢 <b>أرسل رقم آخر حلقة نزلت في موقعك دلوقتي:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, add_step_last_ep, title, series_id)


def add_step_last_ep(message, title, series_id):
    if not is_admin(message): return
    try:
        last_ep = int(get_text(message))
    except Exception:
        return bot.reply_to(message, "❌ يجب أن يكون رقماً. ابدأ من جديد بـ /add")
    msg = bot.reply_to(message, "🔗 <b>أرسل رابط آخر حلقة من موقع لاروزا</b>\n(الرابط لازم يحتوي على video.php):", parse_mode="HTML")
    bot.register_next_step_handler(msg, add_step_laroza, title, series_id, last_ep)


def add_step_laroza(message, title, series_id, last_ep):
    global global_laroza_domain
    if not is_admin(message): return
    laroza_url = get_text(message)
    if not laroza_url or "video.php" not in laroza_url:
        msg = bot.reply_to(message, "❌ الرابط لا يحتوي على video.php!\nأرسل رابط الحلقة الصحيح.", parse_mode="HTML")
        bot.register_next_step_handler(msg, add_step_laroza, title, series_id, last_ep)
        return

    bot.send_message(message.chat.id, "⏳ جاري حفظ بصمات لاروزا الحالية وتزامن مع Firebase...")

    parsed = urlparse(laroza_url)
    if not manual_domain:
        global_laroza_domain = f"{parsed.scheme}://{parsed.netloc}"
        save_config()

    laroza_seen_fps = collect_fingerprints(laroza_url)

    slug = f"series_{series_id}"
    with data_lock:
        data = load_series_data()
        data[slug] = {
            "title": title, "series_id": series_id, "last_ep": last_ep,
            "laroza_url": laroza_url, "laroza_seen_fps": laroza_seen_fps,
            "status_text": "بانتظار الفحص"
        }
        saved_to_firebase = save_series_data(data)

    if saved_to_firebase:
        fb_status = "☁️ <b>تم الحفظ في Firebase سحابياً بنجاح!</b>"
    else:
        fb_status = "⚠️ <b>تنبيه:</b> فشل التزامن مع Firebase! (تم الحفظ محلياً فقط)"

    bot.reply_to(message, f"✅ <b>تمت إضافة المسلسل بنجاح!</b>\n{fb_status}\n\nالبوت مستعد لمراقبة حلقة {last_ep + 1}.", parse_mode="HTML")


@bot.message_handler(commands=["setdomain"])
def set_domain_cmd(message):
    if not is_admin(message): return
    msg = bot.reply_to(message, "🔗 <b>أرسل رابط الدومين اليدوي الجديد لموقع لاروزا:</b>\n(مثال: https://larooza.asia)", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_setdomain)


def process_setdomain(message):
    global manual_domain
    if not is_admin(message): return
    new_url = get_text(message)
    if not new_url:
        return bot.reply_to(message, "❌ أرسل الرابط كنص.")
    new_url = new_url.rstrip('/')
    if not new_url.startswith("http"):
        new_url = "https://" + new_url

    manual_domain = new_url
    saved_fb = save_config()
    get_working_laroza_domain(force=True)

    status_fb = "☁️ تم الحفظ سحابياً في Firebase." if saved_fb else "⚠️ تم الحفظ محلياً فقط."
    bot.reply_to(message, f"✅ <b>تم تثبيت الدومين اليدوي بنجاح:</b>\n<code>{new_url}</code>\n<i>({status_fb})</i>", parse_mode="HTML")


@bot.message_handler(commands=["cleardomain"])
def clear_domain_cmd(message):
    global manual_domain
    if not is_admin(message): return
    manual_domain = None
    save_config()
    get_working_laroza_domain(force=True)
    bot.reply_to(message, "🔓 <b>تم إلغاء الدومين اليدوي.</b>\nالبوت الآن سيعتمد على الاكتشاف التلقائي للدومين الشغال.", parse_mode="HTML")


@bot.message_handler(commands=["checkdomain"])
def check_domain_cmd(message):
    if not is_admin(message): return
    bot.reply_to(message, "⏳ جاري فحص الدومينات...")
    dom = get_working_laroza_domain(force=True)
    ok = _test_domain(dom) is not None
    bot.send_message(message.chat.id,
                     f"🌐 <b>الدومين المعتمد:</b> <code>{dom}</code>\n"
                     f"{'✅ شغال' if ok else '❌ مش شغال حالياً'}"
                     f"{' (يدوي 🔒)' if manual_domain else ' (تلقائي 🔄)'}",
                     parse_mode="HTML")


@bot.message_handler(commands=["status", "list"])
def list_items(message):
    if not is_admin(message): return
    data = load_series_data()

    now = datetime.now(timezone.utc)
    uptime_seconds = (now - bot_start_time).total_seconds()
    h = int(uptime_seconds // 3600)
    m = int((uptime_seconds % 3600) // 60)
    sec = int(uptime_seconds % 60)

    start_str = bot_start_time.strftime("%Y-%m-%d %H:%M:%S")
    last_scan_str = last_scan_at.strftime("%Y-%m-%d %H:%M:%S") if last_scan_at else "لم يبدأ بعد"

    current_dom = _domain_cached or manual_domain or global_laroza_domain
    dom_type = " (يدوي 🔒)" if manual_domain else " (تلقائي 🔄)"

    msg_text = "✅ <b>البوت شغّال وتمام!</b>\n\n"
    msg_text += f"⏱ <b>وقت التشغيل:</b> {h}س {m}د {sec}ث\n"
    msg_text += f"🕐 <b>بدأ في:</b> {start_str}\n"
    msg_text += f"🔍 <b>آخر فحص:</b> {last_scan_str}\n"
    msg_text += f"🌐 <b>الدومين الحالي:</b> {current_dom}{dom_type}\n"
    msg_text += f"🔄 <b>عدد دورات الفحص:</b> {scan_cycles}\n"
    msg_text += f"➕ <b>إجمالي الحلقات المضافة:</b> {total_added}\n\n"
    msg_text += "📺 <b>آخر حالة للمسلسلات:</b>\n\n"

    if not data:
        msg_text += "📭 لا توجد مسلسلات مضافة."
    else:
        for slug, info in data.items():
            title = str(info.get("title", slug)).replace('<', '').replace('>', '')
            last_ep = info.get("last_ep", 0)
            status_txt = str(info.get("status_text", "بانتظار الفحص")).replace('<', '').replace('>', '')
            msg_text += f"🔹 <b>{title}:</b> حلقة {last_ep} — <i>{status_txt}</i>\n"

    for i in range(0, len(msg_text), 4000):
        bot.send_message(message.chat.id, msg_text[i:i + 4000], parse_mode="HTML")


@bot.message_handler(commands=["setep"])
def set_ep_start(message):
    if not is_admin(message): return
    data = load_series_data()
    if not data: return bot.reply_to(message, "📭 القائمة فارغة.")
    markup = InlineKeyboardMarkup(row_width=1)
    for slug, info in data.items():
        title = info.get('title', slug)
        markup.add(InlineKeyboardButton(text=f"✏️ تعديل: {title}", callback_data=f"setep_{slug}"))
    bot.reply_to(message, "🔢 اختر المسلسل لتعديل رقم آخر حلقة مسجلة:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('setep_'))
def process_setep_callback(call):
    if str(call.message.chat.id) != str(ADMIN_CHAT_ID):
        return bot.answer_callback_query(call.id)
    slug = call.data.split('setep_', 1)[1]
    msg = bot.send_message(call.message.chat.id, "🔢 أرسل رقم آخر حلقة صحيحة للمسلسل الآن:\n(مثال: لو عاوز البوت يبحث عن 88، اكتب هنا 87)")
    bot.register_next_step_handler(msg, save_new_ep, slug)
    bot.answer_callback_query(call.id)


def save_new_ep(message, slug):
    if not is_admin(message): return
    try:
        new_ep = int(get_text(message))
    except Exception:
        return bot.reply_to(message, "❌ يجب أن يكون رقماً. ابدأ من جديد بـ /setep")
    with data_lock:
        data = load_series_data()
        if slug in data:
            data[slug]['last_ep'] = new_ep
            data[slug]['status_text'] = "تم التعديل اليدوي، جاري الفحص..."
            fresh = collect_fingerprints(data[slug].get("laroza_url", ""))
            if fresh:
                data[slug]['laroza_seen_fps'] = fresh
            saved_fb = save_series_data(data)

            fb_note = "☁️ (تم تحديث Firebase)" if saved_fb else "⚠️ (تحديث محلي فقط)"
            bot.reply_to(message, f"✅ تم تعديل آخر حلقة للمسلسل لتكون {new_ep} بنجاح. {fb_note}\nالبوت الآن سيبحث عن الحلقة {new_ep + 1}.")
        else:
            bot.reply_to(message, "❌ المسلسل غير موجود.")


@bot.message_handler(commands=["del"])
def delete_item(message):
    if not is_admin(message): return
    data = load_series_data()
    if not data: return bot.reply_to(message, "📭 القائمة فارغة.")
    markup = InlineKeyboardMarkup(row_width=1)
    for slug, info in data.items():
        title = info.get('title', slug)
        markup.add(InlineKeyboardButton(text=f"❌ حذف: {title}", callback_data=f"del_{slug}"))
    bot.reply_to(message, "🗑 اختر المسلسل للحذف:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('del_'))
def process_delete_callback(call):
    if str(call.message.chat.id) != str(ADMIN_CHAT_ID):
        return bot.answer_callback_query(call.id)
    slug = call.data.split('del_', 1)[1]
    with data_lock:
        data = load_series_data()
        if slug in data:
            title = data[slug].get("title", slug)
            del data[slug]
            saved_fb = save_series_data(data)
            fb_note = "☁️ وتم الحذف من Firebase" if saved_fb else ""
            bot.answer_callback_query(call.id, f"✅ تم حذف {title}")
            try:
                bot.edit_message_text(f"✅ تم حذف مسلسل: {title} {fb_note}", call.message.chat.id, call.message.message_id)
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "❌ غير موجود.")


@bot.message_handler(commands=["backup"])
def backup_data(message):
    if not is_admin(message): return
    data = load_series_data()
    if not data:
        return bot.reply_to(message, "❌ لا توجد بيانات حالياً.")
    tmp = os.path.join(DATA_DIR, "backup_tmp.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(tmp, "rb") as f:
        bot.send_document(message.chat.id, f, caption="✅ <b>نسخة احتياطية (series.json)</b>", parse_mode="HTML")


@bot.message_handler(commands=["restore"])
def restore_data_step(message):
    if not is_admin(message): return
    msg = bot.reply_to(message, "📥 <b>أرسل لي ملف series.json أو انسخ محتواه كنص هنا:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_restore)


def process_restore(message):
    if not is_admin(message): return
    try:
        if message.document:
            file_info = bot.get_file(message.document.file_id)
            raw_data = bot.download_file(file_info.file_path).decode('utf-8')
        elif message.text:
            raw_data = message.text
        else:
            return bot.reply_to(message, "❌ يجب إرسال ملف أو نص.")

        parsed_data = json.loads(raw_data.strip())
        if not isinstance(parsed_data, dict):
            return bot.reply_to(message, "❌ الملف لازم يكون JSON object.")

        for slug in list(parsed_data.keys()):
            item = parsed_data[slug]
            if not isinstance(item, dict) or "series_id" not in item or "last_ep" not in item:
                return bot.reply_to(message, f"❌ بيانات غير صالحة عند: {slug}")
            item.pop("tracking", None)
            item.pop("qdrama_url", None)
            item["status_text"] = "تمت الاستعادة بنجاح، جاري الفحص..."

        with data_lock:
            saved_fb = save_series_data(parsed_data)

        fb_status = "☁️ <b>تم رفع الاستعادة لـ Firebase بنجاح!</b>" if saved_fb else "⚠️ تم الاستعادة محلياً فقط."
        bot.reply_to(message, f"✅ <b>تمت الاستعادة وتحديث هيكل البيانات بنجاح!</b>\n{fb_status}", parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ خطأ في الاستعادة: {e}")


def setup_commands_menu():
    commands = [
        BotCommand("add", "➕ إضافة مسلسل"),
        BotCommand("del", "🗑 حذف مسلسل"),
        BotCommand("setep", "✏️ تعديل رقم آخر حلقة"),
        BotCommand("status", "📊 تقرير الحالة الشامل"),
        BotCommand("testfb", "🧪 فحص اتصال Firebase"),
        BotCommand("setdomain", "🔒 تحديد دومين لاروزا يدوي"),
        BotCommand("cleardomain", "🔓 إلغاء الدومين اليدوي"),
        BotCommand("checkdomain", "🌐 فحص الدومين الآن"),
        BotCommand("backup", "💾 نسخة احتياطية"),
        BotCommand("restore", "📥 استعادة البيانات"),
        BotCommand("help", "❓ قايمة الأوامر"),
    ]
    try:
        bot.set_my_commands(commands, scope=BotCommandScopeChat(int(ADMIN_CHAT_ID)))
        log("تم تسجيل قايمة الأوامر")
    except Exception as e:
        log(f"[COMMANDS MENU ERROR] {e}")


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    setup_commands_menu()

    threading.Thread(target=run_scheduler, daemon=True).start()
    log("Bot is running in background thread with Firebase sync...")

    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            log(f"[POLLING ERROR] {e}")
            time.sleep(5)