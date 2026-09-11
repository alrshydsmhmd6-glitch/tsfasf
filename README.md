# Telegram Group Guard

بوت عربي لحماية وإدارة مجموعات Telegram، مبني على Python و`python-telegram-bot` مع قاعدة بيانات SQLite.

## التشغيل

1. ثبّت Python 3.10 أو أحدث.
2. أنشئ بيئة افتراضية وثبّت الحزم:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. انسخ `.env.example` إلى `.env`:

   ```bash
   cp .env.example .env
   ```

4. ضع توكن BotFather في `BOT_TOKEN` ومعرف المالك الرقمي في `ADMIN_ID`.
   يمكن وضع أكثر من معرف مالك بفصلها بفواصل.
5. أضف البوت إلى المجموعة كمشرف، وامنحه صلاحية حذف الرسائل وحظر الأعضاء وتقييدهم.
6. شغّل:

   ```bash
   python admin_id_token_bot.py
   ```

## أهم الأوامر

`/kick`، `/ban`، `/unban`، `/tban`، `/mute`، `/unmute`، `/warn`، `/unwarn`، `/warns`،
`/del`، `/purge`، `/addword`، `/delword`، `/wordlist`، `/setflood`،
`/setcaptcha`، `/setcaptchatime`، `/lock`، `/unlock`، `/promote`، `/demote`،
`/adminlist`، `/setrules`، `/rules`، `/setwelcome`، `/settings`، `/log`، `/history`.

الأوامر التي تستهدف عضوًا تعمل بالرد على رسالته أو باستخدام المعرف الرقمي.
الرد على الرسالة هو الطريقة الأكثر موثوقية؛ Telegram Bot API لا يوفر بحثًا عامًا عن
أعضاء المجموعة بواسطة `@username`.

## ملاحظات أمان

- لا تضع التوكن الحقيقي داخل الكود أو ترسله في المحادثة.
- ملف `.env` مستثنى من Git بواسطة `.gitignore`.
- Telegram لا يوفر تاريخ إنشاء الحساب بشكل موثوق عبر Bot API؛ لذلك لم يتم تنفيذ فلتر عمر الحساب.
- يجب أن يكون البوت مشرفًا مع الصلاحيات المناسبة حتى تعمل العقوبات والحذف والتحقق.