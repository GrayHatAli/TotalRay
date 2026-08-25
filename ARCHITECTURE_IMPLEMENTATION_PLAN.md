# TotalRay Architecture Implementation Plan

## هدف

این سند برنامهٔ مرحله‌ای برای نزدیک‌کردن TotalRay به معماری پیشنهادی دیاگرام `diagram.png` است.

هدف اصلی:

- مستقل‌شدن چرخهٔ سلامت Pool B از scan طولانی Pool A
- جلوگیری از race condition بین تست‌ها، دیتابیس و sing-box
- جلوگیری از اعمال نتایج قدیمی روی state جدید
- کاهش restartهای غیرضروری sing-box
- قابل‌مشاهده‌کردن وضعیت واقعی هر job در `totalray status`

این برنامه عمداً مرحله‌ای است تا هر فاز قابل تست و rollback باشد.

---

## وضعیت فعلی و فاصله با معماری هدف

### مواردی که اکنون وجود دارند

- SQLite با یک `Database._lock` برای محافظت از عملیات دیتابیس
- جدول‌های `subscriptions`، `configs` و `test_log`
- دو pool منطقی در ستون `configs.pool`
- soft delete با `configs.removed`
- تست chunk-based با چند inbound موقت sing-box
- Pool A و Pool B به‌عنوان jobهای جدا در APScheduler
- `Manager._apply_lock` برای rebuild/restart sing-box
- status progress در `round_status.json`
- selector زنده برای تغییر config بدون restart در بعضی شرایط

### شکاف‌های اصلی

- Pool A و بعضی jobهای دیگر هنوز از `_job_lock` مشترک استفاده می‌کنند.
- Pool A و Pool B snapshot یا generation ندارند.
- نتیجهٔ تست مشخص نمی‌کند بر اساس کدام snapshot گرفته شده است.
- `round_status.json` برای read-modify-write قفل مستقل ندارد.
- worker تست، ثبت نتیجه و apply کردن sing-box را بیش از حد به هم متصل کرده‌اند.
- `rebuild_and_apply()` ممکن است از چند مسیر مختلف فراخوانی شود.
- Pool A هنوز می‌تواند صدها هزار config را ساعت‌ها تست کند.
- وضعیت‌های `queued`، `skipped` و علت انتظار باید دقیق‌تر مدل شوند.

---

# اصول معماری هدف

## اصل ۱: worker تست نباید مالک state نهایی باشد

Worker فقط باید:

1. یک snapshot از candidateها دریافت کند؛
2. تست را اجرا کند؛
3. نتیجهٔ خام و metadata آن را برگرداند.

ثبت نتیجه در دیتابیس و تصمیم برای rebuild باید توسط coordinator انجام شود.

## اصل ۲: دو قفل متفاوت داشته باشیم

### State/DB lock

برای transactionهای دیتابیس و تغییر pool، score و soft delete.

### Apply lock

برای نوشتن config و restart/reload sing-box.

این دو قفل نباید با یک lock عمومی جایگزین شوند.

## اصل ۳: نتیجهٔ قدیمی نباید state جدید را overwrite کند

هر round باید این metadata را داشته باشد:

- `round_id`
- `pool`
- `snapshot_generation`
- `started_at`
- `finished_at`
- `worker_id` یا `job_id`

## اصل ۴: restart sing-box آخرین مرحله باشد

تغییر selector بدون restart اولویت دارد. Restart فقط زمانی انجام شود که مجموعهٔ outboundها یا routeها واقعاً تغییر کرده باشد.

## اصل ۵: Pool B کوچک، سریع و مستقل است

Pool B نباید برای اجرای health check خود منتظر scan طولانی Pool A بماند؛ اما commit نتیجه‌ها و apply sing-box باید همچنان کنترل‌شده باشد.

---

# فاز صفر: آماده‌سازی و baseline

## هدف

ثبت وضعیت فعلی قبل از تغییرات و ایجاد امکان مقایسه و rollback.

## کارها

- [ ] ثبت commit فعلی workspace و Raspberry Pi
- [ ] ثبت نسخهٔ sing-box و Python
- [ ] ثبت اندازهٔ Pool A و Pool B
- [ ] ثبت زمان متوسط هر chunk
- [ ] ثبت تعداد restartهای sing-box در ۲۴ ساعت
- [ ] ثبت تعداد failoverهای live monitor
- [ ] ثبت نمونهٔ `totalray status`
- [ ] اطمینان از clean بودن تغییرات ناخواستهٔ Git
- [ ] افزودن یا تکمیل تست‌های فعلی scheduler و live monitor

## فایل‌های محتمل

- `README.md`
- `tests/`
- `totalray/scheduler.py`
- `totalray/db.py`
- `totalray/tester.py`

## معیار پذیرش

- یک baseline قابل تکرار از وضعیت Pi وجود داشته باشد.
- هیچ فایل local ناشناخته قبل از شروع فاز بعدی overwrite نشود.
- rollback به commit baseline ممکن باشد.

## Commit پیشنهادی

```text
chore: record concurrency architecture baseline
```

---

# فاز یک: ایمن‌سازی round status

## هدف

رفع race condition در `round_status.json` و نمایش دقیق state jobها.

## کارها

- [ ] ایجاد `RoundStateStore` مستقل
- [ ] افزودن lock داخلی برای read-modify-write
- [ ] حفظ write اتمیک با temporary file و `os.replace`
- [ ] اضافه‌کردن فیلدهای زیر:
  - `state`: `idle`, `running`, `queued`, `skipped`, `failed`
  - `round_id`
  - `started_at`
  - `finished_at`
  - `last_error`
  - `items_total`
  - `items_processed`
  - `items_ok`
  - `items_failed`
  - `blocked_by`
- [ ] حذف stateهای stale هنگام startup با ثبت `recovered_at`
- [ ] نمایش علت انتظار در status
- [ ] جلوگیری از پاک‌شدن تغییر Pool A هنگام نوشتن Pool B

## پیشنهاد فنی

به‌جای پراکنده‌کردن منطق در `Manager`، فایل جدیدی ایجاد شود:

```text
totalray/round_state.py
```

API پیشنهادی:

```python
state_store.start("pool_a", round_id, total=...)
state_store.progress("pool_a", processed=..., ok=..., failed=...)
state_store.finish("pool_a", success=True)
state_store.skip("pool_b", reason="pool_a_commit")
state_store.snapshot()
```

## معیار پذیرش

- اجرای هم‌زمان updateهای status هیچ field دیگری را overwrite نکند.
- پس از restart، هیچ jobی به‌اشتباه `running` باقی نماند.
- `totalray status` وضعیت `running`, `queued`, `skipped` و `failed` را جدا نشان دهد.
- تست unit برای دو writer هم‌زمان وجود داشته باشد.

## Commit پیشنهادی

```text
fix: make round status updates concurrency safe
```

---

# فاز دو: مدل‌کردن generation و snapshot

## هدف

جلوگیری از اعمال نتیجهٔ تستی که بر اساس state قدیمی گرفته شده است.

## تغییر دیتابیس

یک migration به `totalray/db.py` اضافه شود.

### جدول جدید پیشنهادی

```sql
CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

کلیدهای اولیه:

```text
db_generation
pool_a_generation
pool_b_generation
```

### جدول round پیشنهادی

```sql
CREATE TABLE IF NOT EXISTS test_rounds (
    id TEXT PRIMARY KEY,
    pool TEXT NOT NULL,
    snapshot_generation INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state TEXT NOT NULL,
    total INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    error TEXT
);
```

## کارها

- [ ] افزودن migration بدون از دست‌دادن دادهٔ فعلی
- [ ] ایجاد generation جدید هنگام تغییر membership یا import subscription
- [ ] ثبت generation در ابتدای هر round
- [ ] ذخیرهٔ snapshot candidateها همراه round
- [ ] بررسی معتبر بودن config قبل از commit نتیجه
- [ ] تعریف رفتار stale result:
  - نتیجهٔ config حذف‌شده نادیده گرفته شود
  - نتیجهٔ config با pool متفاوت نادیده گرفته یا merge شود
  - نتیجهٔ معتبر برای config بدون تغییر اعمال شود
- [ ] ثبت تعداد stale resultها در `test_rounds`

## معیار پذیرش

- دو round هم‌زمان نمی‌توانند نتیجهٔ یکدیگر را کورکورانه overwrite کنند.
- اگر config بین snapshot و commit تغییر کند، نتیجهٔ آن در log به‌عنوان stale ثبت شود.
- migration روی دیتابیس فعلی Pi بدون حذف configها موفق باشد.

## Commit پیشنهادی

```text
feat: add test round generations and stale result protection
```

---

# فاز سه: جداسازی worker، commit و apply

## هدف

تبدیل مسیر فعلی به سه لایهٔ مستقل:

```text
Test Worker -> Result Committer -> Apply Coordinator
```

## ساختار پیشنهادی فایل‌ها

```text
totalray/
├── workers.py
├── coordinator.py
├── round_state.py
├── db.py
├── tester.py
└── builder.py
```

## Test Worker

- [ ] دریافت snapshot immutable
- [ ] اجرای chunkها با sing-box موقت
- [ ] برگرداندن نتیجهٔ خام
- [ ] ننوشتن مستقیم در دیتابیس
- [ ] نخواندن یا تغییر `/etc/sing-box/config.json`

## Result Committer

- [ ] گرفتن state/DB lock
- [ ] اعتبارسنجی generation و membership
- [ ] اجرای transaction ثبت score/pool/removed
- [ ] ثبت `test_log` و `test_rounds`
- [ ] تولید event تغییر membership

## Apply Coordinator

- [ ] گرفتن apply lock
- [ ] خواندن state نهایی دیتابیس، نه نتیجهٔ قدیمی worker
- [ ] مقایسهٔ outbound tag set با state قبلی
- [ ] تغییر selector بدون restart در صورت امکان
- [ ] restart sing-box فقط برای تغییر واقعی config/route
- [ ] ثبت موفقیت یا شکست apply

## معیار پذیرش

- هیچ worker مستقیماً `builder.rebuild_and_apply()` را فراخوانی نکند.
- هر apply بر اساس state نهایی دیتابیس انجام شود.
- هم‌زمانی workerها باعث نوشتن هم‌زمان فایل sing-box نشود.
- هر failure در commit یا apply قابل مشاهده و قابل retry باشد.

## Commit پیشنهادی

```text
refactor: separate test workers from state commit and apply
```

---

# فاز چهار: اجرای واقعی Pool A و Pool B به‌صورت هم‌زمان

## هدف

Pool B هر دو دقیقه مستقل از scan چندساعتهٔ Pool A اجرا شود.

## کارها

- [ ] حذف وابستگی Pool A و Pool B به `_job_lock` مشترک
- [ ] نگه‌داشتن lock اختصاصی برای subscription update
- [ ] ایجاد job runner مستقل برای Pool A
- [ ] ایجاد job runner مستقل برای Pool B
- [ ] محدودکردن تعداد round هم‌زمان برای هر pool به یک مورد
- [ ] استفاده از `round_id` برای جلوگیری از commit دوباره
- [ ] اعمال backpressure هنگام پرشدن صف نتیجه‌ها
- [ ] تعریف اولویت Pool B بالاتر از Pool A
- [ ] جلوگیری از اجرای هم‌زمان rules update با apply
- [ ] حفظ traffic sampling به‌عنوان job best-effort

## سیاست پیشنهادی صف

```text
Priority 1: Pool B health check
Priority 2: apply/selector update
Priority 3: subscription update
Priority 4: Pool A scan
Priority 5: traffic sample
```

## نکتهٔ مهم

تست‌های Pool A و Pool B می‌توانند هم‌زمان از instanceهای temporary sing-box استفاده کنند، اما این موارد باید serialized بمانند:

- commit دیتابیس
- membership update
- نوشتن config اصلی
- restart sing-box

## معیار پذیرش

- Pool B در زمان اجرای Pool A واقعاً اجرا شود.
- یک Pool B round بیش از یک بار هم‌زمان اجرا نشود.
- Pool B بتواند config خراب active را demote کند.
- Pool A بتواند به‌صورت تدریجی config سالم را promote کند.
- هیچ crash-loop یا stale route جدیدی در sing-box ایجاد نشود.

## Commit پیشنهادی

```text
feat: run pool A and pool B test workers independently
```

---

# فاز پنج: بهینه‌سازی Pool A

## هدف

جلوگیری از scan بی‌پایان و کاهش زمان رسیدن configهای سالم به Pool B.

## کارها

- [ ] افزودن cursor پایدار برای ادامهٔ scan
- [ ] اولویت‌دادن به configهای جدید
- [ ] اولویت‌دادن به configهای قبلاً سالم
- [ ] محدودکردن تعداد candidateهای هر round
- [ ] توقف زودهنگام پس از رسیدن به حد کافی config سالم
- [ ] جداکردن `untested`, `retry`, `degraded` و `near-removal`
- [ ] backoff برای configهایی که چند بار متوالی شکست خورده‌اند
- [ ] جلوگیری از retest فوری configهای تازه تست‌شده
- [ ] افزودن تنظیمات:
  - `pool_a.max_items_per_round`
  - `pool_a.target_verified_count`
  - `pool_a.cursor_enabled`
  - `pool_a.retry_backoff_minutes`

## معیار پذیرش

- Pool A در یک round مجبور به تست همهٔ ۲۲۷ هزار config نباشد.
- configهای جدید و promising سریع‌تر بررسی شوند.
- زمان round و تعداد configهای بررسی‌شده در status نمایش داده شود.
- Pool B بدون وابستگی به پایان Pool A سالم بماند.

## Commit پیشنهادی

```text
feat: prioritize and bound pool A scanning
```

---

# فاز شش: کنترل restart و health sing-box

## هدف

جلوگیری از تکرار خطای زیر:

```text
append ipv4 loopback route: file exists
```

## کارها

- [ ] متمرکزکردن تمام restartها در `ApplyCoordinator`
- [ ] شمارش restartها و failureهای sing-box
- [ ] بررسی health API پس از restart
- [ ] retry محدود با فاصلهٔ مناسب
- [ ] جلوگیری از restart پشت‌سرهم
- [ ] circuit breaker برای restartهای ناموفق
- [ ] ثبت دلیل هر restart:
  - membership changed
  - routes changed
  - rules updated
  - forced recovery
- [ ] عدم restart برای تغییر صرفاً latency/ranking
- [ ] نمایش آخرین restart و علت آن در status

## معیار پذیرش

- یک Pool round معمولی فقط با تغییر ranking باعث restart نشود.
- هر restart با health check `127.0.0.1:9090` تأیید شود.
- پس از failure، سیستم وارد restart loop بی‌نهایت نشود.
- خطای route stale واضح و actionable گزارش شود.

## Commit پیشنهادی

```text
feat: centralize sing-box apply and restart recovery
```

---

# فاز هفت: status و observability

## هدف

جداکردن واضح این مفاهیم در خروجی status:

- state دیتابیس
- state round
- state sing-box
- سلامت proxy
- public exit IP

## کارها

- [x] نمایش state هر job: `idle/running/queued/skipped/failed`
- [x] نمایش `blocked_by`
- [x] نمایش progress: `processed/total`
- [x] نمایش generation و round id
- [x] نمایش آخرین commit دیتابیس
- [x] نمایش آخرین apply sing-box
- [x] نمایش تعداد restart و failure
- [x] نمایش public exit IP به‌عنوان مقدار مستقل از TUN IP
- [x] حفظ bounded بودن تمام probeها
- [x] عدم چاپ raw exceptionهای طولانی
- [x] خروجی machine-readable اختیاری با `--json`

## نمونهٔ خروجی هدف

```text
Pool A: running | 12,384 / 227,048 | round=abc123
Pool B: waiting for commit | last=16:53 | next=16:55
Subs: idle | last=16:30 | next=17:00
Apply: healthy | last action=selector switch | 0 restarts
Proxy: connected | exit IP=...
TUN: 172.19.0.1/30 | internal only
```

## معیار پذیرش

- [x] کاربر بتواند بفهمد یک job واقعاً در حال اجراست یا فقط زمان قدیمی دارد.
- [x] status در زمان خرابی upstream سریع برگردد.
- [x] تفاوت TUN address و public exit IP واضح باشد.

## Commit پیشنهادی

```text
feat: expose coordinator and round health in status
```

---

# فاز هشت: تست، rollout و rollback روی Raspberry Pi

## تست‌های unit

- [ ] round state concurrent writers
- [ ] stale generation result
- [ ] Pool A promotion
- [ ] Pool B demotion grace
- [ ] serialized apply
- [ ] no restart for selector-only switch
- [ ] restart failure and recovery
- [ ] empty Pool B
- [ ] subscription update during Pool A
- [ ] status with DNS failure

## تست‌های integration

- [ ] دو worker تست با temporary sing-box
- [ ] simultaneous Pool A/B database commits
- [ ] membership change هنگام apply
- [ ] sing-box API unavailable
- [ ] route stale simulation در محیط test
- [ ] restart daemon هنگام round running

## rollout روی Pi

1. [ ] اجرای test suite در workspace
2. [ ] commit کوچک و مستقل
3. [ ] push به `origin/main`
4. [ ] بررسی `git ls-remote origin main`
5. [ ] stash یا ثبت هر تغییر local روی Pi
6. [ ] انتقال/دریافت commit روی Pi
7. [ ] compile در `/opt/totalray`
8. [ ] توقف کنترل‌شدهٔ `totalray.service`
9. [ ] نصب application files
10. [ ] `systemctl daemon-reload` در صورت تغییر unit
11. [ ] start و health check
12. [ ] اجرای `totalray status --json`
13. [ ] بررسی journal حداقل برای یک Pool B round
14. [ ] ثبت hash فایل‌های source و installed

## معیار rollback

اگر هرکدام از موارد زیر رخ داد، rollback انجام شود:

- `totalray.service` active نشود.
- `sing-box.service` crash-loop کند.
- Clash API روی `127.0.0.1:9090` در دسترس نباشد.
- Pool B بی‌دلیل خالی شود.
- دیتابیس migration شکست بخورد.
- round status ناسازگار یا غیرقابل خواندن شود.

Rollback باید شامل این موارد باشد:

```bash
sudo systemctl stop totalray.service
# restore previous /opt/totalray/totalray
# restore database only if migration was transactional
sudo systemctl start totalray.service
sudo systemctl status totalray sing-box
```

---

# ترتیب پیشنهادی نهایی commitها

1. `chore: record concurrency architecture baseline`
2. `fix: make round status updates concurrency safe`
3. `feat: add test round generations and stale result protection`
4. `refactor: separate test workers from state commit and apply`
5. `feat: run pool A and pool B test workers independently`
6. `feat: prioritize and bound pool A scanning`
7. `feat: centralize sing-box apply and restart recovery`
8. `feat: expose coordinator and round health in status`

هر commit باید مستقل compile/test/deploy شود. چند فاز نباید در یک commit بزرگ ادغام شوند.

---

# تصمیم معماری

معماری پیشنهادی دیاگرام تأیید می‌شود، با این اصلاحات:

- shared lock به دو lock منطقی تقسیم شود؛
- workerها مستقیماً DB و sing-box را مدیریت نکنند؛
- generation/snapshot به roundها اضافه شود؛
- round status به یک component مستقل منتقل شود؛
- Pool A bounded و cursor-based شود؛
- apply sing-box فقط از یک coordinator انجام شود.

تا قبل از اجرای فازهای یک تا سه، فعال‌کردن هم‌زمانی کامل Pool A/B توصیه نمی‌شود؛ چون قفل SQLite به‌تنهایی برای جلوگیری از stale result کافی نیست.

## وضعیت فعلی اجرای برنامه

- معماری موجود برای prototype و تست اولیه مناسب است.
- scheduler اکنون می‌تواند قبل از bootstrap فعال شود.
- Pool A به‌صورت chunk-based نتیجه‌ها را تدریجی ثبت می‌کند.
- Pool B از نظر job مستقل شده، اما هنوز state/version coordination کامل ندارد.
- ادامهٔ کار باید از فاز صفر و سپس فاز یک شروع شود، نه از concurrency بیشتر.
