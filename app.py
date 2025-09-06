import os
import uuid
import zipfile
import shutil
import time
import io
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename
from PIL import Image
from supabase import create_client, Client
from flask_cors import CORS
from datetime import datetime

# --- Flask App Initialization ---
app = Flask(__name__)

# --- Supabase Configuration ---
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL and Service Key must be set in environment variables.")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- CORS Configuration ---
allowed_origins = [
    "https://procreate-landing-page-sandbox.onrender.com",
    "https://procreate-landing-page.onrender.com",
    "http://127.0.0.1:5500"
]
CORS(app, origins=allowed_origins, supports_credentials=True )

# --- Constants ---
BUCKET_NAME = 'conversions'

# --- License Check Route ---
@app.route('/check-license', methods=['POST'])
def check_license():
    data = request.get_json()
    if not data or 'licenseKey' not in data:
        return jsonify({"message": "Invalid request: Missing license key."}), 400
    
    license_key = data['licenseKey']
    try:
        response = supabase.rpc('get_license_status', {'p_license_key': license_key}).execute()
        if not response.data:
            return jsonify({"isValid": False, "message": "License key not found."}), 404
        result = response.data[0]
        return jsonify({
            "isValid": result.get('is_valid'),
            "credits": result.get('sessions_remaining'),
            "message": result.get('message')
        }), 200
    except Exception as e:
        print(f"CRITICAL ERROR in /check-license: {e}")
        return jsonify({"message": "A server error occurred while validating the license."}), 500

# --- File Recovery Route ---
@app.route('/recover-link', methods=['POST'])
def recover_link():
    data = request.get_json()
    if not data or 'licenseKey' not in data:
        return jsonify([]), 400
    
    license_key = data['licenseKey']
    try:
        time_48_hours_ago = datetime.fromtimestamp(time.time() - 48*3600).isoformat()
        
        response = supabase.from_('conversions').select('original_filename, download_url, created_at') \
            .eq('license_key', license_key) \
            .gte('created_at', time_48_hours_ago) \
            .order('created_at', desc=True) \
            .execute()
        
        return jsonify(response.data)
    except Exception as e:
        print(f"ERROR in /recover-link: {e}")
        return jsonify([]), 500

# --- Main Conversion Route ---
@app.route('/convert', methods=['POST'])
def convert_files():
    license_key = request.form.get('licenseKey')
    original_filename = request.form.get('originalFilename', 'conversion.brushset')

    if not license_key:
        return jsonify({"message": "Missing license key."}), 401

    if 'file' not in request.files:
        return jsonify({"message": "No file was uploaded."}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"message": "No selected file."}), 400

    temp_extract_dir = None
    try:
        temp_extract_dir = os.path.join('temp', f"extract_{uuid.uuid4().hex}")
        os.makedirs(temp_extract_dir, exist_ok=True)

        processed_images = []
        with zipfile.ZipFile(file, 'r') as brushset_zip:
            brushset_zip.extractall(temp_extract_dir)

        for root, _, files_in_dir in os.walk(temp_extract_dir):
            for name in files_in_dir:
                if name.lower().endswith(('.png', '.jpg', '.jpeg')) and 'artwork.png' not in name.lower():
                    try:
                        img_path = os.path.join(root, name)
                        with Image.open(img_path) as img:
                            if img.width >= 1024 and img.height >= 1024:
                                processed_images.append(img_path)
                    except (IOError, SyntaxError):
                        continue
        
        if not processed_images:
            return jsonify({"message": "No valid stamps (min 1024x1024) were found in the brushset."}), 400

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, img_path in enumerate(sorted(processed_images)):
                base_name = os.path.splitext(original_filename)[0]
                zf.write(img_path, f"{base_name}_{i + 1}.png")
        zip_buffer.seek(0)

        zip_filename = f"ArtyPacks_{os.path.splitext(secure_filename(original_filename))[0]}_{uuid.uuid4().hex[:8]}.zip"
        
        supabase.storage.from_(BUCKET_NAME).upload(file=zip_buffer.getvalue(), path=zip_filename)
        
        public_url_response = supabase.storage.from_(BUCKET_NAME).get_public_url(zip_filename)
        download_url = public_url_response

        decrement_response = supabase.rpc('use_one_credit', {'p_license_key': license_key}).execute()
        if not decrement_response.data or not decrement_response.data[0].get('success'):
            supabase.storage.from_(BUCKET_NAME).remove([zip_filename])
            message = decrement_response.data[0].get('message', 'Invalid license or no credits remaining.')
            return jsonify({"message": message}), 403

        supabase.from_('conversions').insert({
            'license_key': license_key,
            'original_filename': original_filename,
            'download_url': download_url,
            'status': 'completed'
        }).execute()

        return jsonify({"downloadUrl": download_url})

    except zipfile.BadZipFile:
        return jsonify({"message": "A provided file seems to be corrupted or isn't a valid .brushset."}), 400
    except Exception as e:
        print(f"CRITICAL ERROR in /convert: {e}")
        return jsonify({"message": f"A critical server error occurred during conversion."}), 500
    finally:
        if temp_extract_dir and os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir, ignore_errors=True)

@app.route('/')
def index():
    return "Artypacks Converter Backend is running."

if __name__ == '__main__':
    if not os.path.exists('temp'):
        os.makedirs('temp')
    app.run(debug=True, port=5001)
