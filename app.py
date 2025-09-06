import os
import uuid
import zipfile
import shutil
from flask import Flask, request, jsonify, Blueprint
from werkzeug.utils import secure_filename
from PIL import Image
from supabase import create_client, Client
from flask_cors import CORS
from sqlalchemy import desc # THIS IS THE FIX FOR THE 404 CRASH

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
# *** THIS IS THE FINAL FIX FOR THE CORS ERROR ***
CORS(app, origins=allowed_origins, supports_credentials=True, resources={r"/api/*": {}} )

# --- API Blueprint ---
api = Blueprint('api', __name__, url_prefix='/api')

# --- License Check Route ---
@api.route('/check-license', methods=['POST'])
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

# --- Main Conversion Route ---
@api.route('/convert', methods=['POST'])
def convert_files():
    license_key = request.form.get('licenseKey')
    if not license_key:
        return jsonify({"message": "Missing license key."}), 401

    try:
        decrement_response = supabase.rpc('use_one_credit', {'p_license_key': license_key}).execute()
        if not decrement_response.data or not decrement_response.data[0].get('success'):
             message = decrement_response.data[0].get('message', 'Invalid license or no credits remaining.')
             return jsonify({"message": message}), 403
    except Exception as e:
        print(f"CRITICAL ERROR in /convert during credit use: {e}")
        return jsonify({"message": "Failed to update credits due to a database error."}), 500

    if 'file' not in request.files:
        return jsonify({"message": "No file was uploaded."}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"message": "No selected file."}), 400

    temp_extract_dir = None
    try:
        if file and file.filename.endswith('.brushset'):
            filename = secure_filename(file.filename)
            temp_dir = 'temp'
            os.makedirs(temp_dir, exist_ok=True)
            filepath = os.path.join(temp_dir, filename)
            file.save(filepath)
            
            base_name = os.path.splitext(filename)[0]
            processed_images, error, temp_extract_dir = process_brushset(filepath, base_name)
            
            if error:
                return jsonify({"message": error}), 400
            if not processed_images:
                return jsonify({"message": "No valid stamps (min 1024x1024) were found in the brushset."}), 400

            zip_filename = f"ArtyPacks_{base_name}.zip"
            zip_filepath = os.path.join(temp_dir, zip_filename)
            
            with zipfile.ZipFile(zip_filepath, 'w') as zf:
                for img_path in processed_images:
                    zf.write(img_path, os.path.basename(img_path))
            
            storage_path = f"{uuid.uuid4().hex}/{zip_filename}"
            with open(zip_filepath, 'rb') as f:
                supabase.storage.from_('conversions').upload(storage_path, f)
            
            download_url = supabase.storage.from_('conversions').get_public_url(storage_path)
            
            supabase.table('conversions').insert({
                'license_key': license_key,
                'original_filename': filename,
                'download_url': download_url,
                'storage_path': storage_path
            }).execute()

            os.remove(filepath)
            os.remove(zip_filepath)

            return jsonify({"downloadUrl": download_url})
        else:
            return jsonify({"message": "Invalid file type. Only .brushset files are allowed."}), 400
    except Exception as e:
        print(f"CRITICAL ERROR during file processing: {e}")
        return jsonify({"message": f"A critical error occurred during conversion."}), 500
    finally:
        if temp_extract_dir and os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir, ignore_errors=True)

# --- History Recovery Route ---
@api.route('/recover-link', methods=['POST'])
def recover_link():
    data = request.get_json()
    license_key = data.get('licenseKey')
    if not license_key:
        return jsonify([]), 200
    
    try:
        response = supabase.table('conversions').select(
            "original_filename, download_url, created_at"
        ).eq('license_key', license_key).order('created_at', desc=True).limit(5).execute()
        return jsonify(response.data), 200
    except Exception as e:
        print(f"History recovery error: {e}")
        return jsonify([]), 200

# --- Register Blueprint ---
app.register_blueprint(api)

# --- Helper Functions ---
def process_brushset(filepath, original_filename_base):
    temp_extract_dir = os.path.join('temp', f"processed_{uuid.uuid4().hex}")
    os.makedirs(temp_extract_dir, exist_ok=True)
    renamed_image_paths = []
    try:
        with zipfile.ZipFile(filepath, 'r') as brushset_zip:
            image_files = [name for name in brushset_zip.namelist() if name.lower().endswith(('.png', '.jpg',jpeg')) and 'artwork.png' not in name.lower()]
            image_files.sort()
            
            for i, img_name in enumerate(image_files):
                with brushset_zip.open(img_name) as img_file:
                    try:
                        with Image.open(img_file) as img:
                            if img.width >= 1024 and img.height >= 1024:
                                new_filename = f"{original_filename_base}_{i + 1}.png"
                                new_filepath = os.path.join(temp_extract_dir, new_filename)
                                img.save(new_filepath)
                                renamed_image_paths.append(new_filepath)
                    except (IOError, SyntaxError):
                        continue
        return renamed_image_paths, None, temp_extract_dir
    except zipfile.BadZipFile:
        return None, "A provided file seems to be corrupted or isn't a valid .brushset.", temp_extract_dir
    except Exception as e:
        print(f"Error in process_brushset: {e}")
        return None, "Failed to process the brushset file.", temp_extract_dir

@app.route('/')
def index():
    # Health check route for uptime monitors
    return "Artypacks Converter Backend is running."

if __name__ == '__main__':
    if not os.path.exists('temp'):
        os.makedirs('temp')
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, host='0.0.0.0', port=port)
