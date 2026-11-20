import re
import os
import time
import json
import random
import asyncio
import logging

# Python 3.14 compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import aiofiles

from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram import Client, filters
from pyrogram.errors import (
    UserAlreadyParticipant,
    InviteHashExpired,
    InviteHashInvalid,
    PeerIdInvalid,
    InviteRequestSent,
    FloodWait,
)

from urllib.parse import urlparse

from config import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    ADMIN_LIMIT,
    ADMIN_IDS,
    DEFAULT_LIMIT,
    BOT_SESSION_NAME,
    SESSION_POOL_NAMES,
)

# @NOTHIMNARUTO

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = Client(
    BOT_SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=200,
)

# ---------------------------------------------------------
# Session pool — round-robin across N user accounts
# ---------------------------------------------------------
class SessionPool:
    def __init__(self, names):
        self.names = list(names)
        self.clients = {}
        self.index = 0
        self.lock = asyncio.Lock()
        self._started = False

    async def start_all(self):
        if self._started:
            return
        for name in self.names:
            c = Client(
                name,
                api_id=API_ID,
                api_hash=API_HASH,
                workers=200,
            )
            try:
                await c.start()
                self.clients[name] = c
                logger.info(f"Session pool: started {name}")
            except Exception as e:
                logger.error(f"Session pool: failed to start {name}: {e}")
        self._started = True
        if not self.clients:
            raise RuntimeError("Session pool is empty — no user clients started")

    async def get(self):
        async with self.lock:
            if not self.clients:
                raise RuntimeError("Session pool exhausted")
            names = list(self.clients.keys())
            name = names[self.index % len(names)]
            self.index += 1
            return self.clients[name], name

    async def evict(self, name, reason=""):
        async with self.lock:
            client = self.clients.pop(name, None)
            if client:
                logger.warning(f"Session pool: evicted {name} ({reason})")
                try:
                    await client.stop()
                except Exception:
                    pass

    def status(self):
        return {n: "up" for n in self.clients}


pool = SessionPool(SESSION_POOL_NAMES)

# ---------------------------------------------------------
# BIN metadata lookup
# ---------------------------------------------------------
BIN_DATA = {}
try:
    with open("bin_data.json", "r", encoding="utf-8") as _f:
        BIN_DATA = json.load(_f)
    logger.info(f"Loaded {len(BIN_DATA)} BIN records")
except FileNotFoundError:
    logger.warning("bin_data.json missing — BIN lookup will return defaults")
except Exception as e:
    logger.error(f"Failed to load bin_data.json: {e}")


def lookup_bin(card_number):
    """Try 8-digit, then 6-digit BIN. Returns dict with bank/country/type/brand."""
    if not card_number or not card_number.isdigit():
        return None
    for length in (8, 6):
        prefix = card_number[:length]
        if prefix in BIN_DATA:
            info = dict(BIN_DATA[prefix])
            info["bin"] = prefix
            return info
    # fallback: guess brand from first digit
    first = card_number[0]
    brand_guess = {
        "4": "Visa",
        "5": "Mastercard",
        "3": "Amex/JCB",
        "6": "Discover/UnionPay",
    }.get(first)
    return {
        "bin": card_number[:6],
        "bank": "Unknown",
        "country": "??",
        "type": "Unknown",
        "brand": brand_guess or "Unknown",
    }


def build_bin_summary(cards):
    """Cards are 'cc|mm|yy|cvv' strings. Returns a text block for captions."""
    if not cards:
        return ""
    country_counter = {}
    type_counter = {}
    brand_counter = {}
    for card in cards:
        cc = card.split("|", 1)[0]
        info = lookup_bin(cc)
        if not info:
            continue
        country_counter[info["country"]] = country_counter.get(info["country"], 0) + 1
        type_counter[info["type"]] = type_counter.get(info["type"], 0) + 1
        brand_counter[info["brand"]] = brand_counter.get(info["brand"], 0) + 1

    def top(counter, n=3):
        items = sorted(counter.items(), key=lambda x: x[1], reverse=True)[:n]
        total = sum(counter.values()) or 1
        return ", ".join(f"{k} {int(v * 100 / total)}%" for k, v in items)

    lines = []
    if country_counter:
        lines.append(f"🌍 <b>Countries:</b> <code>{top(country_counter)}</code>")
    if brand_counter:
        lines.append(f"💳 <b>Brands:</b> <code>{top(brand_counter)}</code>")
    if type_counter:
        lines.append(f"🧾 <b>Types:</b> <code>{top(type_counter)}</code>")
    return "\n".join(lines)

# ---------------------------------------------------------
# Cooldown + stats (in-memory)
# ---------------------------------------------------------
USER_COOLDOWN = {}
COOLDOWN_SEC = 30

STATS = {
    "cards_scraped": 0,
    "files_shipped": 0,
    "commands_run": 0,
    "started_at": time.time(),
}


def check_cooldown(user_id):
    if user_id is None:
        return True
    now = time.time()
    last = USER_COOLDOWN.get(user_id, 0)
    if now - last < COOLDOWN_SEC:
        return False
    USER_COOLDOWN[user_id] = now
    return True


def cooldown_remaining(user_id):
    if user_id is None:
        return 0
    return max(0, int(COOLDOWN_SEC - (time.time() - USER_COOLDOWN.get(user_id, 0))))

# ---------------------------------------------------------
# Card utilities
# ---------------------------------------------------------
CARD_PATTERN = re.compile(r'\d{16}\D*\d{2}\D*\d{2,4}\D*\d{3,4}')


def luhn_ok(card_number: str) -> bool:
    if not card_number.isdigit():
        return False
    digits = [int(d) for d in card_number][::-1]
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def normalize_card(text):
    text = re.sub(r'[\n\r\|\/]', ' ', text)
    numbers = [n for n in re.findall(r'\d+', text) if n.isdigit()]
    cc = mm = yy = cvv = ''

    for num in numbers:
        if not cc and 13 <= len(num) <= 19:
            cc = num
        elif not mm and len(num) <= 2 and 1 <= int(num) <= 12:
            mm = num.zfill(2)
        elif not yy and (len(num) == 2 or (len(num) == 4 and num.startswith('20'))):
            yy = num if len(num) == 4 else '20' + num
        elif not cvv and len(num) in (3, 4):
            cvv = num

    if cc and mm and yy and cvv:
        return f"{cc}|{mm}|{yy}|{cvv}"
    return None


def remove_duplicates(messages):
    unique_messages = list(dict.fromkeys(messages))
    duplicates_removed = len(messages) - len(unique_messages)
    logger.info(f"Removed {duplicates_removed} duplicates")
    return unique_messages, duplicates_removed

# ---------------------------------------------------------
# Progress ticker — edits the status message live
# ---------------------------------------------------------
class ProgressTicker:
    def __init__(self, client, chat_id, message_id, prefix="Scraping", every=50):
        self.client = client
        self.chat_id = chat_id
        self.message_id = message_id
        self.prefix = prefix
        self.every = every
        self.count = 0
        self.scanned = 0
        self.last_edit = 0.0
        self.min_interval = 2.0  # throttle: never edit more than once per 2s

    async def tick(self, count, scanned):
        self.count = count
        self.scanned = scanned
        if count % self.every != 0:
            return
        now = time.time()
        if now - self.last_edit < self.min_interval:
            return
        self.last_edit = now
        try:
            await self.client.edit_message_text(
                self.chat_id,
                self.message_id,
                f"<b>𝐒𝐜𝐫𝐚𝐩𝐢𝐧𝐠...</b>\n"
                f"📝 <code>{count}</code> cards | 🔍 scanned <code>{scanned}</code>",
                parse_mode=ParseMode.HTML,
            )
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            pass  # editing is cosmetic; never crash the scrape

# ---------------------------------------------------------
# Scraper core
# ---------------------------------------------------------
async def scrape_messages(user_client, chat_id, limit, start_number=None, bank_name=None,
                          ticker=None):
    messages = []
    count = 0
    scanned = 0
    max_scan = max(limit * 50, 1000)

    logger.info(f"Scrape start | chat={chat_id} limit={limit} bin={start_number} bank={bank_name}")

    try:
        async for message in user_client.search_messages(chat_id):
            scanned += 1
            if count >= limit or scanned >= max_scan:
                break

            text = message.text or message.caption
            if not text:
                continue

            if bank_name and bank_name.lower() not in text.lower():
                if ticker and scanned % 50 == 0:
                    await ticker.tick(count, scanned)
                continue

            for matched in CARD_PATTERN.findall(text):
                vals = re.findall(r'\d+', matched)
                if len(vals) != 4:
                    continue
                card_number, mo, year, cvv = vals
                year = year[-2:]

                if start_number and not card_number.startswith(start_number[:6]):
                    continue
                if not luhn_ok(card_number):
                    continue

                messages.append(f"{card_number}|{mo}|{year}|{cvv}")
                count += 1

                if ticker:
                    await ticker.tick(count, scanned)

                if count >= limit:
                    break
    except FloodWait as e:
        logger.warning(f"FloodWait {e.value}s during scrape")
        await asyncio.sleep(e.value)
    except Exception as e:
        logger.error(f"Scrape failure chat={chat_id}: {e}")

    STATS["cards_scraped"] += len(messages)
    logger.info(f"Scraped {len(messages)} | scanned {scanned} | chat={chat_id}")
    return messages[:limit]

# ---------------------------------------------------------
# Session helper (join + resolve)
# ---------------------------------------------------------
async def join_and_resolve(client, invite_link, notify_msg=None):
    try:
        await client.join_chat(invite_link)
        return await client.get_chat(invite_link)
    except UserAlreadyParticipant:
        try:
            return await client.get_chat(invite_link)
        except Exception as e:
            logger.error(f"Already participant but resolve failed: {e}")
            return None
    except InviteRequestSent:
        if notify_msg:
            try:
                await notify_msg.edit_text("<b>Join request sent ✅ — waiting for approval</b>")
            except Exception:
                pass
        return None
    except (InviteHashExpired, InviteHashInvalid, PeerIdInvalid) as e:
        logger.error(f"Join failed {invite_link}: {e}")
        if notify_msg:
            try:
                await notify_msg.edit_text("<b>❌ Invalid or expired invite link</b>")
            except Exception:
                pass
        return None

# ---------------------------------------------------------
# Result delivery
# ---------------------------------------------------------
async def get_user_link(message):
    if message.from_user is None:
        return '<a href="https://t.me/ScrapperxCleanerBot">Scrapper x Cleaner</a>'
    first = message.from_user.first_name or ""
    last = message.from_user.last_name or ""
    full = f"{first} {last}".strip() or "User"
    return f'<a href="tg://user?id={message.from_user.id}">{full}</a>'


async def send_results(client, message, unique_messages, duplicates_removed,
                       source_name, bin_filter=None, bank_filter=None):
    if not unique_messages:
        try:
            await message.delete()
        except Exception:
            pass
        await client.send_message(
            message.chat.id,
            "<b>❌ 𝐍𝐎 𝐂𝐑𝐄𝐃𝐈𝐓 𝐂𝐀𝐑𝐃𝐒 𝐅𝐎𝐔𝐍𝐃</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    file_name = f"x{len(unique_messages)}_{source_name.replace(' ', '_')[:32]}.txt"
    try:
        async with aiofiles.open(file_name, mode='w', encoding='utf-8') as f:
            await f.write("\n".join(unique_messages))

        user_link = await get_user_link(message)
        caption = (
            f"『⭐️ 𝐂𝐑𝐄𝐃𝐈𝐓 𝐂𝐀𝐑𝐃 𝐒𝐂𝐑𝐀𝐏𝐄𝐑 ⭐️』\n\n"
            f"✅ <b>𝐒𝐂𝐑𝐀𝐏𝐏𝐄𝐃 𝐒𝐔𝐂𝐂𝐄𝐒𝐒𝐅𝐔𝐋𝐋𝐘</b>\n"
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"🌐 <b>𝐒𝐎𝐔𝐑𝐂𝐄:</b> <code>{source_name}</code>\n"
            f"📝 <b>𝐀𝐌𝐎𝐔𝐍𝐓:</b> <code>{len(unique_messages)}</code>\n"
            f"🗑️ <b>𝐃𝐔𝐏𝐋𝐈𝐂𝐀𝐓𝐄𝐒 𝐑𝐄𝐌𝐎𝐕𝐄𝐃:</b> <code>{duplicates_removed}</code>\n"
        )
        if bin_filter:
            caption += f"🔍 <b>𝐁𝐈𝐍 𝐅𝐈𝐋𝐓𝐄𝐑:</b> <code>{bin_filter}</code>\n"
        if bank_filter:
            caption += f"🏦 <b>𝐁𝐀𝐍𝐊 𝐅𝐈𝐋𝐓𝐄𝐑:</b> <code>{bank_filter}</code>\n"

        # BIN metadata summary block
        bin_summary = build_bin_summary(unique_messages)
        if bin_summary:
            caption += f"▬▬▬▬▬▬▬▬▬▬▬▬▬\n{bin_summary}\n"

        caption += (
            f"▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
            f"👤 <b>𝐒𝐂𝐑𝐀𝐏𝐏𝐄𝐃 𝐁𝐘:</b> {user_link}\n"
        )

        chat_id = message.chat.id
        reply_id = getattr(message, 'replied_to_message_id', None)

        try:
            await message.delete()
        except Exception:
            pass

        send_kwargs = dict(
            chat_id=chat_id,
            document=file_name,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
        if reply_id:
            send_kwargs["reply_to_message_id"] = reply_id
        await client.send_document(**send_kwargs)
        STATS["files_shipped"] += 1
        logger.info(f"Shipped {len(unique_messages)} cards from {source_name}")
    except Exception as e:
        logger.error(f"Error sending results: {e}")
        try:
            await client.send_message(
                message.chat.id,
                f"<b>❌ 𝐄𝐑𝐑𝐎𝐑 𝐒𝐄𝐍𝐃𝐈𝐍𝐆 𝐅𝐈𝐋𝐄:</b> <code>{str(e)[:200]}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        try:
            if os.path.exists(file_name):
                os.remove(file_name)
        except OSError as e:
            logger.error(f"Error removing file: {e}")

# ---------------------------------------------------------
# Per-channel task (uses pool round-robin)
# ---------------------------------------------------------
async def scrape_messages_task(channel_identifier, limit, bot_client, message):
    user_client = None
    session_name = None
    try:
        user_client, session_name = await pool.get()

        chat = None
        if channel_identifier.startswith("https://t.me/+"):
            temporary_msg = await bot_client.send_message(
                message.chat.id,
                "<b>𝐂𝐡𝐞𝐜𝐤𝐢𝐧𝐠 𝐔𝐬𝐞𝐫𝐧𝐚𝐦𝐞...</b>",
                reply_to_message_id=message.id,
            )
            chat = await join_and_resolve(user_client, channel_identifier, temporary_msg)
            if not chat:
                return []
        else:
            parsed = urlparse(channel_identifier)
            username = parsed.path.lstrip('/') if parsed.scheme else channel_identifier
            chat = await user_client.get_chat(username)

        return await scrape_messages(user_client, chat.id, limit)
    except FloodWait as e:
        logger.warning(f"FloodWait on {session_name} for {e.value}s — evicting")
        if session_name:
            await pool.evict(session_name, f"FloodWait {e.value}s")
        await bot_client.send_message(
            message.chat.id,
            f"<b>⚠️ Session rate-limited on {channel_identifier}. Retry shortly.</b>",
            reply_to_message_id=message.id,
        )
        return []
    except Exception as e:
        await bot_client.send_message(
            message.chat.id,
            f"<b>𝐇𝐞𝐲 𝐁𝐫𝐨! 🥲 𝐈𝐧𝐜𝐨𝐫𝐫𝐞𝐜𝐭 𝐮𝐬𝐞𝐫𝐧𝐚𝐦𝐞 𝐟𝐨𝐫 {channel_identifier} ❌</b>",
            reply_to_message_id=message.id,
        )
        logger.error(f"Failed to scrape from {channel_identifier}: {e}")
        return []

# ---------------------------------------------------------
# Handlers
# ---------------------------------------------------------
def setup_scr_handler(app):
    @app.on_message(filters.command(["scr", "ccscr", "scrcc"],
                                    prefixes=["/", ".", ",", "!"])
                    & (filters.group | filters.private))
    async def scr_cmd(client, message):
        user_id = message.from_user.id if message.from_user else None
        STATS["commands_run"] += 1

        if not check_cooldown(user_id):
            rem = cooldown_remaining(user_id)
            await client.send_message(
                message.chat.id,
                f"<b>⏳ Slow down Bro. Try again in {rem}s.</b>",
                reply_to_message_id=message.id,
            )
            return

        args = message.text.split()[1:]
        if len(args) < 2:
            await client.send_message(
                message.chat.id,
                "<b>⚠️ 𝐏𝐫𝐨𝐯𝐢𝐝𝐞 𝐜𝐡𝐚𝐧𝐧𝐞𝐥 𝐮𝐬𝐞𝐫𝐧𝐚𝐦𝐞 𝐚𝐧𝐝 𝐚𝐦𝐨𝐮𝐧𝐭 ❌</b>",
                reply_to_message_id=message.id,
            )
            return

        channel_identifier = args[0]
        user_client = None
        session_name = None
        chat = None
        channel_name = ""

        try:
            user_client, session_name = await pool.get()
        except RuntimeError:
            await client.send_message(
                message.chat.id,
                "<b>❌ No user sessions available.</b>",
                reply_to_message_id=message.id,
            )
            return

        if channel_identifier.lstrip("-").isdigit():
            chat_id = int(channel_identifier)
            try:
                chat = await user_client.get_chat(chat_id)
                channel_name = chat.title
            except Exception as e:
                logger.error(f"Failed to fetch private channel: {e}")
                await client.send_message(
                    message.chat.id,
                    "<b>𝐇𝐞𝐲 𝐁𝐫𝐨! 🥲 𝐈𝐧𝐯𝐚𝐥𝐢𝐝 𝐜𝐡𝐚𝐭 𝐈𝐃 ❌</b>",
                    reply_to_message_id=message.id,
                )
                return
        elif channel_identifier.startswith("https://t.me/+"):
            temporary_msg = await client.send_message(
                message.chat.id,
                "<b>𝐂𝐡𝐞𝐜𝐤𝐢𝐧𝐠 𝐔𝐬𝐞𝐫𝐧𝐚𝐦𝐞...</b>",
                reply_to_message_id=message.id,
            )
            chat = await join_and_resolve(user_client, channel_identifier, temporary_msg)
            if not chat:
                return
            channel_name = chat.title
            try:
                await temporary_msg.delete()
            except Exception:
                pass
        else:
            if channel_identifier.startswith("https://t.me/"):
                channel_username = channel_identifier[13:]
            elif channel_identifier.startswith("t.me/"):
                channel_username = channel_identifier[5:]
            else:
                channel_username = channel_identifier
            try:
                chat = await user_client.get_chat(channel_username)
                channel_name = chat.title
            except Exception as e:
                logger.error(f"Failed to fetch public channel: {e}")
                await client.send_message(
                    message.chat.id,
                    "<b>𝐇𝐞𝐲 𝐁𝐫𝐨! 🥲 𝐈𝐧𝐜𝐨𝐫𝐫𝐞𝐜𝐭 𝐮𝐬𝐞𝐫𝐧𝐚𝐦𝐞 𝐨𝐫 𝐜𝐡𝐚𝐭 𝐈𝐃 ❌</b>",
                    reply_to_message_id=message.id,
                )
                return

        try:
            limit = int(args[1])
        except ValueError:
            await client.send_message(
                message.chat.id,
                "<b>⚠️ 𝐈𝐧𝐯𝐚𝐥𝐢𝐝 𝐥𝐢𝐦𝐢𝐭 ❌</b>",
                reply_to_message_id=message.id,
            )
            return

        start_number = None
        bank_name = None
        bin_filter = None
        if len(args) > 2:
            if args[2].isdigit():
                start_number = args[2]
                bin_filter = args[2][:6]
            else:
                bank_name = " ".join(args[2:])

        max_lim = ADMIN_LIMIT if user_id in ADMIN_IDS else DEFAULT_LIMIT
        if limit > max_lim:
            await client.send_message(
                message.chat.id,
                f"<b>𝐒𝐨𝐫𝐫𝐲 𝐁𝐫𝐨! 𝐌𝐚𝐱 𝐥𝐢𝐦𝐢𝐭 𝐢𝐬 {max_lim} ❌</b>",
                reply_to_message_id=message.id,
            )
            return

        temporary_msg = await client.send_message(
            message.chat.id,
            "<b>𝐒𝐜𝐫𝐚𝐩𝐢𝐧𝐠 𝐈𝐧 𝐏𝐫𝐨𝐠𝐫𝐞𝐬𝐬...</b>",
            reply_to_message_id=message.id,
        )
        temporary_msg.replied_to_message_id = message.id

        ticker = ProgressTicker(
            client=client,
            chat_id=message.chat.id,
            message_id=temporary_msg.id,
            prefix="Scraping",
            every=50,
        )

        scrapped = await scrape_messages(
            user_client, chat.id, limit,
            start_number=start_number,
            bank_name=bank_name,
            ticker=ticker,
        )
        unique_messages, duplicates_removed = remove_duplicates(scrapped)

        if not unique_messages:
            try:
                await temporary_msg.delete()
            except Exception:
                pass
            await client.send_message(
                message.chat.id,
                "<b>𝐒𝐨𝐫𝐫𝐲 𝐁𝐫𝐨 ❌ 𝐍𝐨 𝐂𝐫𝐞𝐝𝐢𝐭 𝐂𝐚𝐫𝐝 𝐅𝐨𝐮𝐧𝐝</b>",
                reply_to_message_id=message.id,
            )
        else:
            await send_results(client, temporary_msg, unique_messages,
                               duplicates_removed, channel_name,
                               bin_filter=bin_filter, bank_filter=bank_name)

    @app.on_message(filters.command(["mc", "multiscr", "mscr"],
                                    prefixes=["/", ".", ",", "!"])
                    & (filters.group | filters.private))
    async def mc_cmd(client, message):
        user_id = message.from_user.id if message.from_user else None
        STATS["commands_run"] += 1

        if not check_cooldown(user_id):
            rem = cooldown_remaining(user_id)
            await client.send_message(
                message.chat.id,
                f"<b>⏳ Slow down Bro. Try again in {rem}s.</b>",
                reply_to_message_id=message.id,
            )
            return

        args = message.text.split()[1:]
        if len(args) < 2:
            await client.send_message(
                message.chat.id,
                "<b>⚠️ 𝐏𝐫𝐨𝐯𝐢𝐝𝐞 𝐜𝐡𝐚𝐧𝐧𝐞𝐥𝐬 𝐚𝐧𝐝 𝐥𝐢𝐦𝐢𝐭 ❌</b>",
                reply_to_message_id=message.id,
            )
            return

        channel_identifiers = args[:-1]
        try:
            limit = int(args[-1])
        except ValueError:
            await client.send_message(
                message.chat.id,
                "<b>⚠️ 𝐈𝐧𝐯𝐚𝐥𝐢𝐝 𝐥𝐢𝐦𝐢𝐭 ❌</b>",
                reply_to_message_id=message.id,
            )
            return

        max_lim = ADMIN_LIMIT if user_id in ADMIN_IDS else DEFAULT_LIMIT
        if limit > max_lim:
            await client.send_message(
                message.chat.id,
                f"<b>𝐒𝐨𝐫𝐫𝐲 𝐁𝐫𝐨! 𝐌𝐚𝐱 𝐥𝐢𝐦𝐢𝐭 𝐢𝐬 {max_lim} ❌</b>",
                reply_to_message_id=message.id,
            )
            return

        temporary_msg = await client.send_message(
            message.chat.id,
            "<b>𝐒𝐜𝐫𝐚𝐩𝐢𝐧𝐠 𝐈𝐧 𝐏𝐫𝐨𝐠𝐫𝐞𝐬𝐬...</b>",
            reply_to_message_id=message.id,
        )
        temporary_msg.replied_to_message_id = message.id

        tasks = [
            scrape_messages_task(ch, limit, client, message)
            for ch in channel_identifiers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_messages = []
        for r in results:
            if isinstance(r, list):
                all_messages.extend(r)

        unique_messages, duplicates_removed = remove_duplicates(all_messages)
        unique_messages = unique_messages[:limit]

        if not unique_messages:
            try:
                await temporary_msg.delete()
            except Exception:
                pass
            await client.send_message(
                message.chat.id,
                "<b>𝐒𝐨𝐫𝐫𝐲 𝐁𝐫𝐨 ❌ 𝐍𝐨 𝐂𝐫𝐞𝐝𝐢𝐭 𝐂𝐚𝐫𝐝 𝐅𝐨𝐮𝐧𝐝</b>",
                reply_to_message_id=message.id,
            )
        else:
            await send_results(client, temporary_msg, unique_messages,
                               duplicates_removed, "Multiple Chats")


@app.on_message(filters.command("start", prefixes=["/", ".", ",", "!"])
                & (filters.group | filters.private))
async def start(client, message):
    buttons = [
        [
            InlineKeyboardButton("Update Channel", url="https://t.me/KamalxKiller"),
            InlineKeyboardButton("Dev 👨‍💻", user_id=5248903529),
        ]
    ]
    await client.send_message(
        message.chat.id,
        START_MESSAGE,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(buttons),
        reply_to_message_id=message.id,
    )


@app.on_message(filters.command(["stats"], prefixes=["/", ".", ",", "!"])
                & (filters.group | filters.private))
async def stats_cmd(client, message):
    uptime = int(time.time() - STATS["started_at"])
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    pool_status = ", ".join(pool.status().keys()) or "none"
    text = (
        f"『⭐️ 𝐁𝐎𝐓 𝐒𝐓𝐀𝐓𝐒 ⭐️』\n\n"
        f"⏱️ <b>𝐔𝐏𝐓𝐈𝐌𝐄:</b> <code>{h}h {m}m {s}s</code>\n"
        f"📝 <b>𝐂𝐀𝐑𝐃𝐒 𝐒𝐂𝐑𝐀𝐏𝐄𝐃:</b> <code>{STATS['cards_scraped']}</code>\n"
        f"📦 <b>𝐅𝐈𝐋𝐄𝐒 𝐒𝐇𝐈𝐏𝐏𝐄𝐃:</b> <code>{STATS['files_shipped']}</code>\n"
        f"⚡ <b>𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒 𝐑𝐔𝐍:</b> <code>{STATS['commands_run']}</code>\n"
        f"🔋 <b>𝐒𝐄𝐒𝐒𝐈𝐎𝐍𝐒:</b> <code>{pool_status}</code>\n"
    )
    await client.send_message(
        message.chat.id,
        text,
        parse_mode=ParseMode.HTML,
        reply_to_message_id=message.id,
    )


@app.on_message(filters.command(["clean"], prefixes=["/", ".", ",", "!"])
                & (filters.group | filters.private))
async def clean_cmd(client, message):
    if not message.reply_to_message or not message.reply_to_message.document:
        await message.reply_text(
            "<b>⚠️ 𝐏𝐥𝐞𝐚𝐬𝐞 𝐫𝐞𝐩𝐥𝐲 𝐭𝐨 𝐚 .𝐭𝐱𝐭 𝐟𝐢𝐥𝐞</b>"
        )
        return

    document = message.reply_to_message.document
    if not document.file_name.endswith('.txt'):
        await message.reply_text("<b>⚠️ 𝐏𝐥𝐞𝐚𝐬𝐞 𝐩𝐫𝐨𝐯𝐢𝐝𝐞 𝐚 .𝐭𝐱𝐭 𝐟𝐢𝐥𝐞</b>")
        return

    status_msg = await message.reply_text("<b>𝐏𝐫𝐨𝐜𝐞𝐬𝐬𝐢𝐧𝐠 𝐟𝐢𝐥𝐞...</b>")

    file_path = None
    output_filename = None
    try:
        file_path = await client.download_media(document)

        async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = await f.read()

        cleaned_cards = []
        for line in content.strip().split('\n'):
            if line.strip():
                normalized = normalize_card(line)
                if normalized:
                    cleaned_cards.append(normalized)

        unique_cards, duplicates_removed = remove_duplicates(cleaned_cards)

        if unique_cards:
            output_filename = f"cleaned_x{len(unique_cards)}.txt"
            async with aiofiles.open(output_filename, 'w', encoding='utf-8') as f:
                await f.write('\n'.join(unique_cards))

            user_link = await get_user_link(message)
            caption = (
                f"『⭐️ 𝐂𝐑𝐄𝐃𝐈𝐓 𝐂𝐀𝐑𝐃 𝐒𝐂𝐑𝐀𝐏𝐄𝐑 ⭐️』\n\n"
                f"✅ <b>𝐂𝐋𝐄𝐀𝐍𝐄𝐃 𝐒𝐔𝐂𝐂𝐄𝐒𝐒𝐅𝐔𝐋𝐋𝐘</b>\n"
                f"▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                f"🌐 <b>𝐒𝐎𝐔𝐑𝐂𝐄:</b> <code>File Cleaning 🧹</code>\n"
                f"📝 <b>𝐀𝐌𝐎𝐔𝐍𝐓:</b> <code>{len(unique_cards)}</code>\n"
                f"🗑️ <b>𝐃𝐔𝐏𝐋𝐈𝐂𝐀𝐓𝐄𝐒 𝐑𝐄𝐌𝐎𝐕𝐄𝐃:</b> <code>{duplicates_removed}</code>\n"
                f"▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
                f"👤 <b>𝐂𝐋𝐄𝐀𝐍𝐄𝐃 𝐁𝐘:</b> {user_link}\n"
            )

            try:
                await status_msg.delete()
            except Exception:
                pass

            await client.send_document(
                message.chat.id,
                output_filename,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=message.id,
            )
            STATS["files_shipped"] += 1
        else:
            try:
                await status_msg.delete()
            except Exception:
                pass
            await client.send_message(
                message.chat.id,
                "<b>❌ 𝐍𝐎 𝐕𝐀𝐋𝐈𝐃 𝐂𝐑𝐄𝐃𝐈𝐓 𝐂𝐀𝐑𝐃𝐒 𝐅𝐎𝐔𝐍𝐃</b>",
                reply_to_message_id=message.id,
            )

    except Exception as e:
        logger.error(f"Error in clean command: {e}")
        try:
            await status_msg.delete()
        except Exception:
            pass
        await client.send_message(
            message.chat.id,
            f"<b>❌ 𝐄𝐑𝐑𝐎𝐑:</b> <code>{str(e)[:200]}</code>",
            reply_to_message_id=message.id,
        )
    finally:
        for p in (file_path, output_filename):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


async def main():
    await pool.start_all()
    setup_scr_handler(app)
    await app.start()
    logger.info("Bot online — session pool ready")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())