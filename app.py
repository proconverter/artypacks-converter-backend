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
                path
