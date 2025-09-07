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
CORS(app, resources={r"/*": {"origins": allowed_origins}}, supports_credentials=True )

# --- Main Conversion Route ---
@app.route('/convert', methods=['POST'])
def convert_files():
    license_key = request.form.get('licenseKey')
    if not license_key:
        return jsonify({"message": "Missing license key."}), 401

    try:
        with engine.connect() as connection:
            # Begin a transaction
            trans = connection.begin()
            try:
                result = connection.execute(text("SELECT * FROM use_one_credit(:p_license_key)"), {'p_license_key': license_key}).fetchone()
                if not result or not result[0]:
                    message = result[1] if result else 'Invalid license or no credits remaining.'
                    trans.rollback() # Rollback if check fails
                    return jsonify({"message": message}), 403
                # If the check passes, commit the transaction
                trans.commit()
            except Exception:
                trans.rollback()
                raise # Re-raise the exception to be caught by the outer block
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

            # *** THIS IS THE FIX: Add a timestamp to the filename ***
            base_name = os.path.splitext(original_filename)[0]
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            zip_filename_for_storage = f"ArtyPacks.app_{base_name}_{timestamp}.zip"

            supabase.storage.from_("conversions").upload(
                file=zip_buffer.getvalue(),
                path=zip_filename_for_storage,
                file_options={"content-type": "application/zip"}
            )
            
            public_url_data = supabase.storage.from_("conversions").get_public_url(zip_filename_for_storage)
            public_url = public_url_data

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
            
            response_data = {
                "isValid": result[0],
                "sessions_remaining": result[1],
                "message": result[2],
                "user_type": result[3]
            }
            return jsonify(response_data), 200
    except Exception as e:
        print(f"CRITICAL ERROR in /check-license: {e
