import base64
from datetime import datetime
import io
import os
import random
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
    "fail": 10,           # อ่านค่าไม่ได้ต่อเนื่องกี่นาทีจึงแจ้งเตือน
    "dev": "",
    "ip": "",             # IP ของบอร์ดในวง Wi-Fi (ใช้เปิดหน้าติดตั้ง)
    "fw": "",
    "st_t": "normal", "st_c": "normal", "st_h": "normal",   # normal / pending / alerted / fail / off
    "pm_t": 0, "pm_c": 0, "pm_h": 0,                         # ผิดปกติมาแล้วกี่นาที
    "last_sync": 0,       # เวลาที่ Render ได้รับข้อมูลจากบอร์ดครั้งล่าสุด (epoch วินาที)
}

# ค่าตั้งแจ้งเตือนที่ผู้ใช้บันทึกจาก dashboard รอให้บอร์ดดึงไปใช้ (บอร์ดเก็บค่าจริงไว้ใน NVS)
# แนบไปกับคำตอบของ /update_readings จนกว่าบอร์ดจะส่ง cfg_id เดียวกันกลับมา
pending_cfg = None
CFG_KEYS = ["t_min", "t_max", "c_min", "c_max", "h_min", "h_max", "delay", "fail"]
SHEET_URL = "https://docs.google.com/spreadsheets/d/1aKXN7INoNZrynT6NihNWgw424lyYEMo7BppxmKSpMaw/edit"

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
        body {
          background: #000000; color: #f5f5f7; color-scheme: dark; margin: 0; padding: 32px 16px;
          font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
          font-size: 15px; line-height: 1.47; -webkit-font-smoothing: antialiased;
        }
        h2 { font-size: 28px; font-weight: 600; letter-spacing: -0.28px; margin: 0 0 4px; }
        .zone { display: inline-block; vertical-align: top; background: #1c1c1e; border: 1px solid rgba(255,255,255,0.06); border-radius: 18px; padding: 20px; margin: 16px 16px 0 0; text-align: center; }
        .zone img { height: 70px; background: #000; display: block; margin: 10px auto; border-radius: 8px; }
        .zone code { background: #2c2c2e; padding: 2px 6px; border-radius: 5px; }
        .muted { color: #98989d; font-size: 14px; }
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
  int_keys = ["en_t", "en_c", "en_h", "rt", "test", "delay", "fail", "pm_t", "pm_c", "pm_h"]
  text_keys = ["ocr", "dev", "fw", "ip", "st_t", "st_c", "st_h"]
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

  global pending_cfg
  system_data["last_sync"] = int(time.time())
  if pending_cfg and str(data.get("cfg_id")) == str(pending_cfg["cfg_id"]):
    pending_cfg = None   # บอร์ดรับค่าแล้ว
  return jsonify(dict(pending_cfg, success=True) if pending_cfg else {"success": True})


@app.route("/api/config", methods=["POST"])
def set_config():
  global pending_cfg
  data = request.get_json(silent=True) or {}
  try:
    cfg = {key: float(data[key]) for key in CFG_KEYS}
  except (KeyError, TypeError, ValueError):
    return jsonify({"success": False, "error": "กรอกตัวเลขให้ครบทุกช่อง"}), 400

  for lo, hi, name in [("t_min", "t_max", "อุณหภูมิ"), ("c_min", "c_max", "CO2"), ("h_min", "h_max", "ความชื้น")]:
    if cfg[lo] >= cfg[hi]:
      return jsonify({"success": False, "error": "ขอบล่างของ" + name + "ต้องน้อยกว่าขอบบน"}), 400
  if not (1 <= cfg["delay"] <= 240) or cfg["delay"] != int(cfg["delay"]):
    return jsonify({"success": False, "error": "เวลารอแจ้งเตือนต้องเป็นจำนวนเต็ม 1-240 นาที"}), 400
  if not (1 <= cfg["fail"] <= 240) or cfg["fail"] != int(cfg["fail"]):
    return jsonify({"success": False, "error": "เวลาแจ้งเมื่ออ่านค่าไม่ได้ต้องเป็นจำนวนเต็ม 1-240 นาที"}), 400

  cfg["delay"] = int(cfg["delay"])
  cfg["fail"] = int(cfg["fail"])
  cfg["cfg_id"] = random.randint(1, 9999999)   # ไม่เกินความละเอียดของ float บนบอร์ด
  pending_cfg = cfg
  return jsonify({"success": True})


@app.route("/api/data", methods=["GET"])
def get_api_data():
  return jsonify(dict(system_data, server_now=int(time.time()), cfg_pending=pending_cfg is not None))


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ESP32 Incubator Monitor</title>
  <style>
    /* โทนมืดตามแนว Apple: พื้นดำ การ์ดเทาเข้ม ปุ่มสีฟ้าสีเดียว สีเขียว/ส้ม/แดงใช้บอกสถานะเท่านั้น */
    :root {
      color-scheme: dark;
      --bg: #000000; --card: #1c1c1e; --inset: #2c2c2e; --text: #f5f5f7; --muted: #98989d; --hairline: #3a3a3c;
      --blue: #0071e3; --blue-focus: #2997ff;
      --ok: #30d158; --warn: #ff9f0a; --bad: #ff453a;             /* ตัวหนังสือ */
      --ok-line: #30d158; --warn-line: #ff9f0a; --bad-line: #ff453a; /* เส้นขอบ */
    }
    * { box-sizing: border-box; }
    body {
      background: var(--bg); color: var(--text); margin: 0; padding: 32px 16px 40px;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
      font-size: 17px; line-height: 1.47; -webkit-font-smoothing: antialiased;
    }
    .wrap { max-width: 980px; margin: 0 auto; }
    header { display: flex; flex-wrap: wrap; gap: 12px; justify-content: space-between; align-items: center; }
    h1 { margin: 0; font-size: 34px; font-weight: 600; letter-spacing: -0.37px; line-height: 1.15; }
    .sub { color: var(--muted); font-size: 14px; margin-top: 4px; }
    .badges { display: flex; gap: 8px; }
    .badge { font-size: 14px; font-weight: 600; padding: 6px 14px; border-radius: 9999px; }
    .b-ok { color: var(--ok); background: rgba(48,209,88,0.16); }
    .b-warn { color: var(--warn); background: rgba(255,159,10,0.16); }
    .b-bad { color: var(--bad); background: rgba(255,69,58,0.16); }
    .b-test { color: var(--blue-focus); background: rgba(41,151,255,0.16); }
    .lastread { margin: 20px 0 24px; font-size: 17px; }
    .lastread b { font-weight: 600; font-variant-numeric: tabular-nums; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 20px; }
    .card { background: var(--card); border: 1px solid rgba(255,255,255,0.06); border-radius: 18px; padding: 24px; }
    .card.pending { border: 2px solid var(--warn-line); padding: 23px; }
    .card.alerted, .card.fail { border: 2px solid var(--bad-line); padding: 23px; }
    .card.stale { opacity: 0.5; }
    .title { font-size: 17px; font-weight: 600; }
    .value { display: flex; align-items: baseline; gap: 8px; margin: 12px 0 14px; min-height: 56px; }
    .num { font-size: 48px; font-weight: 600; letter-spacing: -0.5px; line-height: 1.07; font-variant-numeric: tabular-nums; }
    .unit { color: var(--muted); font-size: 21px; }
    .na { font-size: 28px; color: var(--muted); }
    .chip { display: inline-block; font-size: 14px; font-weight: 600; padding: 4px 12px; border-radius: 9999px; }
    .c-normal { background: rgba(48,209,88,0.16); color: var(--ok); }
    .c-pending { background: rgba(255,159,10,0.16); color: var(--warn); }
    .c-alerted, .c-fail { background: rgba(255,69,58,0.16); color: var(--bad); }
    .c-off { background: var(--inset); color: var(--muted); }
    .range { margin-top: 14px; font-size: 14px; color: var(--muted); }
    .settings { margin-top: 24px; background: var(--card); border: 1px solid rgba(255,255,255,0.06); border-radius: 18px; padding: 20px 24px; }
    .settings summary { cursor: pointer; font-size: 17px; font-weight: 600; }
    .set-row { display: grid; grid-template-columns: 110px 1fr 1fr; gap: 12px; align-items: end; margin-top: 16px; }
    .set-row label { font-size: 13px; color: var(--muted); display: block; margin-bottom: 4px; }
    .set-row .name { font-weight: 600; padding-bottom: 10px; }
    .settings input {
      width: 100%; padding: 10px 14px; border-radius: 11px; border: 1px solid var(--hairline); background: var(--inset);
      color: var(--text); font: inherit; font-size: 17px; font-variant-numeric: tabular-nums;
    }
    .settings input:focus { outline: 2px solid var(--blue-focus); outline-offset: 0; border-color: transparent; }
    .save {
      margin-top: 20px; padding: 11px 28px; border: 0; border-radius: 9999px; background: var(--blue); color: #fff;
      font: inherit; font-size: 17px; cursor: pointer; transition: transform 0.1s;
    }
    .save:active { transform: scale(0.95); }
    .save:focus-visible { outline: 2px solid var(--blue-focus); outline-offset: 2px; }
    .save:disabled { opacity: 0.5; cursor: default; }
    #set-status { margin-left: 12px; font-size: 14px; font-weight: 600; }
    footer { margin-top: 28px; color: var(--muted); font-size: 12px; display: flex; flex-wrap: wrap; gap: 6px 24px; }
    footer .links { flex-basis: 100%; display: flex; flex-wrap: wrap; gap: 6px 20px; }
    footer a { color: var(--blue-focus); text-decoration: none; }
    @media (max-width: 480px) {
      h1 { font-size: 28px; }
      .set-row { grid-template-columns: 1fr 1fr; }
      .set-row .name { grid-column: 1 / -1; padding-bottom: 0; }
    }
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

    <details class="settings" id="settings">
      <summary>ตั้งค่าการแจ้งเตือน</summary>
      <div id="set-rows"></div>
      <div class="set-row">
        <div class="name">เวลารอ</div>
        <div><label>แจ้งเตือนเมื่อผิดปกติต่อเนื่อง (นาที)</label><input type="number" id="set-delay" min="1" max="240" step="1"></div>
        <div><label>แจ้งเมื่ออ่านค่าไม่ได้ต่อเนื่อง (นาที)</label><input type="number" id="set-fail" min="1" max="240" step="1"></div>
      </div>
      <button class="save" id="set-save" onclick="saveSettings()">บันทึก</button>
      <span id="set-status"></span>
    </details>

    <footer>
      <span>บอร์ดส่งข้อมูลล่าสุด: <span id="sync-time">-</span></span>
      <span>สถานะ OCR: <span id="ocr">-</span></span>
      <span class="links">
        <a href="{{SHEET_URL}}" target="_blank" rel="noopener">ข้อมูลย้อนหลัง (Google Sheet)</a>
        <a href="/debug_crops" target="_blank" rel="noopener">ภาพจากกล้อง (ผู้ติดตั้ง)</a>
        <span id="setup-link"></span>
      </span>
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

    let last = null;
    let waitingBoard = false;

    function fillSettings() {
      if (!last) return;
      document.getElementById('set-rows').innerHTML = VARS.filter(v => last['st_' + v.key] !== 'off').map(v => `
        <div class="set-row">
          <div class="name">${v.name}</div>
          <div><label>ขอบล่าง (${v.unit})</label><input type="number" step="any" id="set-${v.key}-min" value="${last[v.key + '_min']}"></div>
          <div><label>ขอบบน (${v.unit})</label><input type="number" step="any" id="set-${v.key}-max" value="${last[v.key + '_max']}"></div>
        </div>`).join('');
      document.getElementById('set-delay').value = last.delay;
      document.getElementById('set-fail').value = last.fail;
      document.getElementById('set-status').textContent = '';
    }
    document.getElementById('settings').addEventListener('toggle', e => { if (e.target.open) fillSettings(); });

    async function saveSettings() {
      const status = document.getElementById('set-status');
      const body = { delay: document.getElementById('set-delay').value, fail: document.getElementById('set-fail').value };
      VARS.forEach(v => {
        const lo = document.getElementById('set-' + v.key + '-min');
        const hi = document.getElementById('set-' + v.key + '-max');
        body[v.key + '_min'] = lo ? lo.value : last[v.key + '_min'];   // ตัวแปรที่ปิดอยู่ใช้ค่าเดิม
        body[v.key + '_max'] = hi ? hi.value : last[v.key + '_max'];
      });
      status.style.color = 'var(--muted)';
      status.textContent = 'กำลังบันทึก...';
      try {
        const res = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const r = await res.json();
        if (!r.success) { status.style.color = 'var(--bad)'; status.textContent = r.error || 'บันทึกไม่สำเร็จ'; return; }
        waitingBoard = true;
        status.style.color = 'var(--warn)';
        status.textContent = 'บันทึกแล้ว รอบอร์ดรับค่า...';
      } catch (e) {
        status.style.color = 'var(--bad)';
        status.textContent = 'เชื่อมต่อไม่ได้ ลองใหม่อีกครั้ง';
      }
    }

    async function refresh() {
      let d;
      try { d = await (await fetch('/api/data', { cache: 'no-store' })).json(); }
      catch (e) { return; }
      last = d;
      if (waitingBoard && !d.cfg_pending) {
        waitingBoard = false;
        const status = document.getElementById('set-status');
        status.style.color = 'var(--ok)';
        status.textContent = 'บอร์ดรับค่าใหม่แล้ว';
      }

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
      const setup = document.getElementById('setup-link');
      setup.innerHTML = '';
      if (d.ip && /^[0-9.]+$/.test(d.ip) && d.ip !== '0.0.0.0') {
        const a = document.createElement('a');
        a.href = 'http://' + d.ip + '/';
        a.textContent = 'หน้าติดตั้ง http://' + d.ip;
        setup.append(a, ' (ต้องต่อ Wi-Fi เดียวกับบอร์ด)');
      }

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
  return DASHBOARD_HTML.replace("{{SHEET_URL}}", SHEET_URL)


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port, debug=False)