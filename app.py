import os
import uuid
import zipfile
import shutil
import io
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename
from PIL import Image
from supabase import create_client, Client
from flask_cors import CORS
from datetime import datetime, timedelta

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
    "https://procreate-landing-page.onrender.com"
]
CORS(app, origins=allowed_origins, supports_credentials=True )

# --- Main Conversion Route ---
@app.route('/convert', methods=['POST'])
def convert_files():
    license_key = request.form.get('licenseKey')
    original_filename = request.form.get('originalFilename', 'conversion.brushset')
    file = request.files.get('file')

    if not all([license_key, original_filename, file]):
        return jsonify({"message": "Missing required form data."}), 400

    try:
        decrement_response = supabase.rpc('use_one_credit', {'p_license_key': license_key}).execute()
        if not decrement_response.data or not decrement_response.data[0].get('success'):
            message = decrement_response.data[0].get('message', 'Invalid license or no credits remaining.')
            return jsonify({"message": message}), 403
    except Exception as e:
        return jsonify({"message": f"Database error during credit use: {str(e)}"}), 500

    processed_images, error, temp_extract_dir = process_brushset(file)
    if error:
        if temp_extract_dir: shutil.rmtree(temp_extract_dir, ignore_errors=True)
        return jsonify({"message": error}), 400

    if not processed_images:
        if temp_extract_dir: shutil.rmtree(temp_extract_dir, ignore_errors=True)
        return jsonify({"message": "No valid stamps (min 1024x1024) were found in the brushset."}), 400

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as zf:
        for i, img_path in enumerate(processed_images):
            base_name = os.path.splitext(original_filename)[0]
            zf.write(img_path, f"{base_name}_{i+1}.png")
    
    # *** THIS IS THE FIX: Cleanup is moved AFTER the loop is finished ***
    shutil.rmtree(os.path.dirname(processed_images[0]), ignore_errors=True)
    zip_buffer.seek(0)

    storage_path = f"{license_key}/{uuid.uuid4()}.zip"
    try:
        supabase.storage.from_('conversions').upload(storage_path, zip_buffer.getvalue(), {'contentType': 'application/zip'})
        download_url = supabase.storage.from_('conversions').create_signed_url(storage_path, 60 * 60 * 48)['signedURL']
        
        supabase.table('conversions').insert({
            'license_key': license_key,
            'download_url': download_url,
            'storage_path': storage_path,
            'original_filename': original_filename
        }).execute()

        return jsonify({"downloadUrl": download_url})
    except Exception as e:
        return jsonify({"message": f"Error during file upload or history logging: {str(e)}"}), 500
    finally:
        if temp_extract_dir and os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir, ignore_errors=True)

# --- License Check and History Recovery Routes ---
@app.route('/check-license', methods=['POST'])
def check_license():
    data = request.get_json()
    if not data or 'licenseKey' not in data:
        return jsonify({"message": "Invalid request: Missing license key."}), 400
    try:
        response = supabase.rpc('get_license_status', {'p_license_key': data['licenseKey']}).execute()
        if not response.data:
            return jsonify({"isValid": False, "message": "License key not found."}), 404
        return jsonify(response.data[0]), 200
    except Exception as e:
        return jsonify({"message": f"A server error occurred: {str(e)}"}), 500

@app.route('/recover-link', methods=['POST'])
def recover_link():
    data = request.get_json()
    license_key = data.get('licenseKey')
    if not license_key:
        return jsonify({"message": "Missing license key."}), 400
    try:
        forty_eight_hours_ago = (datetime.utcnow() - timedelta(hours=48)).isoformat()
        response = supabase.table('conversions').select('*').eq('license_key', license_key).gte('created_at', forty_eight_hours_ago).order('created_at', desc=True).execute()
        return jsonify(response.data), 200
    except Exception as e:
        return jsonify({"message": f"Failed to fetch history: {str(e)}"}), 500

# --- Helper Functions ---
def process_brushset(file_storage):
    temp_extract_dir = os.path.join('temp', f"extract_{uuid.uuid4().hex}")
    os.makedirs(temp_extract_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(file_storage, 'r') as brushset_zip:
            brushset_zip.extractall(temp_extract_dir)
        
        image_files = []
        for root, _, files in os.walk(temp_extract_dir):
            for name in files:
                if name.lower().endswith(('.png', '.jpg', '.jpeg')) and 'artwork.png' not in name.lower():
                    try:
                        img_path = os.path.join(root, name)
                        with Image.open(img_path) as img:
                            if img.width >= 1024 and img.height >= 1024:
                                image_files.append(img_path)
                    except (IOError, SyntaxError):
                        continue
        
        image_files.sort()
        if not image_files:
            return [], None, temp_extract_dir

        output_dir = os.path.join('temp', f"processed_{uuid.uuid4().hex}")
        os.makedirs(output_dir, exist_ok=True)
        renamed_image_paths = []
        for img_path in image_files:
            new_filepath = os.path.join(output_dir, os.path.basename(img_path))
            shutil.copy(img_path, new_filepath)
            renamed_image_paths.append(new_filepath)
        
        return renamed_image_paths, None, temp_extract_dir
    except zipfile.BadZipFile:
        return None, "A provided file seems to be corrupted or isn't a valid .brushset.", temp_extract_dir
    except Exception as e:
        return None, f"A critical error occurred during file processing: {str(e)}", temp_extract_dir
    finally:
        if temp_extract_dir and os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir, ignore_errors=True)

@app.route('/')
def index():
    return "Artypacks Converter Backend is running."

if __name__ == '__main__':
    if not os.path.exists('temp'):
        os.makedirs('temp')
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, host='0.0.0.0', port=port)
