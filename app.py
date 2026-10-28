import base64
from datetime import datetime
import io
import os
import re
from flask import Flask, jsonify, render_template_string, request
from flask_cors import CORS
import numpy as np
from PIL import Image
import pytesseract
import pytz

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

system_data = {
    "temp": 0,
    "carbon": 0,
    "hum": 0,
    "en_t": 1,
    "en_c": 1,
    "en_h": 1,
    "t_min": 30,
    "t_max": 82,
    "c_min": 10,
    "c_max": 50,
    "h_min": 50,
    "h_max": 90,
    "timestamp": "ยังไม่มีข้อมูล",
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
  global system_data
  data = request.form if request.form else request.json
  if not data:
    data = request.args

  for key in [
      "temp",
      "carbon",
      "hum",
      "en_t",
      "en_c",
      "en_h",
      "t_min",
      "t_max",
      "c_min",
      "c_max",
      "h_min",
      "h_max",
  ]:
    if key in data and data.get(key) is not None:
      try:
        system_data[key] = float(data.get(key)) if "." in str(data.get(key)) or key != "en_t" and key != "en_c" and key != "en_h" else int(data.get(key))
      except:
        pass

  tz = pytz.timezone("Asia/Bangkok")
  system_data["timestamp"] = datetime.now(tz).strftime("%d/%m/%Y, %H:%M:%S")
  return jsonify({"success": True})


@app.route("/api/data", methods=["GET"])
def get_api_data():
  return jsonify(system_data)


@app.route("/dashboard", methods=["GET"])
def dashboard():
  html_content = """
    <!DOCTYPE html>
    <html lang="th">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>ESP32 Engineering Telemetry Dashboard</title>
      <style>
        :root { --bg-color: #0f172a; --card-bg: #1e293b; --border-color: #334155; --text-main: #f8fafc; --text-muted: #94a3b8; --accent-green: #10b981; --accent-orange: #f59e0b; --accent-red: #ef4444; }
        * { box-sizing: border-box; }
        body { background: var(--bg-color); color: var(--text-main); font-family: 'Segoe UI', Inter, sans-serif; margin: 0; padding: 25px; text-align: center; }
        header { max-width: 1000px; margin: 0 auto 25px auto; border-bottom: 1px solid var(--border-color); padding-bottom: 15px; display: flex; justify-content: space-between; align-items: center; }
        h1 { margin: 0; font-size: 1.4rem; color: var(--text-main); }
        .system-status { font-size: 0.85rem; background: rgba(16, 185, 129, 0.15); color: var(--accent-green); padding: 5px 12px; border-radius: 20px; border: 1px solid rgba(16, 185, 129, 0.3); font-weight: 600; }
        .dashboard-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; max-width: 1000px; margin: 0 auto; }
        .card { background: var(--card-bg); border-radius: 10px; padding: 20px; border: 1px solid var(--border-color); text-align: left; position: relative; overflow: hidden; }
        .card::before { content: ''; position: absolute; top: 0; left: 0; width: 4px; height: 100%; }
        .card.temp::before { background: var(--accent-orange); }
        .card.carbon::before { background: #38bdf8; }
        .card.hum::before { background: var(--accent-green); }
        .card-title { font-size: 0.9rem; color: var(--text-muted); font-weight: 600; text-transform: uppercase; margin-bottom: 10px; }
        .metric-display { display: flex; align-items: baseline; justify-content: space-between; margin: 15px 0; }
        .metric-value { font-size: 2.8rem; font-weight: 700; font-family: 'Courier New', monospace; }
        .disabled-text { font-size: 2rem; color: var(--text-muted); font-style: italic; }
        .threshold-panel { background: rgba(15, 23, 42, 0.6); border: 1px solid var(--border-color); border-radius: 6px; padding: 10px 12px; font-size: 0.82rem; display: flex; justify-content: space-between; color: var(--text-muted); }
        .footer-info { max-width: 1000px; margin: 25px auto 0 auto; font-size: 0.85rem; color: var(--text-muted); display: flex; justify-content: space-between; border-top: 1px solid var(--border-color); padding-top: 15px; }
      </style>
    </head>
    <body>
      <header>
        <h1><span>⚙️</span> ESP32 Engineering Telemetry Dashboard</h1>
        <div class="system-status">● SYSTEM ONLINE</div>
      </header>
      <div class="dashboard-grid">
        <div class="card temp">
          <div class="card-title">🌡️ อุณหภูมิ (Temperature)</div>
          <div class="metric-display">
            <div class="metric-value" id="v-temp">--.-</div>
            <div style="color: var(--text-muted); font-weight: 600;" id="unit-temp">UNIT</div>
          </div>
          <div class="threshold-panel" id="panel-temp">
            <span>Min: <b id="t-min" style="color:var(--accent-orange)">--</b></span>
            <span>Max: <b id="t-max" style="color:var(--accent-red)">--</b></span>
          </div>
        </div>
        <div class="card carbon">
          <div class="card-title">🧪 คาร์บอน (Carbon)</div>
          <div class="metric-display">
            <div class="metric-value" id="v-carbon">--.-</div>
            <div style="color: var(--text-muted); font-weight: 600;" id="unit-carbon">PPM</div>
          </div>
          <div class="threshold-panel" id="panel-carbon">
            <span>Min: <b id="c-min" style="color:var(--accent-orange)">--</b></span>
            <span>Max: <b id="c-max" style="color:var(--accent-red)">--</b></span>
          </div>
        </div>
        <div class="card hum">
          <div class="card-title">💧 ความชื้น (Humidity)</div>
          <div class="metric-display">
            <div class="metric-value" id="v-hum">--.-</div>
            <div style="color: var(--text-muted); font-weight: 600;" id="unit-hum">%RH</div>
          </div>
          <div class="threshold-panel" id="panel-hum">
            <span>Min: <b id="h-min" style="color:var(--accent-orange)">--</b></span>
            <span>Max: <b id="h-max" style="color:var(--accent-red)">--</b></span>
          </div>
        </div>
      </div>
      <div class="footer-info">
        <div>Architecture: Flask / Cloud OCR Engine</div>
        <div>Last Synchronized: <span id="v-time" style="font-family: monospace;">Waiting for sync...</span></div>
      </div>
      <script>
        async function fetchTelemetry() {
          try {
            const res = await fetch('/api/data');
            const data = await res.json();
            
            if (data.en_t == 1) {
              document.getElementById('v-temp').innerText = data.temp;
              document.getElementById('unit-temp').style.display = 'block';
              document.getElementById('panel-temp').style.display = 'flex';
            } else {
              document.getElementById('v-temp').innerHTML = '<span class="disabled-text">DISABLED</span>';
              document.getElementById('unit-temp').style.display = 'none';
              document.getElementById('panel-temp').style.display = 'none';
            }
            document.getElementById('t-min').innerText = data.t_min;
            document.getElementById('t-max').innerText = data.t_max;

            if (data.en_c == 1) {
              document.getElementById('v-carbon').innerText = data.carbon;
              document.getElementById('unit-carbon').style.display = 'block';
              document.getElementById('panel-carbon').style.display = 'flex';
            } else {
              document.getElementById('v-carbon').innerHTML = '<span class="disabled-text">DISABLED</span>';
              document.getElementById('unit-carbon').style.display = 'none';
              document.getElementById('panel-carbon').style.display = 'none';
            }
            document.getElementById('c-min').innerText = data.c_min;
            document.getElementById('c-max').innerText = data.c_max;

            if (data.en_h == 1) {
              document.getElementById('v-hum').innerText = data.hum;
              document.getElementById('unit-hum').style.display = 'block';
              document.getElementById('panel-hum').style.display = 'flex';
            } else {
              document.getElementById('v-hum').innerHTML = '<span class="disabled-text">DISABLED</span>';
              document.getElementById('unit-hum').style.display = 'none';
              document.getElementById('panel-hum').style.display = 'none';
            }
            document.getElementById('h-min').innerText = data.h_min;
            document.getElementById('h-max').innerText = data.h_max;
            document.getElementById('v-time').innerText = data.timestamp;
          } catch(e) { console.error(e); }
        }
        setInterval(fetchTelemetry, 2000);
        fetchTelemetry();
      </script>
    </body>
    </html>
    """
  return render_template_string(html_content)


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port, debug=False)