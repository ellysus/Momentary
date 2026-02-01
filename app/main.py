import asyncio
import contextlib
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.db import Database
from app.storage import MinioConfig, MinioStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("momentary")

app = FastAPI(title="Momentary")
cors_allow_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",")
    if origin.strip()
]
if cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

db: Optional[Database] = None
storage: Optional[MinioStorage] = None
bot_app: Optional[Application] = None
scheduler_task: Optional[asyncio.Task] = None
PROMPT_MINUTE_HISTORY_DAYS = 1440
SESSION_COOKIE_NAME = "momentary_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14


def load_minio_config() -> MinioConfig:
    return MinioConfig(
        endpoint=os.getenv("MINIO_ENDPOINT", "minio:9000"),
        access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
        secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
        bucket=os.getenv("MINIO_BUCKET", "photos"),
        secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
    )


def choose_prompt_time(
    now: datetime,
    last_prompt: Optional[datetime],
    excluded_minutes: set[int],
) -> datetime:
    today = now.date()
    if last_prompt and last_prompt.date() == today:
        target_day = today + timedelta(days=1)
    else:
        target_day = today

    available_minutes = [m for m in range(24 * 60) if m not in excluded_minutes]
    if not available_minutes:
        available_minutes = list(range(24 * 60))
    minute_of_day = random.choice(available_minutes)
    hour = minute_of_day // 60
    minute = minute_of_day % 60

    target = datetime(
        year=target_day.year,
        month=target_day.month,
        day=target_day.day,
        hour=hour,
        minute=minute,
        tzinfo=timezone.utc,
    )
    if target <= now:
        target_day = today + timedelta(days=1)
        available_minutes = [m for m in range(24 * 60) if m not in excluded_minutes]
        if not available_minutes:
            available_minutes = list(range(24 * 60))
        minute_of_day = random.choice(available_minutes)
        hour = minute_of_day // 60
        minute = minute_of_day % 60
        target = datetime(
            year=target_day.year,
            month=target_day.month,
            day=target_day.day,
            hour=hour,
            minute=minute,
            tzinfo=timezone.utc,
        )
    return target


def get_owner_telegram_id() -> Optional[int]:
    raw = os.getenv("TELEGRAM_OWNER_ID") or os.getenv("BOT_OWNER_TELEGRAM_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid TELEGRAM_OWNER_ID/BOT_OWNER_TELEGRAM_ID: %r", raw)
        return None


def is_owner(update: Update) -> bool:
    owner_id = get_owner_telegram_id()
    if not owner_id or not update.effective_user:
        return False
    return update.effective_user.id == owner_id


async def require_owner(update: Update) -> bool:
    owner_id = get_owner_telegram_id()
    if not owner_id:
        if update.message:
            await update.message.reply_text(
                "Bot owner is not configured. Set TELEGRAM_OWNER_ID and try again."
            )
        return False
    if update.effective_user and update.effective_user.id == owner_id:
        return True
    if update.message:
        await update.message.reply_text("This command is for the bot owner only.")
    return False


def format_dt_utc(value: Optional[datetime]) -> str:
    if not value:
        return "—"
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_duration(seconds: float) -> str:
    total = int(seconds)
    if total < 0:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _get_session_secret() -> bytes:
    secret = os.getenv("APP_SESSION_SECRET")
    if not secret:
        raise RuntimeError("APP_SESSION_SECRET is not set")
    return secret.encode("utf-8")


def create_session_token(telegram_id: int) -> str:
    issued_at = int(time.time())
    payload = {
        "telegram_id": telegram_id,
        "iat": issued_at,
        "exp": issued_at + SESSION_TTL_SECONDS,
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_get_session_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def parse_and_verify_session(token: str) -> Optional[dict]:
    if not token or "." not in token:
        return None
    payload_b64, sig_b64 = token.split(".", 1)
    try:
        expected_sig = hmac.new(
            _get_session_secret(), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64url_decode(sig_b64), expected_sig):
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def verify_telegram_login_payload(query_params: dict[str, str]) -> Optional[dict[str, str]]:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return None

    provided_hash = query_params.get("hash")
    if not provided_hash:
        return None

    data = {k: v for k, v in query_params.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hashlib.sha256(token.encode("utf-8")).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(computed_hash, provided_hash):
        return None

    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return None
    if abs(int(time.time()) - auth_date) > 60 * 10:
        return None

    return data


async def send_daily_prompt() -> None:
    global db, bot_app
    if not db or not bot_app:
        return

    # Notify all registered users at the same time.
    users = db.get_users()
    if not users:
        logger.info("No registered users to notify.")
        return

    banned_ids = {int(row["telegram_id"]) for row in db.get_banned_users()}
    message = "\U0001f4f8 Momentary time! Send a photo of what you're doing right now. You have 60 seconds."
    for user in users:
        if int(user["telegram_id"]) in banned_ids:
            continue
        try:
            await bot_app.bot.send_message(chat_id=user["telegram_id"], text=message)
        except Exception as exc:  # pragma: no cover - network issues
            logger.warning("Failed to send prompt to %s: %s", user["telegram_id"], exc)

    prompt_time = datetime.now(timezone.utc)
    db.set_last_prompt(prompt_time)
    db.add_prompt_history(prompt_time)
    db.prune_prompt_history(PROMPT_MINUTE_HISTORY_DAYS)


async def scheduler_loop() -> None:
    global db
    while True:
        # Pick a new random prompt time each day (UTC).
        now = datetime.now(timezone.utc)
        if not db:
            await asyncio.sleep(5)
            continue

        target = db.get_next_prompt()
        if target and target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)

        if not target or target <= now:
            last_prompt = db.get_last_prompt()
            excluded = set(db.get_recent_prompt_minutes(PROMPT_MINUTE_HISTORY_DAYS - 1))
            target = choose_prompt_time(now, last_prompt, excluded)
            db.set_next_prompt(target)

        sleep_seconds = (target - now).total_seconds()
        logger.info("Next prompt scheduled for %s UTC", target.isoformat())
        await asyncio.sleep(sleep_seconds)
        await send_daily_prompt()


async def start_bot() -> None:
    global bot_app
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not set; bot will not start.")
        return

    bot_app = ApplicationBuilder().token(token).build()

    bot_app.add_handler(CommandHandler("start", handle_start))
    bot_app.add_handler(CommandHandler("whoami", handle_whoami))
    bot_app.add_handler(CommandHandler("commandlist", handle_commandlist))
    bot_app.add_handler(CommandHandler("prompts", handle_prompts))
    bot_app.add_handler(CommandHandler("users", handle_users))
    bot_app.add_handler(CommandHandler("user", handle_user))
    bot_app.add_handler(CommandHandler("ban", handle_ban))
    bot_app.add_handler(CommandHandler("unban", handle_unban))
    bot_app.add_handler(CommandHandler("delete_user", handle_delete_user))
    bot_app.add_handler(CommandHandler("stats", handle_stats))
    bot_app.add_handler(CommandHandler("registrations", handle_registrations))
    bot_app.add_handler(CommandHandler("open_registrations", handle_open_registrations))
    bot_app.add_handler(
        CommandHandler("close_registrations", handle_close_registrations)
    )
    bot_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    logger.info("Telegram bot started.")


async def stop_bot() -> None:
    global bot_app
    if not bot_app:
        return
    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()
    logger.info("Telegram bot stopped.")


async def handle_registrations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global db
    if not db or not update.message:
        return
    if not await require_owner(update):
        return

    open_state = db.are_registrations_open()
    await update.message.reply_text(
        f"Registrations are currently {'OPEN' if open_state else 'CLOSED'}."
    )


async def handle_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    await update.message.reply_text(
        f"Your Telegram user id is {update.effective_user.id}. Chat id is {update.effective_chat.id}."
    )


async def handle_commandlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await require_owner(update):
        return

    await update.message.reply_text(
        "\n".join(
            [
                "Owner commands:",
                "/registrations - show open/closed",
                "/open_registrations - open registrations",
                "/close_registrations - close registrations",
                "/prompts - show last/next prompt",
                "/users - list users + photo counts",
                "/user <telegram_id> - user details + photo count",
                "/ban <telegram_id> [reason] - ban user",
                "/unban <telegram_id> - unban user",
                "/delete_user <telegram_id> - delete user (removes their DB photo records)",
                "/stats - overall app stats",
            ]
        )
    )


async def handle_prompts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global db
    if not db or not update.message:
        return
    if not await require_owner(update):
        return

    now = datetime.now(timezone.utc)
    last_prompt = db.get_last_prompt()
    next_prompt = db.get_next_prompt()

    if next_prompt and next_prompt.tzinfo is None:
        next_prompt = next_prompt.replace(tzinfo=timezone.utc)

    if not next_prompt or next_prompt <= now:
        excluded = set(db.get_recent_prompt_minutes(PROMPT_MINUTE_HISTORY_DAYS - 1))
        next_prompt = choose_prompt_time(now, last_prompt, excluded)
        db.set_next_prompt(next_prompt)

    seconds_until = (next_prompt - now).total_seconds() if next_prompt else 0
    await update.message.reply_text(
        "\n".join(
            [
                f"Last prompt: {format_dt_utc(last_prompt)}",
                f"Next prompt: {format_dt_utc(next_prompt)}",
                f"Time until next: {format_duration(seconds_until)}",
            ]
        )
    )


async def handle_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global db
    if not db or not update.message:
        return
    if not await require_owner(update):
        return

    users = db.get_users_with_photo_counts()
    if not users:
        await update.message.reply_text("No registered users.")
        return

    banned_ids = {int(row["telegram_id"]) for row in db.get_banned_users()}
    lines = [f"Registered users: {len(users)}"]
    for user in users:
        username = user.get("username") or "—"
        banned = " (BANNED)" if int(user["telegram_id"]) in banned_ids else ""
        lines.append(
            f'{user["telegram_id"]} @{username} photos:{user["photo_count"]}{banned}'
        )

    chunk = []
    for line in lines:
        chunk.append(line)
        if len(chunk) >= 40:
            await update.message.reply_text("\n".join(chunk))
            chunk = []
    if chunk:
        await update.message.reply_text("\n".join(chunk))


async def handle_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global db
    if not db or not update.message:
        return
    if not await require_owner(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /user <telegram_id>")
        return
    try:
        telegram_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("telegram_id must be an integer.")
        return

    user = db.get_user_by_telegram(telegram_id)
    if not user:
        await update.message.reply_text("User not found.")
        return
    photo_count = db.count_photos_for_user(int(user["id"]))
    banned = db.is_user_banned(telegram_id)
    await update.message.reply_text(
        "\n".join(
            [
                f"telegram_id: {user['telegram_id']}",
                f"username: @{user['username']}" if user.get("username") else "username: —",
                f"created_at: {user.get('created_at','—')}",
                f"photos: {photo_count}",
                f"banned: {'yes' if banned else 'no'}",
            ]
        )
    )


async def handle_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global db
    if not db or not update.message:
        return
    if not await require_owner(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /ban <telegram_id> [reason]")
        return
    try:
        telegram_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("telegram_id must be an integer.")
        return

    reason = " ".join(context.args[1:]).strip() or None
    db.ban_user(telegram_id, reason)
    await update.message.reply_text(
        f"Banned {telegram_id}." + (f" Reason: {reason}" if reason else "")
    )


async def handle_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global db
    if not db or not update.message:
        return
    if not await require_owner(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /unban <telegram_id>")
        return
    try:
        telegram_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("telegram_id must be an integer.")
        return

    db.unban_user(telegram_id)
    await update.message.reply_text(f"Unbanned {telegram_id}.")


async def handle_delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global db
    if not db or not update.message:
        return
    if not await require_owner(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /delete_user <telegram_id>")
        return
    try:
        telegram_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("telegram_id must be an integer.")
        return

    deleted = db.delete_user(telegram_id)
    if deleted:
        await update.message.reply_text(
            f"Deleted user {telegram_id} and removed their photo records from the database."
        )
    else:
        await update.message.reply_text("User not found.")


async def handle_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global db
    if not db or not update.message:
        return
    if not await require_owner(update):
        return

    now = datetime.now(timezone.utc)
    last_prompt = db.get_last_prompt()
    next_prompt = db.get_next_prompt()
    registrations_open = db.are_registrations_open()

    if next_prompt and next_prompt.tzinfo is None:
        next_prompt = next_prompt.replace(tzinfo=timezone.utc)
    if not next_prompt or next_prompt <= now:
        excluded = set(db.get_recent_prompt_minutes(PROMPT_MINUTE_HISTORY_DAYS - 1))
        next_prompt = choose_prompt_time(now, last_prompt, excluded)
        db.set_next_prompt(next_prompt)

    await update.message.reply_text(
        "\n".join(
            [
                f"registrations_open: {'yes' if registrations_open else 'no'}",
                f"users: {db.count_users()}",
                f"banned_users: {db.count_banned_users()}",
                f"total_photos: {db.count_photos_total()}",
                f"prompt_history_entries: {db.count_prompt_history()}",
                f"last_prompt: {format_dt_utc(last_prompt)}",
                f"next_prompt: {format_dt_utc(next_prompt)}",
            ]
        )
    )


async def handle_open_registrations(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global db
    if not db or not update.message:
        return
    if not await require_owner(update):
        return

    db.set_registrations_open(True)
    await update.message.reply_text("Registrations are now OPEN.")


async def handle_close_registrations(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global db
    if not db or not update.message:
        return
    if not await require_owner(update):
        return

    db.set_registrations_open(False)
    await update.message.reply_text("Registrations are now CLOSED.")


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global db
    if not db or not update.effective_chat or not update.message:
        return

    if db.is_user_banned(update.effective_chat.id):
        await update.message.reply_text("You are banned from this app.")
        return

    user = update.effective_user
    username = user.username if user else None
    existing = db.get_user_by_telegram(update.effective_chat.id)

    if existing:
        db.upsert_user(update.effective_chat.id, username)
        await update.message.reply_text(
            f"You're already registered. You'll get a daily random prompt. your user id is {update.effective_chat.id}."
        )
        return

    if not db.are_registrations_open():
        await update.message.reply_text(
            "Registrations are currently closed. Please contact the bot owner to be added."
        )
        return

    db.upsert_user(update.effective_chat.id, username)
    await update.message.reply_text(
        f"Registration successful! You'll get a daily random prompt. When it arrives, reply with a photo within 60 seconds to save it. Your User Id is {update.effective_chat.id}."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global db, storage
    if not db or not storage or not update.effective_chat:
        return

    if db.is_user_banned(update.effective_chat.id):
        await update.message.reply_text("You are banned from this app.")
        return

    # Only accept photos within 60 seconds of the last prompt.
    last_prompt = db.get_last_prompt()
    if not last_prompt:
        await update.message.reply_text("No prompt is active yet. Check back later!")
        return

    now = datetime.now(timezone.utc)
    if (now - last_prompt).total_seconds() > 60:
        await update.message.reply_text(
            "Sorry, you missed the 60-second window. Try again next time."
        )
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()

    user_record = db.get_user_by_telegram(update.effective_chat.id)
    if not user_record:
        user_id = db.upsert_user(
            update.effective_chat.id, update.effective_user.username
        )
    else:
        user_id = user_record["id"]

    timestamp = datetime.now(timezone.utc)
    object_key = (
        f"user_{update.effective_chat.id}/{timestamp.strftime('%Y%m%d_%H%M%S')}.jpg"
    )

    storage.upload_photo(object_key, bytes(photo_bytes), content_type="image/jpeg")
    db.add_photo(user_id, timestamp.isoformat(), object_key)

    await update.message.reply_text("Photo received! It's saved to your journal. \u2705")


@app.on_event("startup")
async def on_startup() -> None:
    global db, storage, scheduler_task

    db_path = os.getenv("DATABASE_PATH", "/data/momentary.db")
    db = Database(db_path)
    db.init()

    storage = MinioStorage(load_minio_config())

    scheduler_task = asyncio.create_task(scheduler_loop())
    asyncio.create_task(start_bot())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global scheduler_task
    if scheduler_task:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
    await stop_bot()


@app.get("/users/{telegram_id}/photos")
async def list_photos(telegram_id: int) -> JSONResponse:
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")
    user = db.get_user_by_telegram(telegram_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    photos = db.list_photos_for_user(user["id"])
    enriched = []
    for photo in photos:
        photo_data = dict(photo)
        if storage:
            photo_data["url"] = storage.get_presigned_url(photo["object_key"])
        enriched.append(photo_data)
    return JSONResponse(content={"user": telegram_id, "photos": enriched})

@app.post("/auth/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return response


@app.get("/auth/telegram/callback")
async def telegram_auth_callback(request: Request) -> RedirectResponse:
    global db
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")

    payload = verify_telegram_login_payload(dict(request.query_params))
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid Telegram login payload")

    try:
        telegram_id = int(payload["id"])
    except Exception:
        raise HTTPException(status_code=400, detail="Missing Telegram user id")

    if db.is_user_banned(telegram_id):
        raise HTTPException(status_code=403, detail="Banned")

    existing = db.get_user_by_telegram(telegram_id)
    if not existing and not db.are_registrations_open():
        raise HTTPException(status_code=403, detail="Registrations are closed")

    db.upsert_user(telegram_id, payload.get("username"))

    try:
        session_token = create_session_token(telegram_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    redirect_url = os.getenv("TELEGRAM_LOGIN_REDIRECT_URL", "/")
    response = RedirectResponse(url=redirect_url, status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        max_age=SESSION_TTL_SECONDS,
    )
    return response


@app.get("/me")
async def get_me(request: Request) -> JSONResponse:
    global db
    if not db:
        raise HTTPException(status_code=500, detail="Database not initialized")

    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    payload = parse_and_verify_session(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Not authenticated")

    telegram_id = int(payload["telegram_id"])
    user = db.get_user_by_telegram(telegram_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return JSONResponse(
        content={
            "telegram_id": user["telegram_id"],
            "username": user.get("username"),
            "created_at": user.get("created_at"),
        }
    )
