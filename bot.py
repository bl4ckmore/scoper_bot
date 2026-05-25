"""
CS2 News Approval Bot for Telegram
- Scrapes news from multiple sources + @sl4mtv channel
- Sends each article to YOU privately for approval
- You edit the text and press Approve → posts to @cs2scoper
"""

import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import asyncio
import feedparser
import hashlib
import json
import logging
import re
import schedule
import time
from datetime import datetime
from pathlib import Path
import os
import requests
from bs4 import BeautifulSoup
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "5528680090"))
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@cs2scoper")

CHECK_INTERVAL_MINUTES = 15
POSTED_CACHE_FILE      = "posted_articles.json"
PENDING_FILE           = "pending_articles.json"
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)


# ── NEWS SOURCES ──────────────────────────────
SOURCES = [
    {
        "name": "BLAST",
        "type": "rss",
        "url": "https://blast.tv/feed",
        "emoji": "💥",
    },
    {
        "name": "ESL",
        "type": "rss",
        "url": "https://www.eslgaming.com/feed",
        "emoji": "🏆",
    },
    {
        "name": "Valve Steam",
        "type": "rss",
        "url": "https://store.steampowered.com/feeds/news/app/730/",
        "emoji": "🔧",
    },
    {
        "name": "sl4mtv",
        "type": "telegram",
        "url": "https://t.me/s/sl4mtv",
        "emoji": "📡",
    },
]


# ── CACHE ─────────────────────────────────────
def load_cache() -> set:
    if Path(POSTED_CACHE_FILE).exists():
        with open(POSTED_CACHE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_cache(cache: set):
    with open(POSTED_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(cache), f)

def make_id(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()

def load_pending() -> dict:
    if Path(PENDING_FILE).exists():
        with open(PENDING_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_pending(pending: dict):
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False)


# ── SCRAPERS ──────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

def scrape_telegram_channel(source: dict) -> list[dict]:
    """Scrape public Telegram channel web preview."""
    articles = []
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        messages = soup.select(".tgme_widget_message_text")
        links    = soup.select(".tgme_widget_message_wrap")

        for i, msg in enumerate(messages[:10]):
            text = msg.get_text(separator="\n", strip=True)
            if len(text) < 30:
                continue

            # Try to get message link
            url = source["url"]
            try:
                wrap = links[i]
                a = wrap.select_one("a.tgme_widget_message_date")
                if a:
                    url = a.get("href", source["url"])
            except:
                pass

            articles.append({
                "title": text[:100],
                "full_text": text,
                "url": url,
                "source": source,
            })
    except Exception as e:
        log.warning(f"Telegram scrape failed for {source['name']}: {e}")
    return articles

def fetch_rss(source: dict) -> list[dict]:
    articles = []
    try:
        feed = feedparser.parse(source["url"])
        for entry in feed.entries[:10]:
            title = entry.get("title", "").strip()
            url   = entry.get("link", "").strip()
            if title and url:
                articles.append({
                    "title": title,
                    "full_text": title,
                    "url": url,
                    "source": source,
                })
    except Exception as e:
        log.warning(f"RSS fetch failed for {source['name']}: {e}")
    return articles

def fetch_all_news() -> list[dict]:
    all_articles = []
    for source in SOURCES:
        if source["type"] == "telegram":
            all_articles.extend(scrape_telegram_channel(source))
        elif source["type"] == "rss":
            all_articles.extend(fetch_rss(source))
    return all_articles


# ── SEND FOR APPROVAL ─────────────────────────
async def send_for_approval(bot: Bot, article: dict, article_id: str):
    """Send article to admin with Edit + Approve + Skip buttons."""
    src      = article["source"]
    emoji    = src.get("emoji", "📰")
    text     = article.get("full_text", article["title"])
    url      = article["url"]

    preview = (
        f"📬 <b>New article from {src['name']}</b>\n\n"
        f"{emoji} {text}\n\n"
        f"🔗 {url}\n\n"
        f"<i>Edit the text below and press Approve, or Skip to ignore.</i>"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{article_id}"),
            InlineKeyboardButton("✅ Approve & Post", callback_data=f"approve:{article_id}"),
            InlineKeyboardButton("🗑 Skip", callback_data=f"skip:{article_id}"),
        ]
    ])

    # Save to pending
    pending = load_pending()
    pending[article_id] = {
        "text": text,
        "url": url,
        "source_name": src["name"],
        "emoji": emoji,
    }
    save_pending(pending)

    await bot.send_message(
        chat_id=ADMIN_ID,
        text=preview,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ── CALLBACK HANDLER ──────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()

    data       = query.data
    action, article_id = data.split(":", 1)
    pending    = load_pending()
    cache      = load_cache()

    if article_id not in pending:
        await query.edit_message_text("Article not found (already processed?)")
        return

    article = pending[article_id]

    if action == "edit":
        # Ask admin to send new text
        context.user_data["editing_id"] = article_id
        await query.edit_message_text(
            f"Send me your edited version of this article:\n\n"
            f"{article['text']}\n\n"
            f"<i>Just type and send your new text as a message.</i>",
            parse_mode=ParseMode.HTML,
        )

    elif action == "skip":
        cache.add(article_id)
        save_cache(cache)
        del pending[article_id]
        save_pending(pending)
        await query.edit_message_text(f"Skipped: {article['text'][:60]}...")

    elif action == "approve":
        # Post to channel
        now     = datetime.now().strftime("%d %b %Y · %H:%M")
        message = (
            f"{article['emoji']} {article['text']}\n\n"
            f"#CS2 #CounterStrike #CS2News"
        )
        try:
            bot = context.bot
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            cache.add(article_id)
            save_cache(cache)
            del pending[article_id]
            save_pending(pending)
            await query.edit_message_text(f"Posted to {CHANNEL_ID}!")
            log.info(f"Posted: {article['text'][:60]}...")
        except TelegramError as e:
            await query.edit_message_text(f"Error posting: {e}")
            log.error(f"Post error: {e}")


# ── HANDLE EDITED TEXT ────────────────────────
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends edited text — save it and show Approve button."""
    if update.effective_user.id != ADMIN_ID:
        return

    editing_id = context.user_data.get("editing_id")
    if not editing_id:
        await update.message.reply_text("No article being edited. Use the Edit button first.")
        return

    # Update pending article with new text
    pending = load_pending()
    if editing_id not in pending:
        await update.message.reply_text("Article not found.")
        return

    pending[editing_id]["text"] = update.message.text
    save_pending(pending)
    context.user_data.pop("editing_id", None)

    # Show approve/skip buttons
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve & Post", callback_data=f"approve:{editing_id}"),
            InlineKeyboardButton("🗑 Skip", callback_data=f"skip:{editing_id}"),
        ]
    ])

    await update.message.reply_text(
        f"Text updated! Ready to post:\n\n{update.message.text}\n\nPress Approve to publish.",
        reply_markup=keyboard,
    )


# ── CHECK CYCLE ───────────────────────────────
async def check_and_notify(app: Application):
    log.info("Checking for new CS2 news...")
    cache    = load_cache()
    articles = fetch_all_news()
    bot      = app.bot
    found    = 0

    for article in articles:
        article_id = make_id(article["url"] + article["title"])
        if article_id in cache:
            continue

        try:
            await send_for_approval(bot, article, article_id)
            cache.add(article_id)
            save_cache(cache)
            found += 1
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Error sending for approval: {e}")

    log.info(f"Sent {found} new articles for approval.")


# ── ENTRY POINT ───────────────────────────────
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("Please set your BOT_TOKEN in bot.py!")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    async def startup(app):
        await check_and_notify(app)
        # Schedule periodic checks
        async def periodic():
            while True:
                await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)
                await check_and_notify(app)
        asyncio.create_task(periodic())

    app.post_init = startup
    print("Bot started! Check your Telegram for pending articles.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()