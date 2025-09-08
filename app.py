import os
import uuid
import zipfile
import shutil
import io
import requests
from flask import Flask, request, jsonify, send_file
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
CORS(app, resources={r"/*": {"origins": allowed_origins}}, supports_credentials=True, expose_headers=["Content-Disposition"] )

# --- Main Conversion Route ---
@app.route('/convert', methods=['POST'])
def convert_files():
    license_key = request.form.get('licenseKey')
    if not license_key:
        return jsonify({"message": "Missing license key."}), 401

    try:
        with engine.connect() as connection:
            trans = connection.begin()
            try:
                result = connection.execute(text("SELECT * FROM use_one_credit(:p_license_key)"), {'p_license_key': license_key}).fetchone()
                if not result or not result[0]:
                    message = result[1] if result else 'Invalid license or no credits remaining.'
                    trans.rollback()
                    return jsonify({"message": message}), 403
                trans.commit()
            except Exception:
                trans.rollback()
                raise
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
            
            zip_buffer, error = process_brushset(filepath)
            if error:
                return jsonify({"message": error}), 400

            base_name = os.path.splitext(original_filename)[0]
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            zip_filename_for_storage = f"ArtyPacks.app_{base_name}_{timestamp}.zip"

            supabase.storage.from_("conversions").upload(
                file=zip_buffer.getvalue(),
                path=zip_filename_for_storage,
                file_options={"content-type": "application/zip"}
            )
            
            public_url = supabase.storage.from_("conversions").get_public_url(zip_filename_for_storage)

            with engine.connect() as connection:
                connection.execute(text(
                    "INSERT INTO conversions (license_key, original_filename, download_url) VALUES (:key, :orig_name, :url)"
                ), {'key': license_key, 'orig_name': original_filename, 'url': public_url})
                connection.commit()

            return jsonify({
                "downloadUrl": public_url,
                "originalFilename": original_filename
            })
        else:
            return jsonify({"message": "Invalid file type. Only .brushset files are allowed."}), 400
    except Exception as e:
        print(f"CRITICAL ERROR during file processing or upload: {e}")
        return jsonify({"message": "A critical error occurred while processing the file."}), 500
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

# --- License Check Route ---
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
            
            response_data = {
                "isValid": result[0],
                "sessions_remaining": result[1],
                "message": result[2],
                "user_type": result[3]
            }
            return jsonify(response_data), 200
    except Exception as e:
        print(f"CRITICAL ERROR in /check-license: {e}")
        return jsonify({"message": "A server error occurred while validating the license."}), 500

# --- Session Recovery Route (FINAL VERSION) ---
@app.route('/recover-session', methods=['POST'])
def recover_session():
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
                ORDER BY created_at ASC
            """)
            results = connection.execute(query, {'key': license_key}).fetchall()

            if not results:
                return jsonify({"message": "No recent conversions found for this license."}), 404

            if len(results) > 1:
                # More than one file found, this is a multi-file session
                files_data = [{"originalFilename": row[0], "downloadUrl": row[1]} for row in results]
                return jsonify({
                    "session_type": "multi",
                    "files": files_data
                }), 200
            else:
                # Exactly one file found, this is a single-file session
                return jsonify({
                    "session_type": "single",
                    "original_filename": results[0][0],
                    "download_url": results[0][1]
                }), 200

    except Exception as e:
        print(f"CRITICAL ERROR in /recover-session: {e}")
        return jsonify({"message": "A server error occurred while recovering the session."}), 500

# --- Download All Route ---
@app.route('/download-all', methods=['POST'])
def download_all():
    data = request.get_json()
    urls = data.get('urls')
    if not urls or not isinstance(urls, list):
        return jsonify({"message": "A list of URLs is required."}), 400

    master_zip_buffer = io.BytesIO()
    with zipfile.ZipFile(master_zip_buffer, 'w', zipfile.ZIP_DEFLATED) as master_zf:
        for url in urls:
            try:
                response = requests.get(url, stream=True)
                response.raise_for_status()
                
                # Clean the URL to create a valid filename
                base_name_with_ext = url.split('/')[-1]
                base_name = base_name_with_ext.split('?')[0]
                
                master_zf.writestr(base_name, response.content)
            except requests.exceptions.RequestException as e:
                print(f"Warning: Could not download file from {url}. Error: {e}")
                continue

    master_zip_buffer.seek(0)
    
    # Create a human-readable filename for the master ZIP
    batch_timestamp = datetime.now(timezone.utc).strftime("%a_%b_%d_%I%M%p")
    master_zip_filename = f"ArtyPacks.app_Batch_{batch_timestamp}.zip"

    return send_file(
        master_zip_buffer,
        as_attachment=True,
        download_name=master_zip_filename,
        mimetype='application/zip'
    )

# --- Helper Functions ---
def process_brushset(filepath):
    temp_extract_dir = os.path.join('temp', f"extract_{uuid.uuid4().hex}")
    os.makedirs(temp_extract_dir, exist_ok=True)
    
    try:
        with zipfile.ZipFile(filepath, 'r') as brushset_zip:
            image_files = [name for name in brushset_zip.namelist() if name.lower().endswith(('.png', '.jpg', '.jpeg')) and 'artwork.png' not in name.lower()]
            
            valid_images_data = []
            for image_file_name in image_files:
                with brushset_zip.open(image_file_name) as img_file:
                    img_data = io.BytesIO(img_file.read())
                    try:
                        with Image.open(img_data) as img:
                            if img.width >= 1024 and img.height >= 1024:
                                img_data.seek(0)
                                valid_images_data.append(img_data.read())
                    except Exception:
                        continue # Ignore files that are not valid images

            if not valid_images_data:
                return None, "No valid stamp images (>= 1024x1024px) were found in the brushset."

            original_brushset_name = os.path.splitext(os.path.basename(filepath))[0]
            root_folder_name = f"ArtyPacks.app_{original_brushset_name}"

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                for i, img_content in enumerate(valid_images_data):
                    image_filename_in_zip = f"{original_brushset_name}_{i + 1}.png"
                    full_path_in_zip = os.path.join(root_folder_name, image_filename_in_zip)
                    zf.writestr(full_path_in_zip, img_content)
            
            zip_buffer.seek(0)
            return zip_buffer, None
    except zipfile.BadZipFile:
        return None, "The provided file seems to be corrupted or isn't a valid .brushset."
    except Exception as e:
        print(f"Error in process_brushset: {e}")
        return None, "Failed to process the brushset file."
    finally:
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir, ignore_errors=True)

@app.route('/')
def index():
    return "Artypacks Converter Backend is running."
