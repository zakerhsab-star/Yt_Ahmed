import os
import asyncio
import urllib.parse
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
import yt_dlp

BOT_TOKEN = os.environ.get("BOT_TOKEN")
# Render يوفر هذا المتغير تلقائياً، أو يستخدم الرابط المحلي عند التجربة
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080").rstrip("/")
DOWNLOAD_DIR = "downloads"

# مسار تقديم الملفات الكبيرة برابط مباشر
async def download_file_handler(request):
    filename = request.match_info.get('filename')
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(filepath):
        return web.FileResponse(filepath)
    return web.Response(text="الملف غير موجود أو انتهت صلاحيته.", status=404)

async def health_check(request):
    return web.Response(text="Bot & File Server Running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/download/{filename}', download_file_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def delete_file_later(filepath: str, delay_seconds: int = 1800):
    await asyncio.sleep(delay_seconds)
    if os.path.exists(filepath):
        os.remove(filepath)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً بك!\n"
        "أرسل لي أي رابط يوتيوب (فيديو فردي أو قائمة تشغيل) وسأتولى معالجته فوراً."
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not ("youtube.com" in url or "youtu.be" in url):
        await update.message.reply_text("يرجى إرسال رابط يوتيوب صحيح.")
        return

    # حفظ الرابط في بيانات الجلسة للمستخدم
    context.user_data['current_url'] = url
    is_playlist = "list=" in url

    if is_playlist:
        keyboard = [
            [InlineKeyboardButton("📹 الفيديو الحالي فقط", callback_data="type_single")],
            [InlineKeyboardButton("📑 القائمة بالكامل", callback_data="type_playlist_all")],
            [InlineKeyboardButton("🔢 تحديد نطاق (مثلاً 3-25)", callback_data="type_playlist_range")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("تم التعرف على قائمة تشغيل. ماذا تفضل أن تفعل؟", reply_markup=reply_markup)
    else:
        context.user_data['playlist_items'] = None
        await ask_format(update.message, context)

async def ask_format(message_target, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎬 فيديو عالي الدقة", callback_data="fmt_video")],
        [InlineKeyboardButton("🎧 صوت فقط (MP3)", callback_data="fmt_audio")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await message_target.reply_text("اختر الصيغة المطلوبة:", reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "type_single":
        context.user_data['playlist_items'] = "1"
        await ask_format(query.message, context)
    elif data == "type_playlist_all":
        context.user_data['playlist_items'] = "all"
        await ask_format(query.message, context)
    elif data == "type_playlist_range":
        context.user_data['awaiting_range'] = True
        await query.message.reply_text("أرسل أرقام الفيديوهات المطلوبة بصيغة البداية والنهاية (مثال: `3-25`):", parse_mode="Markdown")
    elif data in ["fmt_video", "fmt_audio"]:
        fmt_type = "audio" if data == "fmt_audio" else "video"
        await process_download(query.message, context, fmt_type)

async def handle_range_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_range'):
        context.user_data['playlist_items'] = update.message.text.strip()
        context.user_data['awaiting_range'] = False
        await ask_format(update.message, context)

async def process_download(message, context: ContextTypes.DEFAULT_TYPE, fmt_type: str):
    url = context.user_data.get('current_url')
    items = context.user_data.get('playlist_items')
    
    status_msg = await message.reply_text("⏳ جاري سحب البيانات وبدء المعالجة...")

    ydl_opts = {
        'outtmpl': f'{DOWNLOAD_DIR}/%(title).100s.%(ext)s',
        'noplaylist': items == "1" or items is None,
    }

    if items and items not in ["1", "all"]:
        ydl_opts['playlist_items'] = items

    if fmt_type == "audio":
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        })
    else:
        ydl_opts['format'] = 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4] / best'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            entries = info.get('entries') if 'entries' in info else [info]

        for entry in entries:
            if not entry:
                continue
            filename = ydl.prepare_filename(entry)
            if fmt_type == "audio":
                filename = os.path.splitext(filename)[0] + ".mp3"

            if not os.path.exists(filename):
                continue

            file_size_mb = os.path.getsize(filename) / (1024 * 1024)
            title = entry.get('title', 'فيديو')

            # فحص الحجم بالنسبة لحد تليجرام (50 ميجا)
            if file_size_mb <= 50:
                await status_msg.edit_text(f"📤 جاري إرسال: {title} ({file_size_mb:.1f} MB)...")
                with open(filename, 'rb') as f:
                    if fmt_type == "audio":
                        await message.reply_audio(audio=f, title=title)
                    else:
                        await message.reply_video(video=f, caption=title)
                os.remove(filename)
            else:
                encoded_name = urllib.parse.quote(os.path.basename(filename))
                download_link = f"{BASE_URL}/download/{encoded_name}"
                await message.reply_text(
                    f"⚠️ **حجم الملف كبير ({file_size_mb:.1f} MB)** وتجاوز حد الـ 50MB الخاص بتليجرام.\n\n"
                    f"📥 **رابط التحميل المباشر والسريع من السيرفر:**\n{download_link}\n\n"
                    f"*(الرابط متاح لمدة 30 دقيقة قبل الحذف التلقائي لتوفير المساحة)*",
                    parse_mode="Markdown"
                )
                asyncio.create_task(delete_file_later(filename, 1800))

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ حدث خطأ أثناء التنزيل: {str(e)}")

async def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    await start_web_server()

    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CallbackQueryHandler(button_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r'^\d+-\d+$'), handle_range_input))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()

    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.run(main())
  
