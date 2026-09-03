# پلن معماری چند-اینستنسی sing-box (Multi-Instance Leaf Architecture)

## هدف

جایگزینی مدل «یک اینستنس sing-box با outbound-set متغیر» با مدل «یک اینستنس اصلی ثابت + N اینستنس leaf مستقل»، به‌طوری‌که:

- تغییر سرور فعال دیگر نیازی به ری‌استارت اینستنس اصلی نداشته باشد
- تشخیص packet loss/شکست به‌جای batch رانویی هر چند دقیقه، در حد چند ثانیه انجام شود
- ری‌استارت (وقتی لازم است) فقط روی یک leaf بدون ترافیک زنده اتفاق بیفتد، نه روی مسیر اصلی

این سند عمداً مرحله‌ای است؛ هر فاز باید مستقل تست، deploy، و در صورت نیاز rollback شود — با همان انضباطی که در `ARCHITECTURE_IMPLEMENTATION_PLAN.md` برای فازبندی Pool A/B استفاده شده.

## ارتباط با پلن هم‌زمانی Pool A/B

این پلن **مستقل از، اما وابسته به** `ARCHITECTURE_IMPLEMENTATION_PLAN.md` است:

- `ApplyCoordinator` در آن پلن دقیقاً همان جزئی است که در فاز پنج این سند باید متد جدید (leaf replacement) را صدا بزند، نه `rebuild_and_apply()` فعلی را.
- توصیه می‌شود فازهای یک تا سه‌ی پلن Pool A/B (ایمن‌سازی round status، snapshot/generation، جداسازی worker/committer/coordinator) **قبل از فاز پنج این سند** تکمیل شده باشند؛ وگرنه دو تغییر معماری هم‌زمان روی coordinator یکسان race ایجاد می‌کنند.
- تا زمانی‌که این ترتیب رعایت نشود، فازهای صفر تا چهار این سند (که مستقل و ایزوله‌اند) بدون مشکل قابل انجام‌اند.

## اصول طراحی (برگرفته از premortem)

۱. **health-monitor باید watchdog مستقل خودش را داشته باشد.** اگر این پروسه هنگ کند، اینستنس اصلی نباید کورکورانه به یک leaf مرده ترافیک بفرستد.

۲. **nftables bypass (routing_mark/exclude_uid) باید از فاز یک، زیر بار پیوسته (نه فقط تست کوتاه) تأیید شود.** چون بعد از این، هر leaf یک پروسهٔ دائمی است، نه یک تست موقت.

۳. **حلقهٔ replacement باید circuit breaker مستقل از circuit breaker فعلی restart داشته باشد.**

۴. **هیسترزیس سریع (برای ثانیه) باید از هیسترزیس کند فعلی (برای دقیقه) کاملاً جدا تعریف شود** تا از flapping جلوگیری شود.

۵. **جایگزینی leaf باید readiness-gated باشد** — یعنی leaf جدید فقط بعد از یک health probe موفق، وارد rotation شود، نه بلافاصله بعد از bind شدن پورت.

۶. **هیچ فازی نباید تولید را لمس کند مگر با موازی (shadow) اجرا شده و مقایسه شده باشد.**

---

# فاز صفر: Baseline و بودجهٔ منابع

## هدف

قبل از اضافه کردن N پروسهٔ دائمی، بفهمیم پای واقعاً چقدر ظرفیت دارد.

## کارها

- [ ] اندازه‌گیری CPU/RAM حین یک اینستنس فعلی (idle و زیر بار)
- [ ] بنچمارک: N=2 و N=3 اینستنس leaf شبیه‌سازی‌شده به مدت ۲۴ ساعت، ثبت CPU/RAM/bandwidth baseline
- [ ] اندازه‌گیری هزینهٔ نگه‌داشتن تونل‌های QUIC-based (Hysteria2/TUIC) زنده به‌صورت idle
- [ ] ثبت پهنای‌باند مصرفی health-probeهای پیشنهادی (فرضی) روی آپلینک DSL فعلی
- [ ] تعیین N نهایی (پیش‌فرض پیشنهادی: ۲ برای شروع، نه ۳)

## معیار پذیرش

- عدد مشخصی برای «سقف N که پای تحملش را دارد» ثبت شود.
- هیچ تغییری در کد production داده نشود.

## Commit پیشنهادی

```text
chore: record Pi resource baseline for multi-instance leaf design
```

---

# فاز یک: Leaf process — پروتوتایپ ایزوله

## هدف

ساخت و تست یک اینستنس leaf مستقل، کاملاً جدا از مسیر تولید، بدون هیچ اتصال به main یا Pool B واقعی.

## کارها

- [ ] ماژول جدید `totalray/leaf.py`: build کانفیگ leaf (یک inbound SOCKS روی پورت ثابت + یک outbound با exclude_uid/routing_mark دائمی)
- [ ] الگوی exclude_uid را از `_dnsmasq_uid()` (که قبلاً برای DNS fallback کار کرد) برای uid مخصوص leaf تکرار کن
- [ ] تست دستی: leaf را ۲۴ ساعت روشن نگه دار، بررسی کن که هیچ double-hop یا CPU spike از auto_redirect اتفاق نیفتد
- [ ] unit test: `build_leaf_config` خروجی معتبر sing-box تولید کند (`sing-box check`)
- [ ] integration test: leaf روشن شود، از طریق SOCKS محلی‌اش به اینترنت وصل شود، و ترافیکش وارد nftables auto_redirect اصلی نشود (بررسی با شمارندهٔ nftables یا لاگ)

## معیار پذیرش

- بعد از ۲۴ ساعت اجرای پیوسته، هیچ افزایش غیرعادی CPU یا route جدید در main دیده نشود.
- تست خودکار bypass بودن leaf (نه main) پاس شود.
- هیچ تغییری در `/etc/sing-box/config.json` تولید داده نشود.

## Commit پیشنهادی

```text
feat: add isolated leaf sing-box process prototype
```

---

# فاز دو: Main instance — selector روی loopback (parity test)

## هدف

اثبات این‌که سوییچ Clash API بین اعضای local loopback، همان رفتار پایدار سوییچ فعلی (بین remote outboundها) را دارد — قبل از این‌که به چند leaf متکی شویم.

## کارها

- [ ] یک outbound موقت در main اضافه کن که به یک پورت لوپ‌بک ثابت (که یک leaf تک رویش گوش می‌دهد) اشاره می‌کند
- [ ] با یک leaf واحد، چند بار سوییچ انجام بده و بررسی کن `interrupt_exist_connections: false` هنوز صادق است (کانکشن‌های زنده قطع نمی‌شوند)
- [ ] اندازه‌گیری round-trip اضافه‌شده از این هاپ اضافی (main → loopback → leaf → اینترنت) در مقابل حالت فعلی (main → مستقیم اینترنت)
- [ ] این فاز را کاملاً موازی (shadow) با مسیر تولید فعلی اجرا کن — main تولید همچنان دست‌نخورده بماند

## معیار پذیرش

- سوییچ بین یک outbound remote و یک outbound loopback هیچ قطعی کانکشنی ایجاد نکند.
- افزایش latency ناشی از هاپ اضافی داخلی، قابل قبول باشد (پیشنهاد: زیر ۵ میلی‌ثانیه، چون فقط loopback است)
- rollback ساده باشد: صرفاً حذف outbound موقت

## Commit پیشنهادی

```text
feat: prototype main selector over loopback leaf (shadow only)
```

---

# فاز سه: Health monitor سریع + flap guard + watchdog

## هدف

جایگزین کردن پراب batch کند فعلی با یک ناظر سریع، مستقل، و خودمحافظ برای leafها.

## کارها

- [ ] ماژول جدید `totalray/leaf_monitor.py`، جدا از `live_monitor.py` فعلی
- [ ] پراب هر leaf هر ۳ تا ۵ ثانیه (Clash API `/proxies/{name}/delay` یا TCP connect سبک)
- [ ] هیسترزیس مستقل: مثلاً ۲ شکست پیاپی در بازهٔ ۱۵ ثانیه (نه ۲ دقیقه)
- [ ] flap guard: حداقل فاصلهٔ زمانی بین دو سوییچ متوالی (مثلاً cooldown ۳۰ ثانیه‌ای) برای جلوگیری از نوسان سریع
- [ ] self-watchdog: heartbeat file که یک ناظر بیرونی (systemd watchdog یا یک thread جدا در `main.py`) چک می‌کند؛ اگر heartbeat قدیمی شد، leaf_monitor ری‌استارت شود
- [ ] unit test: شبیه‌سازی packet loss متناوب (نه کامل) و بررسی رفتار hysteresis/flap guard
- [ ] unit test: قطع شدن خود leaf_monitor و تأیید این‌که watchdog تشخیص می‌دهد

## معیار پذیرش

- شکست کامل leaf در کمتر از ۱۰ ثانیه شناسایی و failover شود.
- یک leaf با کیفیت نوسانی/مرزی باعث بیش از ۱ سوییچ در ۳۰ ثانیه نشود.
- قطع شدن خود leaf_monitor حداکثر ظرف N ثانیه شناسایی و لاگ شود.

## Commit پیشنهادی

```text
feat: add fast leaf health monitor with flap guard and watchdog
```

---

# فاز چهار: Leaf lifecycle orchestrator (replacement + circuit breaker)

## هدف

مدیریت جایگزینی خودکار leaf خراب با کاندید بعدی، بدون race condition و بدون چرخهٔ بی‌نهایت.

## کارها

- [ ] ماژول جدید `totalray/leaf_orchestrator.py`: مسئول start/stop/replace هر leaf روی پورت ثابتش
- [ ] پروتکل handoff امن: leaf جدید فقط بعد از یک health probe موفق (نه صرفاً bind شدن پورت) وارد rotation می‌شود
- [ ] در حین جایگزینی، اگر main در حال حاضر به همان پورت متصل است، اول به یک leaf دیگر (اگر سالم است) سوییچ کن، بعد leaf را ری‌استارت کن (هیچ‌وقت main را به پورتی که در حال ری‌استارت است نگه ندار)
- [ ] circuit breaker مستقل: اگر یک leaf بیش از N بار در بازهٔ M دقیقه جایگزین شود و بازهم fail کند، آن slot را تا بررسی دستی متوقف کن و در دیتابیس `removed` علامت بزن
- [ ] یکپارچه‌سازی با soft-delete موجود در `db.py` (بدون تغییر منطق فعلی امتیازدهی)
- [ ] test: race condition — همزمان health probe و replacement روی یک پورت
- [ ] test: circuit breaker trip بعد از N شکست پیاپی جایگزینی

## معیار پذیرش

- هیچ پنجرهٔ زمانی‌ای وجود نداشته باشد که main به یک پورت bind-نشده اشاره کند.
- circuit breaker این حلقه، مستقل از circuit breaker ری‌استارت فعلی trip شود و جدا لاگ شود.
- تست race condition به‌صورت خودکار و تکرارپذیر پاس شود.

## Commit پیشنهادی

```text
feat: add leaf replacement orchestrator with independent circuit breaker
```

---

# فاز پنج: یکپارچه‌سازی با خط لولهٔ Pool A/B

## هدف

وصل کردن خروجی Pool B (کاندیدهای verified) به صف «بهترین کاندید بعدی» leaf orchestrator، به‌جای diff کردن کل outbound-set.

## پیش‌نیاز

تکمیل فازهای یک تا سه‌ی `ARCHITECTURE_IMPLEMENTATION_PLAN.md` (به دلیل توضیح داده‌شده در بخش «ارتباط با پلن هم‌زمانی Pool A/B»).

## کارها

- [ ] `ApplyCoordinator` به‌جای `rebuild_and_apply()` کامل، متد جدید `leaf_orchestrator.request_replacement(slot, candidate)` را صدا بزند
- [ ] معیار انتخاب کاندید بعدی: بالاترین امتیاز Pool B که در حال حاضر به هیچ leaf فعالی assign نشده
- [ ] حذف مسیر قدیمی diff-تگ/restart کامل برای این حالت خاص (نگه‌داشتنش فقط برای موارد نادر مثل تغییر rule-set یا آپدیت main)
- [ ] test: سناریوی end-to-end — یک کانفیگ در Pool A تست می‌شود، به Pool B می‌رود، توسط orchestrator به یک leaf آزاد assign می‌شود، health probe می‌شود، و در نهایت main بهش سوییچ می‌کند

## معیار پذیرش

- یک کانفیگ جدید سالم در Pool B، بدون هیچ ری‌استارت main، وارد rotation شود.
- مسیر قدیمی restart-کامل فقط برای تغییرات واقعی main (rule-set، نسخهٔ sing-box) باقی بماند.

## Commit پیشنهادی

```text
feat: wire pool B promotion into leaf replacement queue
```

---

# فاز شش: Observability

## هدف

گسترش `totalray status` برای نمایش وضعیت هر leaf، مشابه سبک فاز observability پلن دیگر.

## کارها

- [ ] هر رویداد leaf_monitor/orchestrator با یک `leaf_slot_id` مشترک لاگ شود تا correlate کردن راحت باشد
- [ ] افزودن جدول به خروجی status:

```text
Leaf 1 (active):  slot=30001  server=... delay=142ms   since=14:32:07
Leaf 2 (standby): slot=30002  server=... delay=198ms   since=14:20:11
Leaf 3 (replace): slot=30003  status=warming up...
```

- [ ] نمایش تعداد سوییچ‌ها و trip‌های circuit breaker در ۲۴ ساعت اخیر
- [ ] خروجی `--json` هم شامل این فیلدها شود

## معیار پذیرش

- از روی یک incident، بتوان ظرف چند ثانیه فهمید کدام leaf چه موقع و چرا سوییچ کرده.

## Commit پیشنهادی

```text
feat: expose leaf pool state in totalray status
```

---

# فاز هفت: rollout مرحله‌ای روی Pi

## کارها

1. [ ] اجرای shadow mode: leafها روشن باشند و مانیتور شوند، ولی main همچنان از مسیر قدیمی استفاده کند (مقایسهٔ delay/uptime بدون ریسک)
2. [ ] مقایسهٔ حداقل ۴۸ ساعت داده‌ی shadow با رفتار فعلی
3. [ ] cutover با N=2 (فعال + آماده)، مسیر قدیمی به‌عنوان fallback دستی نگه داشته شود
4. [ ] مانیتور حداقل یک هفته: تعداد سوییچ، circuit breaker trips، CPU/RAM
5. [ ] در صورت پایداری، افزایش به N=3
6. [ ] فقط بعد از پایداری کامل N=3، مسیر قدیمی restart-کامل به‌عنوان fallback حذف شود (نه زودتر)

## معیار rollback

اگر هرکدام از این‌ها رخ داد، فوراً به مسیر قدیمی برگرد:

- بیش از ۱ trip از circuit breaker جدید در ساعت
- CPU sustained بالای آستانهٔ تعیین‌شده در فاز صفر
- هر گزارش قطعی کاربر که با زمان یک leaf switch هم‌زمان است

## Commit پیشنهادی

```text
chore: staged rollout plan for multi-instance leaf architecture
```

---

# تصمیم معماری

معماری leaf چندگانه تأیید می‌شود، با این پیش‌شرط‌های صریح برگرفته از premortem:

- health-monitor باید watchdog خودش را داشته باشد (فاز سه)
- nftables bypass باید زیر بار پیوسته تست شود، نه فقط تست کوتاه (فاز یک)
- حلقهٔ replacement باید circuit breaker مستقل داشته باشد (فاز چهار)
- تا قبل از تکمیل فاز سه‌ی `ARCHITECTURE_IMPLEMENTATION_PLAN.md`، فاز پنج این سند شروع نشود

هیچ فازی نباید مسیر production فعلی را مستقیماً جایگزین کند؛ گذار باید از طریق shadow mode و rollout تدریجی (فاز هفت) انجام شود.
