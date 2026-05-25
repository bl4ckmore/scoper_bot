"""
CS2 News Approval Bot — No external telegram library, works on any Python version!
Uses Telegram HTTP API directly.
"""

import asyncio
import feedparser
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@cs2scoper")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "5528680090"))

CHECK_INTERVAL_SECONDS = 15 * 60  # 15 minutes
POSTED_CACHE_FILE      = "posted_articles.json"
PENDING_FILE           = "pending_articles.json"

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# ── SOURCES ───────────────────────────────────
SOURCES = [
    {"name": "BLAST",       "type": "rss",      "url": "https://blast.tv/feed",                                    "emoji": "💥"},
    {"name": "ESL",         "type": "rss",      "url": "https://www.eslgaming.com/feed",                           "emoji": "🏆"},
    {"name": "Valve Steam", "type": "rss",      "url": "https://store.steampowered.com/feeds/news/app/730/",       "emoji": "🔧"},
    {"name": "sl4mtv",      "type": "telegram", "url": "https://t.me/s/sl4mtv",                                    "emoji": "📡"},
]


# ── CACHE ─────────────────────────────────────
def load_json(path, default):
    if Path(path).exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def make_id(text):
    return hashlib.md5(text.encode()).hexdigest()


# ── TELEGRAM API HELPERS ──────────────────────
def tg(method, **kwargs):
    try:
        r = requests.post(f"{API}/{method}", json=kwargs, timeout=15)
        return r.json()
    except Exception as e:
        log.error(f"Telegram API error: {e}")
        return {}

def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", **payload)

def edit_message(chat_id, message_id, text):
    return tg("editMessageText", chat_id=chat_id, message_id=message_id, text=text)

def answer_callback(callback_query_id, text=""):
    return tg("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

def get_updates(offset=None):
    params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
    if offset:
        params["offset"] = offset
    return tg("getUpdates", **params)


# ── SCRAPERS ──────────────────────────────────
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def scrape_telegram_channel(source):
    articles = []
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        messages = soup.select(".tgme_widget_message_text")
        links    = soup.select(".tgme_widget_message_wrap")
        for i, msg in enumerate(messages[:10]):
            text = msg.get_text(separator="\n", strip=True)
            if len(text) < 30:
                continue
            url = source["url"]
            try:
                a = links[i].select_one("a.tgme_widget_message_date")
                if a:
                    url = a.get("href", url)
            except:
                pass
            articles.append({"title": text[:100], "full_text": text, "url": url, "source": source})
    except Exception as e:
        log.warning(f"Telegram scrape failed: {e}")
    return articles

def fetch_rss(source):
    articles = []
    try:
        feed = feedparser.parse(source["url"])
        for entry in feed.entries[:10]:
            title = entry.get("title", "").strip()
            url   = entry.get("link", "").strip()
            if title and url:
                articles.append({"title": title, "full_text": title, "url": url, "source": source})
    except Exception as e:
        log.warning(f"RSS failed for {source['name']}: {e}")
    return articles

def fetch_all_news():
    all_articles = []
    for source in SOURCES:
        if source["type"] == "telegram":
            all_articles.extend(scrape_telegram_channel(source))
        elif source["type"] == "rss":
            all_articles.extend(fetch_rss(source))
    return all_articles


# ── SEND FOR APPROVAL ─────────────────────────
def send_for_approval(article, article_id):
    src     = article["source"]
    text    = article.get("full_text", article["title"])
    url     = article["url"]

    preview = (
        f"📬 <b>New from {src['name']}</b>\n\n"
        f"{src['emoji']} {text}\n\n"
        f"🔗 {url}"
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": "✏️ Edit",           "callback_data": f"edit:{article_id}"},
            {"text": "✅ Approve & Post", "callback_data": f"approve:{article_id}"},
            {"text": "🗑 Skip",           "callback_data": f"skip:{article_id}"},
        ]]
    }

    # Save to pending
    pending = load_json(PENDING_FILE, {})
    pending[article_id] = {
        "text": text,
        "url": url,
        "source_name": src["name"],
        "emoji": src["emoji"],
    }
    save_json(PENDING_FILE, pending)

    send_message(ADMIN_ID, preview, reply_markup=keyboard)


# ── HANDLE UPDATES ────────────────────────────
editing_state = {}  # user_id -> article_id being edited

def handle_callback(callback_query):
    cq_id      = callback_query["id"]
    data       = callback_query.get("data", "")
    msg        = callback_query.get("message", {})
    message_id = msg.get("message_id")
    user_id    = callback_query["from"]["id"]

    if ":" not in data:
        return

    action, article_id = data.split(":", 1)
    pending = load_json(PENDING_FILE, {})
    cache   = set(load_json(POSTED_CACHE_FILE, []))

    if article_id not in pending:
        answer_callback(cq_id, "Already processed!")
        return

    article = pending[article_id]
    answer_callback(cq_id)

    if action == "skip":
        cache.add(article_id)
        save_json(POSTED_CACHE_FILE, list(cache))
        del pending[article_id]
        save_json(PENDING_FILE, pending)
        edit_message(ADMIN_ID, message_id, f"Skipped.")

    elif action == "edit":
        editing_state[user_id] = article_id
        send_message(
            ADMIN_ID,
            f"Send me your edited version:\n\n{article['text']}",
        )

    elif action == "approve":
        text    = article["text"]
        emoji   = article["emoji"]
        message = f"{emoji} {text}\n\n#CS2 #CounterStrike #CS2News"
        result  = send_message(CHANNEL_ID, message)
        if result.get("ok"):
            cache.add(article_id)
            save_json(POSTED_CACHE_FILE, list(cache))
            del pending[article_id]
            save_json(PENDING_FILE, pending)
            edit_message(ADMIN_ID, message_id, f"Posted to {CHANNEL_ID}!")
            log.info(f"Posted: {text[:60]}...")
        else:
            edit_message(ADMIN_ID, message_id, f"Error: {result}")

def handle_message(message):
    user_id = message["from"]["id"]
    text    = message.get("text", "")

    if user_id != ADMIN_ID:
        return

    if user_id in editing_state:
        article_id = editing_state.pop(user_id)
        pending    = load_json(PENDING_FILE, {})

        if article_id not in pending:
            send_message(ADMIN_ID, "Article not found.")
            return

        pending[article_id]["text"] = text
        save_json(PENDING_FILE, pending)

        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Approve & Post", "callback_data": f"approve:{article_id}"},
                {"text": "🗑 Skip",           "callback_data": f"skip:{article_id}"},
            ]]
        }
        send_message(ADMIN_ID, f"Text updated! Ready to post:\n\n{text}", reply_markup=keyboard)


# ── MAIN LOOP ─────────────────────────────────
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("Set BOT_TOKEN environment variable first!")
        return

    log.info("CS2 Scoper Bot started!")
    offset         = None
    last_check     = 0

    while True:
        # Check for new news
        now = time.time()
        if now - last_check >= CHECK_INTERVAL_SECONDS:
            log.info("Checking for new articles...")
            cache    = set(load_json(POSTED_CACHE_FILE, []))
            articles = fetch_all_news()
            found    = 0
            for article in articles:
                article_id = make_id(article["url"] + article["title"])
                if article_id not in cache:
                    send_for_approval(article, article_id)
                    cache.add(article_id)
                    save_json(POSTED_CACHE_FILE, list(cache))
                    found += 1
                    time.sleep(1)
            log.info(f"Sent {found} new articles for approval.")
            last_check = now

        # Poll for button presses / messages
        try:
            result = get_updates(offset)
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                if "callback_query" in update:
                    handle_callback(update["callback_query"])
                elif "message" in update:
                    handle_message(update["message"])
        except Exception as e:
            log.error(f"Polling error: {e}")

        time.sleep(2)

if __name__ == "__main__":
    main()