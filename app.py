import base64
from datetime import datetime
import io
import os
import re
import time
from flask import Flask, jsonify, render_template_string, request
from flask_cors import CORS
import numpy as np
from PIL import Image
import pytesseract
import pytz

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

system_data = {
    "temp": -999.0,
    "carbon": -999.0,
    "hum": -999.0,
    "en_t": 1,
    "en_c": 1,
    "en_h": 1,
    "t_min": 30,
    "t_max": 82,
    "c_min": 10,
    "c_max": 50,
    "h_min": 50,
    "h_max": 90,
    "rt": 0,              # เวลาที่บอร์ดถ่ายภาพรอบล่าสุด (epoch วินาที)
    "ocr": "",
    "test": 0,
    "delay": 0,
    "dev": "",
    "fw": "",
    "st_t": "normal", "st_c": "normal", "st_h": "normal",   # normal / pending / alerted / fail / off
    "pm_t": 0, "pm_c": 0, "pm_h": 0,                         # ผิดปกติมาแล้วกี่นาที
    "last_sync": 0,       # เวลาที่ Render ได้รับข้อมูลจากบอร์ดครั้งล่าสุด (epoch วินาที)
}

OCR_CONFIG = r"--psm 7 -c tessedit_char_whitelist=0123456789.-"
NUMBER_PATTERN = re.compile(r"^-?\d+(\.\d+)?$")
ZONE_KEYS = ["t", "c", "h"]
ZONE_NAMES = ["1. อุณหภูมิ", "2. คาร์บอน", "3. ความชื้น"]

# เก็บภาพที่ตัดแล้วจากรอบล่าสุดที่บอร์ดส่งมา ไว้ให้เปิดดูที่ /debug_crops
last_frame_debug = {"timestamp": "ยังไม่มีข้อมูล", "zones": []}


@app.route("/", methods=["GET"])
def health_check():
  return jsonify({"status": "Cloud OCR Server is running!"}), 200


@app.route("/process_ocr", methods=["POST"])
def process_ocr():
  if "image" not in request.files:
    return (
        jsonify({"success": False, "number": "--.-", "error": "No image sent"}),
        400,
    )
  file = request.files["image"]
  try:
    image = Image.open(io.BytesIO(file.read())).convert("L")
    custom_config = r"--psm 7 -c tessedit_char_whitelist=0123456789.-"
    detected_number = pytesseract.image_to_string(
        image, config=custom_config
    ).strip()
    if detected_number:
      return jsonify({"success": True, "number": detected_number})
    else:
      return jsonify({"success": False, "number": "--.-"})
  except Exception as e:
    return jsonify({"success": False, "number": "ERR", "error": str(e)}), 500


def crop_and_threshold(image, w_pct, h_pct, off_x, off_y, thresh):
  # ใช้สูตรเดียวกับ processCropImage ในหน้าเว็บ ESP32 เพื่อให้ผลเทียบกับแบบเดิมได้
  img_w, img_h = image.size
  box_w = int(img_w * (w_pct / 100.0))
  box_h = int(img_h * (h_pct / 100.0))
  box_x = (img_w - box_w) // 2 + off_x
  box_y = (img_h - box_h) // 2 + off_y
  if box_w <= 0 or box_h <= 0:
    return None

  crop = image.crop((box_x, box_y, box_x + box_w, box_y + box_h))
  rgb = np.asarray(crop, dtype=np.float64)
  gray = rgb[:, :, 0] * 0.299 + rgb[:, :, 1] * 0.587 + rgb[:, :, 2] * 0.114
  bw = np.where(gray < thresh, 0, 255).astype(np.uint8)

  # หน้าเว็บส่งภาพเป็น JPEG (คุณภาพ 0.92) จึงบีบอัดแบบเดียวกันก่อนอ่าน
  buf = io.BytesIO()
  Image.fromarray(bw, "L").convert("RGB").save(buf, format="JPEG", quality=92)
  buf.seek(0)
  return Image.open(buf).convert("L")


@app.route("/process_frame", methods=["POST"])
def process_frame():
  # รับภาพเต็มจากบอร์ด ESP32 แล้วตัดกรอบ ทำขาวดำ และอ่านค่าเอง (ไม่ต้องเปิดหน้าเว็บค้าง)
  global last_frame_debug
  raw_image = request.get_data()
  if not raw_image:
    return jsonify({"success": False, "error": "No image sent"}), 400
  try:
    image = Image.open(io.BytesIO(raw_image)).convert("RGB")
  except Exception as e:
    return jsonify({"success": False, "error": str(e)}), 400

  thresh = request.args.get("thr", default=188, type=int)
  result = {"success": True}
  debug_zones = []

  for i, key in enumerate(ZONE_KEYS):
    result[key] = -999.0
    result[key + "_raw"] = ""
    if request.args.get(f"en{i}", default=1, type=int) != 1:
      continue

    try:
      zone_img = crop_and_threshold(
          image,
          request.args.get(f"z{i}w", type=int),
          request.args.get(f"z{i}h", type=int),
          request.args.get(f"z{i}x", type=int),
          request.args.get(f"z{i}y", type=int),
          thresh,
      )
      if zone_img is None:
        continue
      text = pytesseract.image_to_string(zone_img, config=OCR_CONFIG).strip()
    except Exception:
      continue

    result[key + "_raw"] = text
    if NUMBER_PATTERN.match(text):
      result[key] = float(text)

    png = io.BytesIO()
    zone_img.save(png, format="PNG")
    debug_zones.append({
        "name": ZONE_NAMES[i],
        "raw": text,
        "value": result[key],
        "png": base64.b64encode(png.getvalue()).decode("ascii"),
    })

  tz = pytz.timezone("Asia/Bangkok")
  last_frame_debug = {
      "timestamp": datetime.now(tz).strftime("%d/%m/%Y, %H:%M:%S"),
      "zones": debug_zones,
  }
  return jsonify(result)


@app.route("/debug_crops", methods=["GET"])
def debug_crops():
  html_content = """
    <!DOCTYPE html>
    <html lang="th">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <meta http-equiv="refresh" content="10">
      <title>OCR Debug Crops</title>
      <style>
        body { background: #0f172a; color: #f8fafc; font-family: 'Segoe UI', sans-serif; padding: 20px; }
        .zone { display: inline-block; background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 12px; margin: 8px; text-align: center; }
        .zone img { height: 70px; background: #000; display: block; margin: 8px auto; }
        .muted { color: #94a3b8; font-size: 0.85rem; }
      </style>
    </head>
    <body>
      <h2>ภาพที่ Render ตัดจากบอร์ด (รอบล่าสุด)</h2>
      <div class="muted">เวลา: {{ data.timestamp }} · หน้านี้รีเฟรชทุก 10 วินาที</div>
      {% for z in data.zones %}
        <div class="zone">
          <b>{{ z.name }}</b>
          <img src="data:image/png;base64,{{ z.png }}">
          <div>OCR ดิบ: <code>{{ z.raw or '(ว่าง)' }}</code></div>
          <div>ค่าที่ใช้: {{ 'อ่านไม่ได้' if z.value == -999.0 else z.value }}</div>
        </div>
      {% endfor %}
    </body>
    </html>
    """
  return render_template_string(html_content, data=last_frame_debug)


@app.route("/update_readings", methods=["POST"])
def update_readings():
  data = request.form if request.form else (request.get_json(silent=True) or request.args)

  float_keys = ["temp", "carbon", "hum", "t_min", "t_max", "c_min", "c_max", "h_min", "h_max"]
  int_keys = ["en_t", "en_c", "en_h", "rt", "test", "delay", "pm_t", "pm_c", "pm_h"]
  text_keys = ["ocr", "dev", "fw", "st_t", "st_c", "st_h"]
  for key in float_keys + int_keys + text_keys:
    value = data.get(key)
    if value is None:
      continue
    try:
      if key in float_keys:
        system_data[key] = float(value)
      elif key in int_keys:
        system_data[key] = int(float(value))
      else:
        system_data[key] = str(value)[:60]
    except ValueError:
      pass

  system_data["last_sync"] = int(time.time())
  return jsonify({"success": True})


@app.route("/api/data", methods=["GET"])
def get_api_data():
  return jsonify(dict(system_data, server_now=int(time.time())))


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ESP32 Incubator Monitor</title>
  <style>
    :root {
      --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #f8fafc; --muted: #94a3b8;
      --ok: #10b981; --warn: #f59e0b; --bad: #ef4444; --off: #64748b;
    }
    * { box-sizing: border-box; }
    body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', Inter, 'Noto Sans Thai', sans-serif; margin: 0; padding: 24px 16px; }
    .wrap { max-width: 1000px; margin: 0 auto; }
    header { display: flex; flex-wrap: wrap; gap: 12px; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 14px; }
    h1 { margin: 0; font-size: 1.35rem; }
    .sub { color: var(--muted); font-size: 0.85rem; margin-top: 4px; }
    .badges { display: flex; gap: 8px; }
    .badge { font-size: 0.85rem; font-weight: 600; padding: 5px 12px; border-radius: 20px; border: 1px solid; }
    .b-ok { color: var(--ok); border-color: var(--ok); background: rgba(16,185,129,0.12); }
    .b-warn { color: var(--warn); border-color: var(--warn); background: rgba(245,158,11,0.12); }
    .b-bad { color: var(--bad); border-color: var(--bad); background: rgba(239,68,68,0.12); }
    .b-test { color: #38bdf8; border-color: #38bdf8; background: rgba(56,189,248,0.12); }
    .lastread { margin: 16px 0 20px; font-size: 1rem; }
    .lastread b { font-family: 'Courier New', monospace; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }
    .card { background: var(--card); border: 1px solid var(--border); border-left: 5px solid var(--off); border-radius: 10px; padding: 18px; }
    .card.normal { border-left-color: var(--ok); }
    .card.pending { border-left-color: var(--warn); }
    .card.alerted, .card.fail { border-left-color: var(--bad); }
    .card.stale { opacity: 0.55; }
    .title { color: var(--muted); font-weight: 600; font-size: 0.95rem; }
    .value { display: flex; align-items: baseline; gap: 8px; margin: 12px 0; min-height: 3.2rem; }
    .num { font-size: 2.6rem; font-weight: 700; font-family: 'Courier New', monospace; }
    .unit { color: var(--muted); font-weight: 600; }
    .na { font-size: 1.6rem; color: var(--muted); }
    .chip { display: inline-block; font-size: 0.85rem; font-weight: 600; padding: 3px 10px; border-radius: 6px; }
    .c-normal { background: rgba(16,185,129,0.15); color: var(--ok); }
    .c-pending { background: rgba(245,158,11,0.15); color: var(--warn); }
    .c-alerted, .c-fail { background: rgba(239,68,68,0.15); color: var(--bad); }
    .c-off { background: rgba(100,116,139,0.2); color: var(--muted); }
    .range { margin-top: 12px; font-size: 0.85rem; color: var(--muted); }
    footer { margin-top: 22px; padding-top: 14px; border-top: 1px solid var(--border); color: var(--muted); font-size: 0.85rem; display: flex; flex-wrap: wrap; gap: 6px 24px; }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <h1>ESP32 Incubator Monitor</h1>
        <div class="sub" id="device">อุปกรณ์ -</div>
      </div>
      <div class="badges">
        <span class="badge b-test" id="test-badge" style="display:none">TEST</span>
        <span class="badge b-bad" id="conn">กำลังโหลด...</span>
      </div>
    </header>

    <div class="lastread">อ่านค่าล่าสุดเมื่อ <b id="read-time">-</b> <span id="read-age" class="sub"></span></div>

    <div class="grid" id="grid"></div>

    <footer>
      <span>บอร์ดส่งข้อมูลล่าสุด: <span id="sync-time">-</span></span>
      <span>สถานะ OCR: <span id="ocr">-</span></span>
    </footer>
  </div>

  <script>
    const VARS = [
      { key: 't', field: 'temp', name: 'อุณหภูมิ', unit: '°C' },
      { key: 'c', field: 'carbon', name: 'CO₂', unit: '%' },
      { key: 'h', field: 'hum', name: 'ความชื้น', unit: '%RH' }
    ];
    const STALE_READ_SEC = 180;   // ไม่มีค่าอ่านใหม่เกิน 3 นาที = ข้อมูลล่าช้า
    const LOST_SYNC_SEC = 30;     // บอร์ดไม่ส่งข้อมูลเกิน 30 วินาที = ขาดการเชื่อมต่อ

    document.getElementById('grid').innerHTML = VARS.map(v => `
      <div class="card" id="card-${v.key}">
        <div class="title">${v.name}</div>
        <div class="value" id="val-${v.key}"><span class="na">-</span></div>
        <span class="chip c-off" id="chip-${v.key}">-</span>
        <div class="range" id="range-${v.key}"></div>
      </div>`).join('');

    function fmtTime(epoch) {
      return new Date(epoch * 1000).toLocaleString('en-GB', { timeZone: 'Asia/Bangkok' }).replace(',', '');
    }
    function fmtAge(sec) {
      if (sec < 60) return sec + ' วินาทีที่แล้ว';
      if (sec < 3600) return Math.floor(sec / 60) + ' นาทีที่แล้ว';
      return Math.floor(sec / 3600) + ' ชั่วโมงที่แล้ว';
    }

    async function refresh() {
      let d;
      try { d = await (await fetch('/api/data', { cache: 'no-store' })).json(); }
      catch (e) { return; }

      const syncAge = d.last_sync ? d.server_now - d.last_sync : Infinity;
      const readAge = d.rt ? d.server_now - d.rt : Infinity;
      const conn = document.getElementById('conn');
      let stale = false;
      if (syncAge > LOST_SYNC_SEC) { conn.textContent = 'ขาดการเชื่อมต่อ'; conn.className = 'badge b-bad'; stale = true; }
      else if (readAge > STALE_READ_SEC) { conn.textContent = 'ข้อมูลล่าช้า'; conn.className = 'badge b-warn'; stale = true; }
      else { conn.textContent = 'ออนไลน์'; conn.className = 'badge b-ok'; }

      document.getElementById('test-badge').style.display = d.test == 1 ? '' : 'none';
      document.getElementById('device').textContent = 'อุปกรณ์ ' + (d.dev || '-') + (d.fw ? ' · firmware ' + d.fw : '');
      document.getElementById('read-time').textContent = d.rt ? fmtTime(d.rt) : 'ยังไม่มีข้อมูล';
      document.getElementById('read-age').textContent = d.rt ? '(' + fmtAge(readAge) + ')' : '';
      document.getElementById('sync-time').textContent = d.last_sync ? fmtTime(d.last_sync) + ' (' + fmtAge(syncAge) + ')' : 'ยังไม่มีข้อมูล';
      document.getElementById('ocr').textContent = d.ocr || '-';

      VARS.forEach(v => {
        const st = d['st_' + v.key] || 'normal';
        const mins = d['pm_' + v.key] || 0;
        const value = d[v.field];
        const card = document.getElementById('card-' + v.key);
        const chip = document.getElementById('chip-' + v.key);
        const val = document.getElementById('val-' + v.key);

        card.className = 'card ' + st + (stale ? ' stale' : '');
        if (st === 'off') val.innerHTML = '<span class="na">ปิดใช้งาน</span>';
        else if (value === -999 || value === null || value === undefined) val.innerHTML = '<span class="na">อ่านไม่ได้</span>';
        else val.innerHTML = '<span class="num">' + value.toFixed(2) + '</span><span class="unit">' + v.unit + '</span>';

        const labels = {
          normal: 'ปกติ',
          pending: 'รอยืนยัน ' + mins + '/' + d.delay + ' นาที',
          alerted: 'แจ้งเตือนแล้ว (' + mins + ' นาที)',
          fail: 'อ่านไม่ได้',
          off: 'ปิดใช้งาน'
        };
        chip.textContent = labels[st] || st;
        chip.className = 'chip c-' + st;

        const lo = d[v.key + '_min'], hi = d[v.key + '_max'];
        document.getElementById('range-' + v.key).textContent =
          st === 'off' ? '' : 'ช่วงปกติ ' + Number(lo).toFixed(1) + '–' + Number(hi).toFixed(1) + ' ' + v.unit;
      });
    }
    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""


@app.route("/dashboard", methods=["GET"])
def dashboard():
  return DASHBOARD_HTML


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port, debug=False)