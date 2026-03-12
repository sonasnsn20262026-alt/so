import os
import asyncio
import re
import sys
from datetime import datetime
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Message, Channel
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ==============================
# 1. إعدادات التهيئة من متغيرات البيئة
# ==============================
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
# استخدام قائمة قنوات مفصولة بفواصل
CHANNELS = os.environ.get("CHANNELS", "https://t.me/ShoofFilm,https://t.me/shoofcima")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
STRING_SESSION = os.environ.get("STRING_SESSION", "")
IMPORT_HISTORY = os.environ.get("IMPORT_HISTORY", "false").lower() == "true"
CHECK_DELETED_MESSAGES = os.environ.get("CHECK_DELETED_MESSAGES", "true").lower() == "true"

# تحقق من وجود المتغيرات الأساسية
if not all([API_ID, API_HASH, DATABASE_URL, STRING_SESSION]):
    print("❌ خطأ: واحد أو أكثر من المتغيرات التالية مفقود: API_ID, API_HASH, DATABASE_URL, STRING_SESSION")
    sys.exit(1)

# إصلاح رابط قاعدة البيانات
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# تقسيم القنوات إلى قائمة
CHANNEL_LIST = [chan.strip() for chan in CHANNELS.split(',') if chan.strip()]

# ==============================
# 2. إعداد الاتصال بقاعدة البيانات
# ==============================
try:
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    print("✅ تم الاتصال بقاعدة البيانات بنجاح.")
except Exception as e:
    print(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
    sys.exit(1)

# ==============================
# 3. إنشاء الجداول إذا لم تكن موجودة وتعديل القيود
# ==============================
try:
    with engine.begin() as conn:
        # إنشاء جدول series إذا لم يكن موجوداً
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS series (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                type VARCHAR(10) DEFAULT 'series',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        # إنشاء جدول episodes إذا لم يكن موجوداً
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS episodes (
                id SERIAL PRIMARY KEY,
                series_id INTEGER REFERENCES series(id),
                season INTEGER DEFAULT 1,
                episode_number INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                telegram_channel_id VARCHAR(255),
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        
        # إزالة القيد الفريد القديم على telegram_message_id إذا كان موجوداً (لأنه سيتعارض مع الجديد)
        # نستخدم كتلة try/except لأن القيد قد لا يكون موجوداً
        try:
            conn.execute(text("ALTER TABLE episodes DROP CONSTRAINT IF EXISTS episodes_telegram_message_id_key"))
            print("✅ تم إزالة القيد الفريد القديم على telegram_message_id.")
        except Exception as e:
            print(f"⚠️ ملاحظة أثناء إزالة القيد: {e}")
        
        # إضافة قيد فريد جديد على (telegram_channel_id, telegram_message_id)
        conn.execute(text("""
            ALTER TABLE episodes 
            ADD CONSTRAINT unique_channel_message UNIQUE (telegram_channel_id, telegram_message_id)
        """))
        print("✅ تم إضافة القيد الفريد (telegram_channel_id, telegram_message_id).")
        
        # إنشاء الفهارس الأخرى
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_series_name_type ON series(name, type)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_episodes_channel_id ON episodes(telegram_channel_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_episodes_series_season ON episodes(series_id, season, episode_number)"))
        
    print("✅ تم التحقق من هياكل الجداول والفهارس وتحديث القيود.")
except Exception as e:
    print(f"⚠️ خطأ أثناء تعديل الجداول: {e}")
    # قد يكون القيد موجوداً بالفعل، نواصل التشغيل

# ==============================
# 4. دوال المساعدة (التحليل والحفظ والحذف)
# ==============================
def clean_name(name):
    """تنظيف الاسم من كلمات 'مسلسل' و'فيلم' والأرقام في النهاية."""
    if not name:
        return name
    
    # إزالة كلمات "مسلسل" و"فيلم" من البداية
    name = re.sub(r'^(مسلسل\s+|فيلم\s+)', '', name, flags=re.IGNORECASE)
    
    # إزالة كلمات "مسلسل" و"فيلم" من أي مكان (إذا كانت منفصلة)
    name = re.sub(r'\s+(مسلسل|فيلم)\s+', ' ', name, flags=re.IGNORECASE)
    
    # تنظيف المسافات الزائدة
    name = re.sub(r'\s+', ' ', name).strip()
    
    return name

def extract_numbers_from_name(name):
    """استخراج الأرقام من الاسم (مثل 13 من 'يوم-13')"""
    match = re.search(r'[-_]?(\d+)$', name)
    if match:
        return int(match.group(1))
    return None

def parse_content_info(message_text):
    """تحليل نص الرسالة لاستخراج المعلومات."""
    if not message_text:
        return None, None, None, None
    
    text_cleaned = message_text.strip()
    
    # 1. البحث عن نمط الأفلام
    film_pattern_dash = r'^فيلم\s+(.+?)[-_](\d+)$'
    match = re.search(film_pattern_dash, text_cleaned, re.IGNORECASE)
    if match:
        content_type = 'movie'
        raw_name = match.group(1).strip()
        season_num = int(match.group(2))
        episode_num = 1
        clean_name_text = clean_name(raw_name)
        return clean_name_text, content_type, season_num, episode_num
    
    film_pattern_space = r'^فيلم\s+(.+?)\s+(\d+)$'
    match = re.search(film_pattern_space, text_cleaned, re.IGNORECASE)
    if match:
        content_type = 'movie'
        raw_name = match.group(1).strip()
        season_num = int(match.group(2))
        episode_num = 1
        clean_name_text = clean_name(raw_name)
        return clean_name_text, content_type, season_num, episode_num
    
    film_pattern_name_only = r'^فيلم\s+(.+)$'
    match = re.search(film_pattern_name_only, text_cleaned, re.IGNORECASE)
    if match:
        content_type = 'movie'
        raw_name = match.group(1).strip()
        extracted_num = extract_numbers_from_name(raw_name)
        if extracted_num:
            raw_name = re.sub(r'[-_]?\d+$', '', raw_name).strip()
            season_num = extracted_num
        else:
            season_num = 1
        episode_num = 1
        clean_name_text = clean_name(raw_name)
        return clean_name_text, content_type, season_num, episode_num
    
    # 2. البحث عن نمط المسلسل مع الموسم
    series_season_pattern = r'^(.*?)\s+الموسم\s+(\d+)\s+الحلقة\s+(\d+)$'
    match = re.search(series_season_pattern, text_cleaned)
    if match:
        content_type = 'series'
        raw_name = match.group(1).strip()
        season_num = int(match.group(2))
        episode_num = int(match.group(3))
        clean_name_text = clean_name(raw_name)
        return clean_name_text, content_type, season_num, episode_num
    
    # 3. البحث عن نمط المسلسل بدون موسم
    series_episode_pattern = r'^(.*?)\s+الحلقة\s+(\d+)$'
    match = re.search(series_episode_pattern, text_cleaned)
    if match:
        content_type = 'series'
        raw_name = match.group(1).strip()
        season_num = 1
        episode_num = int(match.group(2))
        clean_name_text = clean_name(raw_name)
        return clean_name_text, content_type, season_num, episode_num
    
    # 4. البحث عن نمط بسيط
    simple_pattern = r'^(.*?[^\d\s])\s+(\d+)$'
    match = re.search(simple_pattern, text_cleaned)
    if match:
        raw_name = match.group(1).strip()
        
        if 'فيلم' in raw_name.lower():
            content_type = 'movie'
            season_num = int(match.group(2))
            episode_num = 1
        else:
            content_type = 'series'
            season_num = 1
            episode_num = int(match.group(2))
        
        clean_name_text = clean_name(raw_name)
        return clean_name_text, content_type, season_num, episode_num
    
    # 5. نمط المسلسل العربي
    arabic_series_pattern = r'^مسلسل\s+(.*?)\s+الموسم\s+(\d+)\s+الحلقة\s+(\d+)$'
    match = re.search(arabic_series_pattern, text_cleaned, re.IGNORECASE)
    if match:
        content_type = 'series'
        raw_name = match.group(1).strip()
        season_num = int(match.group(2))
        episode_num = int(match.group(3))
        clean_name_text = clean_name(raw_name)
        return clean_name_text, content_type, season_num, episode_num
    
    # 6. نمط المسلسل العربي بدون موسم
    arabic_series_simple = r'^مسلسل\s+(.*?)\s+الحلقة\s+(\d+)$'
    match = re.search(arabic_series_simple, text_cleaned, re.IGNORECASE)
    if match:
        content_type = 'series'
        raw_name = match.group(1).strip()
        season_num = 1
        episode_num = int(match.group(2))
        clean_name_text = clean_name(raw_name)
        return clean_name_text, content_type, season_num, episode_num
    
    print(f"⚠️ لم يتم التعرف على النمط للنص: {text_cleaned}")
    
    # محاولة أخيرة: إذا كان النص يحتوي على "فيلم" في البداية
    if text_cleaned.lower().startswith('فيلم'):
        content_type = 'movie'
        raw_name = text_cleaned[4:].strip()
        extracted_num = extract_numbers_from_name(raw_name)
        if extracted_num:
            raw_name = re.sub(r'[-_]?\d+$', '', raw_name).strip()
            season_num = extracted_num
        else:
            season_num = 1
        episode_num = 1
        clean_name_text = clean_name(raw_name)
        print(f"   ⚠️ معالجة كفيلم افتراضي: {clean_name_text}")
        return clean_name_text, content_type, season_num, episode_num
    
    return None, None, None, None

async def get_channel_entity(client, channel_input):
    """الحصول على كيان القناة مع معالجة أخطاء الانضمام."""
    try:
        # محاولة الحصول على القناة مباشرة
        channel = await client.get_entity(channel_input)
        return channel
    except Exception as e:
        print(f"⚠️ لم نتمكن من الوصول للقناة {channel_input}: {e}")
        
        # إذا كان رابط دعوة، حاول الانضمام
        if isinstance(channel_input, str) and channel_input.startswith('https://t.me/+'):
            try:
                # استخراج الهاش من الرابط
                invite_hash = channel_input.split('+')[-1]
                print(f"🔄 محاولة الانضمام للقناة عبر رابط الدعوة: {invite_hash}")
                
                # الانضمام للقناة
                await client(ImportChatInviteRequest(invite_hash))
                print(f"✅ تم الانضمام للقناة بنجاح")
                
                # المحاولة مرة أخرى
                return await client.get_entity(channel_input)
            except Exception as join_error:
                print(f"❌ فشل الانضمام: {join_error}")
                return None
        return None

def save_to_database(name, content_type, season_num, episode_num, telegram_msg_id, channel_id, series_id=None):
    """حفظ المحتوى في قاعدة البيانات مع التحقق من نجاح الإدراج باستخدام المفتاح المركب (channel, message)."""
    try:
        with engine.begin() as conn:
            # البحث عن المسلسل/الفيلم بنفس الاسم والنوع
            if not series_id:
                result = conn.execute(
                    text("""
                        SELECT id FROM series 
                        WHERE name = :name AND type = :type
                    """),
                    {"name": name, "type": content_type}
                ).fetchone()
                
                if not result:
                    # إضافة مسلسل/فيلم جديد
                    conn.execute(
                        text("""
                            INSERT INTO series (name, type) 
                            VALUES (:name, :type)
                        """),
                        {"name": name, "type": content_type}
                    )
                    # جلب الـ ID الجديد
                    result = conn.execute(
                        text("""
                            SELECT id FROM series 
                            WHERE name = :name AND type = :type
                        """),
                        {"name": name, "type": content_type}
                    ).fetchone()
                
                series_id = result[0]
            
            # إضافة الحلقة/الجزء مع معرف القناة
            # استخدام ON CONFLICT على (telegram_channel_id, telegram_message_id) لأنه المفتاح الفريد الصحيح
            result = conn.execute(
                text("""
                    INSERT INTO episodes (series_id, season, episode_number, 
                           telegram_message_id, telegram_channel_id)
                    VALUES (:sid, :season, :ep_num, :msg_id, :channel)
                    ON CONFLICT (telegram_channel_id, telegram_message_id) DO NOTHING
                """),
                {
                    "sid": series_id,
                    "season": season_num,
                    "ep_num": episode_num,
                    "msg_id": telegram_msg_id,
                    "channel": channel_id
                }
            )
            
            # التحقق من نجاح الإدراج (rowcount سيكون 1 إذا تم الإدراج، 0 إذا كان موجودًا مسبقًا)
            if result.rowcount == 0:
                print(f"⏭️ الحلقة موجودة مسبقاً: {name} - الموسم {season_num} الحلقة {episode_num} (msg_id: {telegram_msg_id}, channel: {channel_id})")
                return False  # لم تتم الإضافة (موجودة مسبقاً)
            
        type_arabic = "مسلسل" if content_type == 'series' else "فيلم"
        if content_type == 'movie':
            print(f"✅ تمت إضافة {type_arabic}: {name} - الجزء {season_num} من {channel_id}")
        else:
            print(f"✅ تمت إضافة {type_arabic}: {name} - الموسم {season_num} الحلقة {episode_num} من {channel_id}")
        return True
        
    except SQLAlchemyError as e:
        print(f"❌ خطأ في قاعدة البيانات: {e}")
        return False

def delete_from_database(message_id, channel_id=None):
    """حذف حلقة/جزء من قاعدة البيانات عند حذفها من القناة."""
    try:
        with engine.begin() as conn:
            # البحث عن الحلقة المراد حذفها (نحتاج channel_id لتحديدها بدقة)
            if channel_id:
                # إذا كان لدينا channel_id، نستخدمه مع message_id
                episode_result = conn.execute(
                    text("""
                        SELECT e.id, e.series_id, s.name, s.type, e.season, e.episode_number, e.telegram_channel_id
                        FROM episodes e
                        JOIN series s ON e.series_id = s.id
                        WHERE e.telegram_message_id = :msg_id AND e.telegram_channel_id = :channel
                    """),
                    {"msg_id": message_id, "channel": channel_id}
                ).fetchone()
            else:
                # للتوافق مع الإصدارات السابقة، نبحث بالرسالة فقط (قد يكون هناك عدة)
                episode_result = conn.execute(
                    text("""
                        SELECT e.id, e.series_id, s.name, s.type, e.season, e.episode_number, e.telegram_channel_id
                        FROM episodes e
                        JOIN series s ON e.series_id = s.id
                        WHERE e.telegram_message_id = :msg_id
                    """),
                    {"msg_id": message_id}
                ).fetchone()
            
            if not episode_result:
                print(f"⚠️ لم يتم العثور على الحلقة {message_id} في قاعدة البيانات")
                return False
            
            episode_id, series_id, name, content_type, season, episode_num, channel_id = episode_result
            
            # حذف الحلقة
            conn.execute(
                text("DELETE FROM episodes WHERE id = :episode_id"),
                {"episode_id": episode_id}
            )
            
            # التحقق مما إذا كان المسلسل/الفيلم لا يزال لديه حلقات أخرى
            remaining_episodes = conn.execute(
                text("SELECT COUNT(*) FROM episodes WHERE series_id = :series_id"),
                {"series_id": series_id}
            ).scalar()
            
            type_arabic = "مسلسل" if content_type == 'series' else "فيلم"
            
            if remaining_episodes == 0:
                # إذا لم يعد هناك حلقات، حذف المسلسل/الفيلم أيضًا
                conn.execute(
                    text("DELETE FROM series WHERE id = :series_id"),
                    {"series_id": series_id}
                )
                print(f"🗑️ تم حذف {type_arabic}: {name} بالكامل من {channel_id} (لا توجد حلقات/أجزاء متبقية)")
            else:
                if content_type == 'movie':
                    print(f"🗑️ تم حذف {type_arabic}: {name} - الجزء {season} من {channel_id}")
                else:
                    print(f"🗑️ تم حذف {type_arabic}: {name} - الموسم {season} الحلقة {episode_num} من {channel_id}")
            
            return True
            
    except SQLAlchemyError as e:
        print(f"❌ خطأ في حذف من قاعدة البيانات: {e}")
        return False

async def check_deleted_messages(client, channel):
    """التحقق من الرسائل المحذوفة في القناة."""
    channel_id = f"@{channel.username}" if hasattr(channel, 'username') and channel.username else str(channel.id)
    print(f"\n🔍 التحقق من الرسائل المحذوفة في {channel.title}...")
    
    try:
        with engine.connect() as conn:
            # جلب جميع معرفات الرسائل المخزنة في قاعدة البيانات لهذه القناة
            stored_messages = conn.execute(
                text("""
                    SELECT telegram_message_id FROM episodes 
                    WHERE telegram_channel_id = :channel_id 
                    ORDER BY telegram_message_id
                """),
                {"channel_id": channel_id}
            ).fetchall()
            
            stored_ids = [msg[0] for msg in stored_messages]
            
            if not stored_ids:
                print(f"   لا توجد رسائل مخزنة للقناة {channel.title}")
                return
            
            # جلب معرفات الرسائل الحالية في القناة
            current_ids = []
            async for message in client.iter_messages(channel, limit=1000):
                current_ids.append(message.id)
            
            # تحديد الرسائل المحذوفة (الموجودة في قاعدة البيانات ولكن ليس في القناة)
            deleted_ids = []
            for stored_id in stored_ids:
                if stored_id not in current_ids:
                    deleted_ids.append(stored_id)
            
            if deleted_ids:
                print(f"   تم العثور على {len(deleted_ids)} رسالة محذوفة في {channel.title}")
                for msg_id in deleted_ids:
                    print(f"   🗑️ معالجة الرسالة المحذوفة: {msg_id}")
                    # نمرر channel_id لتحديد الحلقة بدقة
                    delete_from_database(msg_id, channel_id)
            else:
                print(f"   ✅ لا توجد رسائل محذوفة في {channel.title}")
                
    except Exception as e:
        print(f"❌ خطأ في التحقق من الرسائل المحذوفة في {channel.title}: {e}")

# ==============================
# 5. استيراد المسلسلات القديمة
# ==============================
async def import_channel_history(client, channel):
    """استيراد جميع الرسائل القديمة من القناة بأقدمها أولاً."""
    print(f"\n" + "="*50)
    print(f"📂 بدء استيراد المحتوى القديم من القناة: {channel.title}")
    print("="*50)
    
    imported_count = 0
    skipped_count = 0
    error_count = 0
    
    try:
        # جمع جميع الرسائل أولاً
        all_messages = []
        async for message in client.iter_messages(channel, limit=1000):
            all_messages.append(message)
        
        # عكس الترتيب للحصول على الأقدم أولاً
        all_messages.reverse()
        
        print(f"📊 تم جمع {len(all_messages)} رسالة للاستيراد...")
        
        for message in all_messages:
            if not message.text:
                continue
            
            try:
                name, content_type, season_num, episode_num = parse_content_info(message.text)
                if name and content_type and episode_num:
                    channel_id = f"@{message.chat.username}" if hasattr(message.chat, 'username') and message.chat.username else str(message.chat.id)
                    if save_to_database(name, content_type, season_num, episode_num, message.id, channel_id):
                        imported_count += 1
                    else:
                        skipped_count += 1
                else:
                    print(f"⚠️ لم يتم تحليل الرسالة: {message.text[:50]}...")
                    error_count += 1
            except Exception as e:
                print(f"❌ خطأ في معالجة الرسالة {message.id}: {e}")
                error_count += 1
        
        print("="*50)
        print(f"✅ اكتمل استيراد القناة {channel.title}!")
        print(f"   - تم استيراد: {imported_count} عنصر جديد")
        print(f"   - تم تخطي: {skipped_count} عنصر (موجود مسبقاً)")
        print(f"   - فشل تحليل: {error_count} رسالة")
        print("="*50)
        
    except Exception as e:
        print(f"❌ خطأ أثناء استيراد التاريخ من {channel.title}: {e}")

# ==============================
# 6. الدالة الرئيسية لمراقبة القنوات
# ==============================
async def monitor_channels():
    """الدالة الرئيسية لمراقبة عدة قنوات."""
    print("="*50)
    print(f"🔍 بدء مراقبة {len(CHANNEL_LIST)} قناة:")
    for i, chan in enumerate(CHANNEL_LIST, 1):
        print(f"   {i}. {chan}")
    print("="*50)
    
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    
    try:
        await client.start()
        print("✅ تم الاتصال بـ Telegram بنجاح.")
        
        # الحصول على كيانات جميع القنوات
        channel_entities = []
        for channel_input in CHANNEL_LIST:
            try:
                channel = await get_channel_entity(client, channel_input)
                if channel:
                    channel_entities.append(channel)
                    print(f"✅ تمت إضافة القناة: {channel.title}")
                else:
                    print(f"❌ فشل إضافة القناة: {channel_input}")
            except Exception as e:
                print(f"❌ خطأ في إضافة القناة {channel_input}: {e}")
        
        if not channel_entities:
            print("❌ لم يتم العثور على أي قناة صالحة!")
            return
        
        # استيراد المحتوى القديم إذا كان مفعلاً
        if IMPORT_HISTORY:
            for channel in channel_entities:
                await import_channel_history(client, channel)
        else:
            print("⚠️ استيراد المحتوى القديم معطل.")
        
        # التحقق من الرسائل المحذوفة إذا كان مفعلاً
        if CHECK_DELETED_MESSAGES:
            for channel in channel_entities:
                await check_deleted_messages(client, channel)
        
        # مراقبة الرسائل الجديدة من جميع القنوات
        @client.on(events.NewMessage(chats=channel_entities))
        async def handler(event):
            message = event.message
            if message.text:
                channel_name = f"@{message.chat.username}" if hasattr(message.chat, 'username') and message.chat.username else message.chat.title
                print(f"📥 رسالة جديدة من {channel_name}: {message.text[:50]}...")
                
                name, content_type, season_num, episode_num = parse_content_info(message.text)
                if name and content_type and episode_num:
                    type_arabic = "مسلسل" if content_type == 'series' else "فيلم"
                    if content_type == 'movie':
                        print(f"   تم التعرف على {type_arabic}: {name} - الجزء {season_num}")
                    else:
                        print(f"   تم التعرف على {type_arabic}: {name} - الموسم {season_num} الحلقة {episode_num}")
                    
                    # إضافة معرف القناة في قاعدة البيانات
                    channel_id = f"@{message.chat.username}" if hasattr(message.chat, 'username') and message.chat.username else str(message.chat.id)
                    save_to_database(name, content_type, season_num, episode_num, message.id, channel_id)
        
        # مراقبة حذف الرسائل من جميع القنوات
        @client.on(events.MessageDeleted(chats=channel_entities))
        async def delete_handler(event):
            # لسنا متأكدين من القناة التي حدث فيها الحذف، لذا نستخدم الدالة القديمة (بدون channel_id)
            # ولكن يمكن تحسين ذلك إذا أمكن الحصول على القناة من الحدث
            for msg_id in event.deleted_ids:
                print(f"🗑️ تم حذف رسالة: {msg_id}")
                # نمرر None للـ channel_id، وستبحث الدالة عن أي حلقة بهذا المعرف
                delete_from_database(msg_id, None)
        
        print("\n🎯 جاهز لمراقبة القنوات:")
        for i, chan in enumerate(channel_entities, 1):
            print(f"   {i}. {chan.title}")
        print("   (اضغط Ctrl+C في Railway لإيقاف المراقبة)\n")
        
        await client.run_until_disconnected()
        
    except Exception as e:
        print(f"❌ خطأ في تشغيل الـ Worker: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.disconnect()
        print("🛑 تم إيقاف مراقبة القنوات.")

# ==============================
# 7. نقطة دخول البرنامج
# ==============================
if __name__ == "__main__":
    print("🚀 بدء تشغيل Worker لمراقبة قنوات المسلسلات والأفلام...")
    print(f"📡 عدد القنوات المحددة: {len(CHANNEL_LIST)}")
    asyncio.run(monitor_channels())
