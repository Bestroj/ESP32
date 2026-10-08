import os
import io
import numpy as np
from PIL import Image
import pytesseract
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
# ปลดล็อค CORS ให้อุปกรณ์ภายนอกส่งรูปเข้ามาได้
CORS(app, resources={r"/*": {"origins": "*"}})

@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "Cloud OCR Server is running!"}), 200

@app.route('/process_ocr', methods=['POST'])
def process_ocr():
    if 'image' not in request.files:
        return jsonify({"success": False, "number": "--.-", "error": "No image sent"}), 400

    file = request.files['image']
    image_bytes = file.read()

    try:
        image = Image.open(io.BytesIO(image_bytes)).convert('L')

        # ปรับให้อ่านเฉพาะตัวเลขและจุดทศนิยมแบบบรรทัดเดียว
        custom_config = r'--psm 7 -c tessedit_char_whitelist=0123456789.-'
        detected_number = pytesseract.image_to_string(image, config=custom_config).strip()

        if detected_number:
            print(f"[Cloud OCR Result]: {detected_number}")
            return jsonify({"success": True, "number": detected_number})
        else:
            return jsonify({"success": False, "number": "--.-"})

    except Exception as e:
        print(f"Error processing image: {e}")
        return jsonify({"success": False, "number": "ERR", "error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
