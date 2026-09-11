"""
Telegram Group Guard

التوكن ومعرف المالك يقرآن من متغيرات البيئة:
    BOT_TOKEN=...
    ADMIN_ID=...

شغّل:
    python admin_id_token_bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import Iterable, Optional

from dotenv import load_dotenv
from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot_data.sqlite3").strip()
OWNER_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_ID", "").split(",")
    if value.strip().lstrip("-").isdigit()
}
DEFAULT_WARN_LIMIT = int(os.getenv("WARN_LIMIT", "3"))
DEFAULT_FLOOD_COUNT = int(os.getenv("FLOOD_COUNT", "5"))
DEFAULT_FLOOD_WINDOW = int(os.getenv("FLOOD_WINDOW", "10"))
DEFAULT_FLOOD_MUTE_MINUTES = int(os.getenv("FLOOD_MUTE_MINUTES", "10"))
DEFAULT_CAPTCHA_TIMEOUT = int(os.getenv("CAPTCHA_TIMEOUT_MINUTES", "5"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger("telegram-group-guard")

ROLE_LEVELS = {"member": 0, "moderator": 1, "admin": 2, "senior_admin": 3, "owner": 4}
ROLE_LABELS = {
    "member": "عضو",
    "moderator": "مشرف مساعد",
    "admin": "أدمن",
    "senior_admin": "أدمن رئيسي",
    "owner": "مالك",
}
URL_RE = re.compile(r"(https?://|t\.me/|telegram\.me/|www\.)", re.IGNORECASE)
DURATION_RE = re.compile(r"^(\d+)([mhdw])$", re.IGNORECASE)


@dataclass
class Target:
    user_id: int
    display_name: str
    username: Optional[str] = None


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def duration_seconds(value: str) -> Optional[int]:
    match = DURATION_RE.fullmatch(value.strip())
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    multiplier = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return amount * multiplier


class Database:
    def __init__(self, path: str):
        self.path = path
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with closing(self.connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    chat_id INTEGER PRIMARY KEY,
                    warn_limit INTEGER NOT NULL DEFAULT 3,
                    warn_action TEXT NOT NULL DEFAULT 'mute',
                    flood_count INTEGER NOT NULL DEFAULT 5,
                    flood_window INTEGER NOT NULL DEFAULT 10,
                    flood_action TEXT NOT NULL DEFAULT 'mute',
                    flood_mute_minutes INTEGER NOT NULL DEFAULT 10,
                    captcha_enabled INTEGER NOT NULL DEFAULT 0,
                    captcha_timeout INTEGER NOT NULL DEFAULT 5,
                    lockdown INTEGER NOT NULL DEFAULT 0,
                    welcome TEXT NOT NULL DEFAULT '',
                    rules TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS admins (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    admin_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS punishments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    until_ts INTEGER,
                    reason TEXT NOT NULL,
                    admin_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS banned_words (
                    chat_id INTEGER NOT NULL,
                    word TEXT NOT NULL,
                    PRIMARY KEY (chat_id, word)
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    target_id INTEGER,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    admin_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            db.commit()

    def ensure_chat(self, chat_id: int) -> None:
        with closing(self.connect()) as db:
            db.execute(
                "INSERT OR IGNORE INTO settings(chat_id) VALUES (?)",
                (chat_id,),
            )
            db.commit()

    def settings(self, chat_id: int) -> sqlite3.Row:
        self.ensure_chat(chat_id)
        with closing(self.connect()) as db:
            return db.execute(
                "SELECT * FROM settings WHERE chat_id = ?", (chat_id,)
            ).fetchone()

    def set_setting(self, chat_id: int, key: str, value) -> None:
        self.ensure_chat(chat_id)
        allowed = {
            "warn_limit",
            "flood_count",
            "flood_window",
            "flood_action",
            "flood_mute_minutes",
            "captcha_enabled",
            "captcha_timeout",
            "lockdown",
            "welcome",
            "rules",
        }
        if key not in allowed:
            raise ValueError("Unsupported setting")
        with closing(self.connect()) as db:
            db.execute(f"UPDATE settings SET {key} = ? WHERE chat_id = ?", (value, chat_id))
            db.commit()

    def role(self, chat_id: int, user_id: int) -> str:
        if user_id in OWNER_IDS:
            return "owner"
        self.ensure_chat(chat_id)
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT role FROM admins WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
        return row["role"] if row else "member"

    def set_role(self, chat_id: int, user_id: int, role: str) -> None:
        if role == "member":
            self.remove_role(chat_id, user_id)
            return
        with closing(self.connect()) as db:
            db.execute(
                "INSERT INTO admins(chat_id, user_id, role) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET role = excluded.role",
                (chat_id, user_id, role),
            )
            db.commit()

    def remove_role(self, chat_id: int, user_id: int) -> None:
        with closing(self.connect()) as db:
            db.execute(
                "DELETE FROM admins WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            db.commit()

    def admins(self, chat_id: int) -> list[sqlite3.Row]:
        self.ensure_chat(chat_id)
        with closing(self.connect()) as db:
            return list(
                db.execute(
                    "SELECT user_id, role FROM admins WHERE chat_id = ? ORDER BY role DESC",
                    (chat_id,),
                )
            )

    def add_warning(self, chat_id: int, user_id: int, reason: str, admin_id: int) -> int:
        with closing(self.connect()) as db:
            db.execute(
                "INSERT INTO warnings(chat_id,user_id,reason,admin_id,created_at) VALUES (?,?,?,?,?)",
                (chat_id, user_id, reason, admin_id, utc_now()),
            )
            db.commit()
            return db.execute(
                "SELECT COUNT(*) AS count FROM warnings WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()["count"]

    def remove_last_warning(self, chat_id: int, user_id: int) -> bool:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT id FROM warnings WHERE chat_id = ? AND user_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (chat_id, user_id),
            ).fetchone()
            if not row:
                return False
            db.execute("DELETE FROM warnings WHERE id = ?", (row["id"],))
            db.commit()
            return True

    def warning_rows(self, chat_id: int, user_id: int) -> list[sqlite3.Row]:
        with closing(self.connect()) as db:
            return list(
                db.execute(
                    "SELECT reason, created_at FROM warnings WHERE chat_id = ? AND user_id = ? "
                    "ORDER BY id DESC",
                    (chat_id, user_id),
                )
            )

    def add_word(self, chat_id: int, word: str) -> None:
        with closing(self.connect()) as db:
            db.execute(
                "INSERT OR IGNORE INTO banned_words(chat_id, word) VALUES (?, ?)",
                (chat_id, word.casefold()),
            )
            db.commit()

    def remove_word(self, chat_id: int, word: str) -> None:
        with closing(self.connect()) as db:
            db.execute(
                "DELETE FROM banned_words WHERE chat_id = ? AND word = ?",
                (chat_id, word.casefold()),
            )
            db.commit()

    def words(self, chat_id: int) -> list[str]:
        with closing(self.connect()) as db:
            return [
                row["word"]
                for row in db.execute(
                    "SELECT word FROM banned_words WHERE chat_id = ? ORDER BY word",
                    (chat_id,),
                )
            ]

    def log(self, chat_id: int, target_id: Optional[int], action: str, reason: str, admin_id: int) -> None:
        with closing(self.connect()) as db:
            db.execute(
                "INSERT INTO audit_log(chat_id,target_id,action,reason,admin_id,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (chat_id, target_id, action, reason, admin_id, utc_now()),
            )
            db.commit()

    def recent_logs(self, chat_id: int, limit: int = 20) -> list[sqlite3.Row]:
        with closing(self.connect()) as db:
            return list(
                db.execute(
                    "SELECT * FROM audit_log WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                    (chat_id, limit),
                )
            )

    def active_punishments(self, now: Optional[int] = None) -> list[sqlite3.Row]:
        now = now or int(time.time())
        with closing(self.connect()) as db:
            return list(
                db.execute(
                    "SELECT * FROM punishments WHERE until_ts IS NOT NULL AND until_ts <= ?",
                    (now,),
                )
            )

    def save_punishment(
        self, chat_id: int, user_id: int, action: str, until_ts: Optional[int], reason: str, admin_id: int
    ) -> None:
        with closing(self.connect()) as db:
            db.execute(
                "INSERT INTO punishments(chat_id,user_id,action,until_ts,reason,admin_id,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (chat_id, user_id, action, until_ts, reason, admin_id, utc_now()),
            )
            db.commit()

    def delete_punishment(self, punishment_id: int) -> None:
        with closing(self.connect()) as db:
            db.execute("DELETE FROM punishments WHERE id = ?", (punishment_id,))
            db.commit()


db = Database(DATABASE_PATH)
flood_tracker: defaultdict[tuple[int, int], deque[float]] = defaultdict(deque)
captcha_users: dict[tuple[int, int], int] = {}


def command_chat(update: Update) -> Optional[int]:
    return update.effective_chat.id if update.effective_chat else None


def is_group(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type in {ChatType.GROUP, ChatType.SUPERGROUP})


async def require_group(update: Update) -> bool:
    if not is_group(update):
        if update.effective_message:
            await update.effective_message.reply_text("هذا الأمر يعمل داخل المجموعات فقط.")
        return False
    return True


async def require_role(update: Update, minimum: str = "moderator") -> bool:
    if not await require_group(update):
        return False
    chat_id = command_chat(update)
    user = update.effective_user
    if not user or ROLE_LEVELS[db.role(chat_id, user.id)] < ROLE_LEVELS[minimum]:
        await update.effective_message.reply_text("ما عندك صلاحية لتنفيذ هذا الأمر.")
        return False
    return True


def actor_role(update: Update) -> str:
    return db.role(update.effective_chat.id, update.effective_user.id)


async def resolve_target(update: Update, token: Optional[str] = None) -> Optional[Target]:
    message = update.effective_message
    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        return Target(user.id, user.full_name, user.username)
    if not token:
        return None
    token = token.strip().lstrip("@")
    if token.lstrip("-").isdigit():
        user_id = int(token)
        try:
            member = await update.effective_chat.get_member(user_id)
            return Target(user_id, member.user.full_name, member.user.username)
        except Exception:
            return Target(user_id, token)
    # Telegram Bot API لا يوفر بحثًا عامًا عن العضو بواسطة @username.
    # لذلك يدعم البوت الرد على رسالة العضو أو استخدام المعرف الرقمي.
    return None


async def can_act_on(update: Update, target: Target) -> bool:
    actor = update.effective_user
    actor_level = ROLE_LEVELS[db.role(update.effective_chat.id, actor.id)]
    target_level = ROLE_LEVELS[db.role(update.effective_chat.id, target.user_id)]
    if target.user_id == actor.id or target_level >= actor_level or target_level >= ROLE_LEVELS["owner"]:
        await update.effective_message.reply_text("لا يمكن تنفيذ هذا الإجراء على عضو أعلى منك أو على المالك.")
        return False
    return True


def command_args(update: Update) -> list[str]:
    return list(update.effective_message.text.split()[1:]) if update.effective_message else []


def reason_from(args: Iterable[str], start: int = 0) -> str:
    value = " ".join(list(args)[start:]).strip()
    return value or "سبب غير محدد"


async def apply_restriction(
    update: Update,
    target: Target,
    action: str,
    reason: str,
    until_ts: Optional[int] = None,
) -> bool:
    chat = update.effective_chat
    admin_id = update.effective_user.id
    try:
        if action == "kick":
            await chat.ban_member(target.user_id)
            await chat.unban_member(target.user_id)
        elif action == "ban":
            await chat.ban_member(target.user_id)
        elif action == "tban":
            await chat.ban_member(target.user_id, until_date=until_ts)
        elif action == "mute":
            permissions = ChatPermissions(can_send_messages=False)
            await chat.restrict_member(target.user_id, permissions=permissions, until_date=until_ts)
        else:
            raise ValueError(action)
    except Exception as exc:
        LOGGER.warning("Could not apply %s to %s: %s", action, target.user_id, exc)
        await update.effective_message.reply_text(
            "تعذر تنفيذ الإجراء. تأكد أن البوت مشرف ولديه الصلاحيات المطلوبة."
        )
        return False
    db.save_punishment(chat.id, target.user_id, action, until_ts, reason, admin_id)
    db.log(chat.id, target.user_id, action, reason, admin_id)
    duration = ""
    if until_ts:
        duration = f"\nالانتهاء: {datetime.fromtimestamp(until_ts, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    await update.effective_message.reply_text(
        f"تم تنفيذ الإجراء.\nالعضو: {escape(target.display_name)}\n"
        f"العقوبة: {action}{duration}\nالسبب: {escape(reason)}\n"
        f"المنفذ: {escape(update.effective_user.full_name)}",
        parse_mode=ParseMode.HTML,
    )
    return True


async def punishment_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    action = update.effective_message.text.split()[0].lstrip("/").split("@")[0].lower()
    args = command_args(update)
    if action == "tban" and args and args[0].lower() == "list":
        if not await require_role(update, "moderator"):
            return
        rows = db.active_punishments()
        rows = [row for row in rows if row["chat_id"] == update.effective_chat.id and row["action"] == "tban"]
        if not rows:
            await update.effective_message.reply_text("لا توجد عقوبات مؤقتة مسجلة.")
            return
        await update.effective_message.reply_text(
            "\n".join(
                f"{row['user_id']} — حتى {datetime.fromtimestamp(row['until_ts'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                for row in rows
            )
        )
        return
    minimum = "admin" if action in {"ban", "unban"} else "moderator"
    if not await require_role(update, minimum):
        return
    target = await resolve_target(update, args[0] if args else None)
    if not target:
        await update.effective_message.reply_text("استخدم الأمر بالرد على العضو أو بذكر المعرف.")
        return
    if not await can_act_on(update, target):
        return
    reason_start = 1
    until_ts = None
    if action == "tban":
        if len(args) < 2 or not duration_seconds(args[1]):
            await update.effective_message.reply_text("الصيغة: /tban @user 30m السبب")
            return
        until_ts = int(time.time()) + duration_seconds(args[1])
        reason_start = 2
    elif action == "mute":
        if len(args) < 2 or not duration_seconds(args[1]):
            await update.effective_message.reply_text("الصيغة: /mute @user 30m")
            return
        until_ts = int(time.time()) + duration_seconds(args[1])
        reason_start = 2
    reason = reason_from(args, reason_start)
    await apply_restriction(update, target, action, reason, until_ts)


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "admin"):
        return
    args = command_args(update)
    if not args or not args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text("الصيغة: /unban user_id")
        return
    user_id = int(args[0])
    try:
        await update.effective_chat.unban_member(user_id)
        db.log(update.effective_chat.id, user_id, "unban", "رفع الحظر يدويًا", update.effective_user.id)
        await update.effective_message.reply_text("تم رفع الحظر.")
    except Exception:
        await update.effective_message.reply_text("تعذر رفع الحظر. تأكد من معرف العضو وصلاحيات البوت.")


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "moderator"):
        return
    args = command_args(update)
    target = await resolve_target(update, args[0] if args else None)
    if not target or not await can_act_on(update, target):
        await update.effective_message.reply_text("استخدم الأمر بالرد على العضو أو بذكر المعرف.")
        return
    try:
        await update.effective_chat.restrict_member(
            target.user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        db.log(update.effective_chat.id, target.user_id, "unmute", "رفع الكتم", update.effective_user.id)
        await update.effective_message.reply_text("تم رفع الكتم.")
    except Exception:
        await update.effective_message.reply_text("تعذر رفع الكتم.")


async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "moderator"):
        return
    args = command_args(update)
    target = await resolve_target(update, args[0] if args else None)
    if not target:
        await update.effective_message.reply_text("استخدم الأمر بالرد على العضو أو بذكر المعرف.")
        return
    if not await can_act_on(update, target):
        return
    reason = reason_from(args, 1)
    count = db.add_warning(update.effective_chat.id, target.user_id, reason, update.effective_user.id)
    db.log(update.effective_chat.id, target.user_id, "warn", reason, update.effective_user.id)
    limit = db.settings(update.effective_chat.id)["warn_limit"]
    await update.effective_message.reply_text(
        f"تم تحذير {escape(target.display_name)}.\nالسبب: {escape(reason)}\n"
        f"عدد التحذيرات: {count}/{limit}",
        parse_mode=ParseMode.HTML,
    )
    if count >= limit:
        warning_action = db.settings(update.effective_chat.id)["warn_action"]
        if warning_action == "kick":
            action, expiry = "kick", None
        elif warning_action == "ban":
            action, expiry = "ban", None
        elif warning_action == "tban":
            action, expiry = "tban", int(time.time()) + 7 * 86400
        else:
            action, expiry = "mute", int(time.time()) + 24 * 3600
        await apply_restriction(
            update,
            target,
            action,
            "تجاوز حد التحذيرات",
            expiry,
        )


async def warn_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "senior_admin"):
        return
    action = update.effective_message.text.split()[0].lstrip("/").lower()
    args = command_args(update)
    if action == "setwarnlimit":
        if not args or not args[0].isdigit() or int(args[0]) < 1:
            await update.effective_message.reply_text("الصيغة: /setwarnlimit عدد")
            return
        db.set_setting(update.effective_chat.id, "warn_limit", int(args[0]))
        await update.effective_message.reply_text("تم تحديث حد التحذيرات.")
        return
    if not args or args[0].lower() not in {"kick", "ban", "tban", "mute"}:
        await update.effective_message.reply_text("الصيغة: /setwarnaction عدد الإجراء")
        return
    db.set_setting(update.effective_chat.id, "warn_action", args[0].lower())
    await update.effective_message.reply_text("تم تحديث إجراء تجاوز التحذيرات.")


async def warns_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_group(update):
        return
    args = command_args(update)
    target = await resolve_target(update, args[0] if args else None)
    if not target:
        await update.effective_message.reply_text("استخدم /warns بالرد على رسالة العضو أو بذكر المعرف.")
        return
    rows = db.warning_rows(update.effective_chat.id, target.user_id)
    if not rows:
        await update.effective_message.reply_text("لا توجد تحذيرات مسجلة.")
        return
    text = "\n".join(f"{index}. {row['reason']} — {row['created_at']}" for index, row in enumerate(rows, 1))
    await update.effective_message.reply_text(f"تحذيرات {target.display_name} ({len(rows)}):\n{text}")


async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "moderator"):
        return
    args = command_args(update)
    target = await resolve_target(update, args[0] if args else None)
    if not target or not await can_act_on(update, target):
        await update.effective_message.reply_text("استخدم الأمر بالرد على العضو أو بذكر المعرف.")
        return
    if db.remove_last_warning(update.effective_chat.id, target.user_id):
        db.log(update.effective_chat.id, target.user_id, "unwarn", "إزالة آخر تحذير", update.effective_user.id)
        await update.effective_message.reply_text("تمت إزالة آخر تحذير.")
    else:
        await update.effective_message.reply_text("لا يوجد تحذير لإزالته.")


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "moderator"):
        return
    message = update.effective_message
    if message.reply_to_message:
        try:
            await message.reply_to_message.delete()
            await message.delete()
        except Exception:
            await message.reply_text("تعذر حذف الرسالة.")
        return
    args = command_args(update)
    if not args or not args[0].isdigit():
        await message.reply_text("استخدم /del بالرد على رسالة، أو /purge مع عدد الرسائل.")
        return
    count = min(int(args[0]), 100)
    message_id = message.message_id
    for message_id_to_delete in range(message_id, message_id - count - 1, -1):
        try:
            await context.bot.delete_message(update.effective_chat.id, message_id_to_delete)
        except Exception:
            pass


async def purge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "moderator"):
        return
    message = update.effective_message
    args = command_args(update)
    if message.reply_to_message:
        start = message.reply_to_message.message_id
        end = message.message_id
    elif args and args[0].isdigit():
        count = min(int(args[0]), 100)
        start, end = max(1, message.message_id - count), message.message_id
    else:
        await message.reply_text("الصيغة: /purge 20 أو استخدمه بالرد على رسالة.")
        return
    for message_id in range(start, end + 1):
        try:
            await context.bot.delete_message(update.effective_chat.id, message_id)
        except Exception:
            pass


async def word_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "admin"):
        return
    args = command_args(update)
    action = update.effective_message.text.split()[0].lstrip("/").lower()
    if action == "wordlist":
        words = db.words(update.effective_chat.id)
        await update.effective_message.reply_text("الكلمات الممنوعة:\n" + ("\n".join(words) if words else "القائمة فارغة."))
        return
    if not args:
        await update.effective_message.reply_text("اكتب الكلمة بعد الأمر.")
        return
    if action == "addword":
        db.add_word(update.effective_chat.id, args[0])
        await update.effective_message.reply_text("تمت إضافة الكلمة للقائمة.")
    elif action == "delword":
        db.remove_word(update.effective_chat.id, args[0])
        await update.effective_message.reply_text("تم حذف الكلمة من القائمة.")


async def flood_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "admin"):
        return
    args = command_args(update)
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await update.effective_message.reply_text("الصيغة: /setflood عدد_الرسائل الثواني mute")
        return
    action = args[2].lower() if len(args) > 2 else "mute"
    if action not in {"mute", "delete"}:
        await update.effective_message.reply_text("الإجراء يجب أن يكون mute أو delete.")
        return
    db.set_setting(update.effective_chat.id, "flood_count", max(2, int(args[0])))
    db.set_setting(update.effective_chat.id, "flood_window", max(1, int(args[1])))
    db.set_setting(update.effective_chat.id, "flood_action", action)
    await update.effective_message.reply_text("تم تحديث إعدادات مكافحة الفيضان.")


async def captcha_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "admin"):
        return
    action = update.effective_message.text.split()[0].lstrip("/").lower()
    args = command_args(update)
    if action == "setcaptcha":
        if not args or args[0].lower() not in {"on", "off"}:
            await update.effective_message.reply_text("الصيغة: /setcaptcha on أو /setcaptcha off")
            return
        db.set_setting(update.effective_chat.id, "captcha_enabled", int(args[0].lower() == "on"))
        await update.effective_message.reply_text("تم تحديث إعداد التحقق.")
    else:
        if not args or not args[0].isdigit():
            await update.effective_message.reply_text("الصيغة: /setcaptchatime دقائق")
            return
        db.set_setting(update.effective_chat.id, "captcha_timeout", max(1, int(args[0])))
        await update.effective_message.reply_text("تم تحديث مهلة التحقق.")


async def lockdown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "senior_admin"):
        return
    enabled = update.effective_message.text.split()[0].lstrip("/").lower() == "lock"
    db.set_setting(update.effective_chat.id, "lockdown", int(enabled))
    permissions = ChatPermissions(can_send_messages=not enabled)
    try:
        await update.effective_chat.set_permissions(permissions)
        await update.effective_message.reply_text("تم تفعيل القفل الطارئ." if enabled else "تم رفع القفل.")
    except Exception:
        await update.effective_message.reply_text("تعذر تعديل صلاحيات المجموعة.")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "senior_admin"):
        return
    args = command_args(update)
    action = update.effective_message.text.split()[0].lstrip("/").lower()
    target = await resolve_target(update, args[0] if args else None)
    if action in {"promote", "demote"} and not target:
        await update.effective_message.reply_text("استخدم الأمر بالرد على العضو أو بذكر المعرف.")
        return
    if action == "promote":
        role = args[1] if len(args) > 1 else "admin"
        if role not in {"moderator", "admin", "senior_admin"}:
            await update.effective_message.reply_text("الرتبة: moderator أو admin أو senior_admin")
            return
        if ROLE_LEVELS[db.role(update.effective_chat.id, target.user_id)] >= ROLE_LEVELS[actor_role(update)]:
            await update.effective_message.reply_text("لا يمكن تعديل رتبة أدمن أعلى منك.")
            return
        db.set_role(update.effective_chat.id, target.user_id, role)
        await update.effective_message.reply_text(f"تمت ترقية {target.display_name} إلى {ROLE_LABELS[role]}.")
    elif action == "demote":
        if not await can_act_on(update, target):
            return
        db.remove_role(update.effective_chat.id, target.user_id)
        await update.effective_message.reply_text(f"تم تنزيل رتبة {target.display_name}.")
    elif action == "adminlist":
        rows = db.admins(update.effective_chat.id)
        lines = [f"{row['user_id']} — {ROLE_LABELS.get(row['role'], row['role'])}" for row in rows]
        for owner_id in sorted(OWNER_IDS):
            lines.insert(0, f"{owner_id} — مالك")
        await update.effective_message.reply_text("قائمة الإدارة:\n" + ("\n".join(lines) if lines else "لا يوجد أدمنية."))


async def text_setting_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "senior_admin"):
        return
    action = update.effective_message.text.split()[0].lstrip("/").lower()
    value = update.effective_message.text.partition(" ")[2].strip()
    if action == "rules" and not value:
        value = db.settings(update.effective_chat.id)["rules"] or "لم يتم تحديد القوانين بعد."
        await update.effective_message.reply_text(value)
        return
    if action == "setrules":
        db.set_setting(update.effective_chat.id, "rules", value or "لم يتم تحديد القوانين بعد.")
        await update.effective_message.reply_text("تم حفظ القوانين.")
    elif action == "setwelcome":
        db.set_setting(update.effective_chat.id, "welcome", value)
        await update.effective_message.reply_text("تم حفظ رسالة الترحيب.")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_group(update):
        return
    row = db.settings(update.effective_chat.id)
    text = (
        f"الإعدادات:\n"
        f"حد التحذيرات: {row['warn_limit']}\n"
        f"الفيضان: {row['flood_count']} رسائل / {row['flood_window']} ثوانٍ / {row['flood_action']}\n"
        f"التحقق: {'مفعل' if row['captcha_enabled'] else 'معطل'} ({row['captcha_timeout']} دقائق)\n"
        f"القفل: {'مفعل' if row['lockdown'] else 'معطل'}\n"
        f"الكلمات الممنوعة: {len(db.words(update.effective_chat.id))}"
    )
    await update.effective_message.reply_text(text)


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "moderator"):
        return
    rows = db.recent_logs(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("لا توجد إجراءات مسجلة.")
        return
    lines = [
        f"{row['created_at']} | {row['action']} | العضو: {row['target_id']} | السبب: {row['reason']}"
        for row in rows
    ]
    await update.effective_message.reply_text("\n".join(lines))


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_role(update, "moderator"):
        return
    args = command_args(update)
    target = await resolve_target(update, args[0] if args else None)
    if not target:
        await update.effective_message.reply_text("استخدم الأمر بالرد على العضو أو بذكر المعرف.")
        return
    rows = [row for row in db.recent_logs(update.effective_chat.id, 100) if row["target_id"] == target.user_id]
    if not rows:
        await update.effective_message.reply_text("لا يوجد سجل لهذا العضو.")
        return
    await update.effective_message.reply_text(
        "\n".join(f"{row['created_at']} | {row['action']} | {row['reason']}" for row in rows)
    )


async def new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.chat_member:
        old_status = update.chat_member.old_chat_member.status
        new_status = update.chat_member.new_chat_member.status
        joined_statuses = {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.RESTRICTED,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }
        if old_status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED} or new_status not in joined_statuses:
            return
        chat = update.chat_member.chat
        members = [update.chat_member.new_chat_member.user]
    elif update.effective_message and update.effective_message.new_chat_members:
        chat = update.effective_chat
        members = update.effective_message.new_chat_members
    else:
        return
    chat_id = chat.id
    row = db.settings(chat_id)
    for user in members:
        if row["welcome"]:
            await context.bot.send_message(
                chat_id,
                row["welcome"].replace("{name}", escape(user.full_name)).replace("{group}", escape(chat.title or "")),
                parse_mode=ParseMode.HTML,
            )
        if row["captcha_enabled"] and not user.is_bot:
            expires = int(time.time()) + row["captcha_timeout"] * 60
            captcha_users[(chat_id, user.id)] = expires
            keyboard = [[InlineKeyboardButton("أنا لست بوت", callback_data=f"captcha:{chat_id}:{user.id}")]]
            try:
                await context.bot.restrict_chat_member(
                    chat_id,
                    user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=expires,
                )
                await context.bot.send_message(
                    chat_id,
                    f"مرحبًا {escape(user.full_name)}، اضغط الزر خلال {row['captcha_timeout']} دقائق للتحقق.",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                LOGGER.exception("Captcha setup failed")


async def captcha_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_raw, user_id_raw = query.data.split(":")
        chat_id, user_id = int(chat_id_raw), int(user_id_raw)
    except Exception:
        return
    if query.from_user.id != user_id:
        await query.answer("هذا الزر ليس مخصصًا لك.", show_alert=True)
        return
    try:
        await context.bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        captcha_users.pop((chat_id, user_id), None)
        await query.edit_message_text("تم التحقق بنجاح.")
    except Exception:
        await query.edit_message_text("تعذر إكمال التحقق.")


async def moderation_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group(update) or not update.effective_message or not update.effective_user:
        return
    message = update.effective_message
    user = update.effective_user
    chat_id = update.effective_chat.id
    if user.is_bot or ROLE_LEVELS[db.role(chat_id, user.id)] >= ROLE_LEVELS["moderator"]:
        return
    settings = db.settings(chat_id)
    text = message.text or message.caption or ""
    words = db.words(chat_id)
    matched = next((word for word in words if word.casefold() in text.casefold()), None)
    if matched or (settings["lockdown"] and text):
        try:
            await message.delete()
        except Exception:
            pass
        if matched:
            db.log(chat_id, user.id, "auto-delete", f"كلمة ممنوعة: {matched}", 0)
        return
    now = time.monotonic()
    key = (chat_id, user.id)
    queue = flood_tracker[key]
    queue.append(now)
    while queue and now - queue[0] > settings["flood_window"]:
        queue.popleft()
    if len(queue) > settings["flood_count"]:
        queue.clear()
        try:
            await message.delete()
        except Exception:
            pass
        if settings["flood_action"] == "mute":
            target = Target(user.id, user.full_name, user.username)
            fake_update = update
            await apply_restriction(
                fake_update,
                target,
                "mute",
                "مكافحة الفيضان",
                int(time.time()) + settings["flood_mute_minutes"] * 60,
            )
        return
    if URL_RE.search(text):
        try:
            await message.delete()
            db.log(chat_id, user.id, "auto-delete", "رابط من عضو غير موثوق", 0)
        except Exception:
            pass


async def expire_punishments(context: ContextTypes.DEFAULT_TYPE) -> None:
    for row in db.active_punishments():
        try:
            await context.bot.unban_chat_member(row["chat_id"], row["user_id"], only_if_banned=True)
            await context.bot.restrict_chat_member(
                row["chat_id"],
                row["user_id"],
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_audios=True,
                    can_send_documents=True,
                    can_send_photos=True,
                    can_send_videos=True,
                    can_send_video_notes=True,
                    can_send_voice_notes=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                ),
            )
        except Exception as exc:
            LOGGER.info("Expiry action failed for %s/%s: %s", row["chat_id"], row["user_id"], exc)
        finally:
            db.delete_punishment(row["id"])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error("Unhandled update error: %s", context.error, exc_info=context.error)


def build_application() -> Application:
    if not BOT_TOKEN or BOT_TOKEN == "ضع_توكن_البوت_هنا":
        raise RuntimeError("ضع BOT_TOKEN في ملف .env قبل التشغيل.")
    if not OWNER_IDS:
        raise RuntimeError("ضع ADMIN_ID في ملف .env قبل التشغيل.")
    app = Application.builder().token(BOT_TOKEN).build()
    for name in ("kick", "ban", "tban", "mute"):
        app.add_handler(CommandHandler(name, punishment_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("unmute", unmute_command))
    app.add_handler(CommandHandler("warn", warn_command))
    app.add_handler(CommandHandler("unwarn", unwarn_command))
    app.add_handler(CommandHandler("warns", warns_command))
    app.add_handler(CommandHandler("del", delete_command))
    app.add_handler(CommandHandler("purge", purge_command))
    app.add_handler(CommandHandler(["addword", "delword", "wordlist"], word_command))
    app.add_handler(CommandHandler("setwarnlimit", warn_settings_command))
    app.add_handler(CommandHandler("setwarnaction", warn_settings_command))
    app.add_handler(CommandHandler("setflood", flood_command))
    app.add_handler(CommandHandler(["setcaptcha", "setcaptchatime"], captcha_command))
    app.add_handler(CommandHandler(["lock", "unlock"], lockdown_command))
    app.add_handler(CommandHandler(["promote", "demote", "adminlist"], admin_command))
    app.add_handler(CommandHandler(["setrules", "setwelcome", "rules"], text_setting_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler(["log", "history"], logs_command))
    app.add_handler(ChatMemberHandler(new_members, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_members))
    app.add_handler(CallbackQueryHandler(captcha_button, pattern=r"^captcha:"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, moderation_filter))
    app.add_error_handler(error_handler)
    if app.job_queue:
        app.job_queue.run_repeating(expire_punishments, interval=60, first=10)
    return app


if __name__ == "__main__":
    application = build_application()
    LOGGER.info("Telegram Group Guard is running.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)