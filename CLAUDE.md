# CLAUDE.md — Bow ML Labeler
# Web App สำหรับ Label Raw Session ข้อมูล Bow Pet IoT

> ไฟล์นี้คือ source of truth สำหรับ ml_labeler.py
> อ่านก่อนแก้ code ทุกครั้ง — ห้ามเปลี่ยน logic โดยไม่อัปเดตไฟล์นี้

---

## โปรเจกต์นี้คืออะไร

Web App สำหรับ Human-in-the-loop ML Labeling ของระบบ **Bow Pet IoT**
ดึง raw session จาก Railway Backend → แสดงกราฟ → รับ label จากผู้ใช้ → ส่งกลับ Railway

---

## Tech Stack

| ส่วน | รายละเอียด |
|------|-----------|
| Language | Python 3 |
| Web Framework | Flask (single-file app) |
| Frontend | Vanilla JS + Chart.js 4.4 (CDN) |
| Deployed | Railway — https://web-production-6358e.up.railway.app |
| Source | GitHub → thanapatkong-spec/bow-ml-labeler |
| Auto-deploy | Push to `main` → Railway deploy อัตโนมัติ |

---

## Architecture

```
ml_labeler.py
├── _fetch_railway()        ← background thread — poll Railway ทุก 6 วิ
├── sessions_cache{}        ← id → full session dict (รวม points)
├── pending[]               ← session summary สำหรับแสดงใน UI
├── labeled[]               ← label ล่าสุด 50 รายการ (local only)
│
├── GET  /                  ← HTML UI (embedded ใน HTML string)
├── GET  /api/state         ← pending + labeled + railway_ok
├── GET  /api/session_data  ← raw points สำหรับ Chart.js
├── POST /api/label         ← บันทึก label → PATCH Railway
└── POST /api/skip          ← ซ่อน session จาก pending list
```

---

## Data Flow

```
Railway /api/sense-pad/raw-sessions
    ↓ (poll ทุก 6s)
_fetch_railway()
    ↓
pending[] + sessions_cache[]
    ↓ (UI แสดงผล)
ผู้ใช้กดปุ่ม label
    ↓
PATCH /api/sense-pad/raw-sessions/:id/label
    → { wasteType, behavior, notes, labeledBy: "user" }
```

---

## Pending Entry Fields

```python
{
    "id":         int,       # DB primary key (ใช้เรียก API)
    "sessionId":  str,       # Unix timestamp SID (แสดงใน UI)
    "catId":      int|None,
    "padType":    str,       # "bow" | "food" | "water"
    "device":     str,       # เหมือน padType
    "start":      str,       # "YYYY-MM-DD HH:MM:SS" Bangkok time
    "uploadedAt": str,
    "durationMs": int,
    "pointCount": int,
    "peak":       float,     # peakTotal_g
    "activity":   str|None,  # จาก SensePadEvent (join ที่ backend)
    "weight_g":   float|None,# netCatWeight_g
    "waste_g":    float|None,# wasteWeight_g
}
```

---

## Raw Points Format

จาก `/api/session_data?id=<id>` — แปลงเป็น list ของ:
```python
{
    "t_ms": int,    # เวลาตั้งแต่ session start
    "tot":  float,  # total weight (g)
    "fl":   float,  # corner front-left (g)
    "fr":   float,  # corner front-right (g)
    "rl":   float,  # corner rear-left (g)
    "rr":   float,  # corner rear-right (g)
    "dw":   float,  # dW/dt (g/s)
    "std":  float,  # stdDev (g)
}
```

⚠️ **BOW pad ส่งเป็น NET values** (หัก baseline แล้ว) — graph เริ่มที่ ~0g
⚠️ **FOOD pad** ส่งเป็น absolute total และ net

---

## Label Categories

### BOW (Litter Box)
**🐱 Cat:**
| ปุ่ม | wasteType | ความหมาย |
|------|-----------|---------|
| Urine | URINE | ปัสสาวะ |
| Feces | FECES | อุจจาระ |
| Both | URINE+FECES | ทั้งคู่ |
| Groom | GROOMING | เลีย/ทำความสะอาด |
| Sleep | SLEEP | นอน |
| Visit | VISIT | เดินผ่าน |
| Dig | DIG | คุ้ยทราย |
| False | FALSE | false trigger |

**🧑 Owner:**
| ปุ่ม | wasteType |
|------|-----------|
| Scoop | SCOOP |
| Sand | SAND |
| Remove Box | REMOVE_BOX |
| Place Box | PLACE_BOX |

**Behavior (สำหรับ URINE/FECES/GROOMING):**
`normal` | `restless` | `difficulty`

### FOOD (Feed Pad)
**🍲 Cat:** `EATING` → behavior: `Licking` | `Biting` | `Chomping`
**🧑 Owner:** `FILL_FOOD` | `REMOVE_BOWL` | `PLACE_BOWL`
**Other:** `SNIFFING` | `VISIT` | `FALSE`

### WATER (Water Pad)
**💧 Cat:** `DRINKING` → behavior: `long_drink` | `short_drink`
**🧑 Owner:** `FILL_WATER` | `REMOVE_BOWL` | `PLACE_BOWL`
**Other:** `SNIFFING` | `VISIT` | `FALSE`

---

## Poll & Cache Logic

```python
POLL_INTERVAL = 6    # วินาที
FETCH_LIMIT   = 100  # sessions ต่อครั้ง

# ทุกรอบ poll:
# 1. sessions_cache[id] = sess  ← อัปเดตทุกรอบเสมอ (รวม metadata ใหม่)
# 2. ถ้า id ไม่อยู่ใน pending → append ใหม่
# 3. ถ้า id อยู่ใน pending อยู่แล้ว → อัปเดต activity/weight_g/waste_g/peak
#    (สำคัญ: metadata อาจมาทีหลังถ้า backend deploy ใหม่)
```

---

## Session Time Display

```python
# sessionId คือ Unix timestamp (วินาที)
# ถ้า sessionId > 1e12 = milliseconds → หาร 1000 ก่อน
ts_s = int(sess["sessionId"]) / 1000 if int(sess["sessionId"]) > 1e12 else int(sess["sessionId"])
ts_dt = datetime.fromtimestamp(ts_s, tz=TZ_BKK)  # Bangkok UTC+7
start_str = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
```

---

## Environment Variables

| Variable | Default | ความหมาย |
|----------|---------|---------|
| `RAILWAY_URL` | `https://bow-iot-backend-production.up.railway.app` | Backend URL |
| `PORT` | `8080` | HTTP port |
| `ACCESS_USER` | `bow` | Basic auth username |
| `ACCESS_PASS` | `""` (no auth) | Basic auth password — ถ้าไม่ set = ไม่มี auth |

---

## Backend API ที่ใช้

| Method | Endpoint | ใช้ทำ |
|--------|----------|-------|
| GET | `/api/sense-pad/raw-sessions?unlabeled=true&limit=100` | ดึง pending sessions |
| PATCH | `/api/sense-pad/raw-sessions/:id/label` | บันทึก label |

### Response fields จาก GET raw-sessions
backend join SensePadEvent → เพิ่ม fields:
```json
{
  "activity":      "URINE OR FECES",
  "weight_g":      1234.5,
  "waste_g":       15.3,
  "grossWeight_g": 1249.8
}
```

---

## กฎห้ามละเมิด

1. **ห้ามเปลี่ยน label category** โดยไม่อัปเดตตาราง Label Categories ข้างบน
2. **ห้ามเปลี่ยน API endpoint** โดยไม่ตรวจสอบกับ `bow-iot-backend/router/sensePad.js` ก่อน
3. **pending[] อัปเดต metadata ทุกรอบ poll** — ห้าม skip existing session โดยไม่อัปเดต fields
4. **`sessions_cache` เก็บ full session รวม points** — ห้ามตัด points ออกตอนเก็บ cache
5. **labeledBy ต้องส่งเป็น `"user"`** เสมอเมื่อ label จากหน้าเว็บ
6. **ห้าม import library ใหม่** นอกจาก stdlib + flask + requests (Railway ไม่มี pip cache)

---

## Deploy

```bash
# Local test
python ml_labeler.py
# → http://localhost:8080

# Deploy to Railway
git add -A && git commit -m "..." && git push
# Railway auto-deploy จาก main branch
# รอ ~1-2 นาที → https://web-production-6358e.up.railway.app
```

---

## การอัปเดตไฟล์นี้

- เพิ่ม label category → อัปเดต Label Categories section
- เปลี่ยน API → อัปเดต Backend API section
- เปลี่ยน poll logic → อัปเดต Poll & Cache Logic section
- เพิ่ม env var → อัปเดต Environment Variables section
