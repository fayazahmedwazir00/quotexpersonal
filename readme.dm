# Quotex AI Analyzer — Termux Setup (Complete)

## 🔧 Step 1 — Termux install
F-Droid سے Termux install کریں (Play Store والا پرانا ہے، استعمال نہ کریں)۔

## 🔧 Step 2 — Dependencies
```bash
pkg update && pkg upgrade -y
pkg install python git openssl libffi -y
pip install --upgrade pip
pip install Flask requests python-dotenv
pip install git+https://github.com/cleitonleonel/pyquotex.git
```

## 🔧 Step 3 — Files رکھیں
Termux میں ایک فولڈر بنائیں:
```bash
mkdir -p ~/quotex-ai && cd ~/quotex-ai
```
پھر اوپر دی گئی 8 فائلیں یہاں رکھیں:
- server.py
- quotex_auth.py
- analyzer.py
- database.py
- index.html
- requirements.txt
- setup.md
- .env.example

## 🔧 Step 4 — Server چلائیں
```bash
cd ~/quotex-ai
python server.py
```

اگر sab theek ہو تو یہ نظر آئے گا:
```
* Running on http://0.0.0.0:5000
```

## 🔧 Step 5 — Termux wake-lock (mobile sleep سے بچنے کے لیے)
نیا Termux session کھولیں:
```bash
termux-wake-lock
```

## 🔧 Step 6 — Cloudflare Tunnel (public URL)
نیا Termux session:
```bash
pkg install cloudflared -y
cloudflared tunnel --url http://localhost:5000
```

آپ کو ایک URL ملے گا جیسے:
```
https://random-words-here.trycloudflare.com
```

**یہی URL آپ کی website ہے — browser میں کھولیں۔**

## 🔧 Step 7 — Test
1. Browser میں Cloudflare URL کھولیں
2. Email + Password دیں → Login
3. Email پر PIN آئے گا → PIN ڈالیں
4. Symbol + Timeframe select کریں
5. **ANALYZE NOW** دبائیں
6. 25-60 seconds میں result آئے گا

## ⚠️ ضروری نکات

### Session persistance
`pyquotex` session file بناتا ہے — دوبارہ login نہیں مانگے گا جب تک server restart نہ ہو۔ اگر session expire ہو جائے تو دوبارہ login کریں۔

### Time sync
اگر Quotex "wrong time" error دے تو:
```bash
pkg install termux-api -y
termux-setup-storage
```

### Cloudflare URL badalna
ہر بار `cloudflared` چلانے پر URL بدل جاتا ہے۔ اگر permanent URL چاہیے تو Cloudflare account بنائیں (free) اور named tunnel استعمال کریں۔

### Flask production
Testing کے لیے Flask dev server کافی ہے۔ اگر 24/7 چلانا ہو تو:
```bash
pip install waitress
python -c "from waitress import serve; import server; serve(server.app, host='0.0.0.0', port=5000)"
```

## 🚀 Auto-Trade Toggle
- **Default OFF** — صرف analysis دیں گے
- Test کے لیے ON کریں تو confidence >= 60% پر trade place ہوگا
- ⚠️ Real account پر ON کرنے سے پہلے **Demo پر 1 مہینہ test** کریں

## 📊 Troubleshooting

| مسئلہ | حل |
|-------|-----|
| `pyquotex import error` | `pip install git+https://github.com/cleitonleonel/pyquotex.git` دوبارہ چلائیں |
| Login stuck at "Connecting" | PIN email میں چیک کریں، spam folder بھی |
| `check_connect = False` | Server restart کریں |
| Cloudflare URL نہیں کھل رہا | Termux کے دونوں sessions چیک کریں |
| Analysis timeout | Symbol badal kar try کریں (EURUSD_otc best ہے) |

## 🎯 Next Steps
- Phase 2: Signal history database dashboard
- Phase 3: Multiple symbols monitor
- Phase 4: Advanced strategies (multiple models per symbol)
