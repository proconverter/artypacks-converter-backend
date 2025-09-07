import os
import uuid
import zipfile
import shutil
import io
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from PIL import Image
from supabase import create_client, Client
from flask_cors import CORS
from sqlalchemy import create_engine, text
from datetime import datetime, timezone

# --- Flask App Initialization ---
app = Flask(__name__)

# --- Database Configuration ---
db_url = os.environ.get('SUPABASE_DB_URL')
if not db_url:
    raise ValueError("SUPABASE_DB_URL must be set in environment variables.")
engine = create_engine(db_url)

# --- Supabase Client Initialization ---
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL and Service Key must be set in environment variables.")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- CORS Configuration ---
allowed_origins = [
    "https://procreate-landing-page-sandbox.onrender.com",
    "https://artypacks.app",
    "http://127.0.0.1:5500"
]
CORS(app, resources={r"/*": {"origins": allowed_origins}}, supports_credentials=True  )

# --- Main Conversion Route ---
@app.route('/convert', methods=['POST'])
def convert_files():
    license_key = request.form.get('licenseKey')
    if not license_key:
        return jsonify({"message": "Missing license key."}), 401

    try:
        with engine.connect() as connection:
            with connection.begin():
                result = connection.execute(text("SELECT * FROM use_one_credit(:p_license_key)"), {'p_license_key': license_key}).fetchone()
            
            if not result or not result[0]:
                message = result[1] if result else 'Invalid license or no credits remaining.'
                return jsonify({"message": message}), 403
    except Exception as e:
        print(f"CRITICAL ERROR in /convert during credit use: {e}")
        return jsonify({"message": "Failed to update credits due to a database error."}), 500

    if 'file' not in request.files:
        return jsonify({"message": "No file was uploaded."}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"message": "No selected file."}), 400

    temp_dir = os.path.join('temp', str(uuid.uuid4()))
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        if file and file.filename.endswith('.brushset'):
            original_filename = secure_filename(file.filename)
            filepath = os.path.join(temp_dir, original_filename)
            file.save(filepath)
            
            zip_buffer, error = process_brushset(filepath, original_filename)
            if error:
                return jsonify({"message": error}), 400

            base_name = original_filename.replace('.brushset', '')
            final_zip_filename = f"ArtyPacks.app_{base_name}.zip"

            supabase.storage.from_("conversions").upload(
                file=zip_buffer.getvalue(), 
                path=final_zip_filename,
                file_options={"content-type": "application/zip"}
            )
            
            public_url_data = supabase.storage.from_("conversions").get_public_url(final_zip_filename)
            public_url = public_url_data

            with engine.connect() as connection:
                with connection.begin():
                    connection.execute(text(
                        "INSERT INTO conversions (license_key, original_filename, download_url) VALUES (:key, :orig_name, :url)"
                    ), {'key': license_key, 'orig_name': original_filename, 'url': public_url})

            return jsonify({"downloadUrl": public_url, "originalFilename": original_filename})
        else:
            return jsonify({"message": "Invalid file type. Only .brushset files are allowed."}), 400
    except Exception as e:
        print(f"CRITICAL ERROR during file processing or upload: {e}")
        return jsonify({"message": "A critical error occurred while processing the file."}), 500
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

# --- License Check and Recovery Routes ---
@app.route('/check-license', methods=['POST'])
def check_license():
    data = request.get_json()
    if not data or 'licenseKey' not in data:
        return jsonify({"message": "Invalid request: Missing license key."}), 400
    
    license_key = data['licenseKey']
    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT * FROM get_license_status(:p_license_key)"), {'p_license_key': license_key}).fetchone()
            if not result:
                return jsonify({"isValid": False, "message": "License key not found."}), 404
            
            # ==================================================================
            # THIS IS THE FIX - The dictionary now correctly maps all 4 columns
            # ==================================================================
            response_data = {
                "isValid": result[0],
                "sessions_remaining": result[1],
                "message": result[2],
                "user_type": result[3] # Correctly access the 4th item (index 3)
            }
            return jsonify(response_data), 200
    except Exception as e:
        print(f"CRITICAL ERROR in /check-license: {e}")
        return jsonify({"message": "A server error occurred while validating the license."}), 500

@app.route('/recover-link', methods=['POST'])
def recover_link():
    data = request.get_json()
    license_key = data.get('licenseKey')
    if not license_key:
        return jsonify({"message": "License key is required."}), 400

    try:
        with engine.connect() as connection:
            query = text("""
                SELECT original_filename, download_url 
                FROM conversions 
                WHERE license_key = :key 
                AND created_at >= NOW() - INTERVAL '60 minutes'
                ORDER BY created_at DESC 
                LIMIT 1
            """)
            result = connection.execute(query, {'key': license_key}).fetchone()

            if result:
                response_data = {
                    "original_filename": result[0],
                    "download_url": result[1]
                }
                return jsonify(response_data), 200
            else:
                return jsonify({"message": "No recent conversion found for this license."}), 404
    except Exception as e:
        print(f"CRITICAL ERROR in /recover-link: {e}")
        return jsonify({"message": "A server error occurred while recovering the link."}), 500

# --- Helper Functions ---
def process_brushset(filepath, original_filename):
    temp_extract_dir = os.path.join('temp', f"extract_{uuid.uuid4().hex}")
    os.makedirs(temp_extract_dir, exist_ok=True)
    
    try:
        with zipfile.ZipFile(filepath, 'r') as brushset_zip:
            image_files = [name for name in brushset_zip.namelist() if name.lower().endswith(('.png', '.jpg', '.jpeg')) and 'artwork.png' not in name.lower()]
            if not image_files:
                return None, "No valid stamp images were found in the brushset."

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                base_folder_name = original_filename.replace('.brushset', '')
                
                for i, image_file_name in enumerate(image_files):
                    with brushset_zip.open(image_file_name) as img_file:
                        img_data = io.BytesIO(img_file.read())
                        with Image.open(img_data) as img:
                            if img.width < 1024 or img.height < 1024:
                                continue
                        
                        img_data.seek(0)
                        new_filename_in_zip = f"{base_folder_name}/stamp_{i + 1}.png"
                        zf.writestr(new_filename_in_zip, img_data.read())
            
            zip__buffer.seek(0)
            return zip_buffer, None
    except zipfile.BadZipFile:
        return None, "A provided file seems to be corrupted or isn't a valid .brushset."
    except Exception as e:
        print(f"Error in process_brushset: {e}")
        return None, "Failed to process the brushset file."
    finally:
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir, ignore_errors=True)

@app.route('/')
def index():
    return "Artypacks Converter Backend is running."

if __name__ == '__main__':
    if not os.path.exists('temp'):
        os.makedirs('temp')
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, host='0.0.0.0', port=port)
