import os
import uuid
import zipfile
import shutil
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from PIL import Image
from supabase import create_client, Client
from flask_cors import CORS
# We need the logging library to be imported at the top level
import logging

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

# --- License Check Route (Unchanged) ---
@app.route('/check-license', methods=['POST'])
def check_license():
    data = request.get_json()
    if not data or 'licenseKey' not in data:
        return jsonify({"message": "Invalid request."}), 400
    
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
        })
    except Exception as e:
        print(f"Supabase RPC error on /check-license: {e}")
        return jsonify({"message": "Could not validate license due to a server error."}), 500

# --- Main Conversion Route (with your new logging) ---
@app.route('/convert', methods=['POST'])
def convert_files():
    # --- START: Enhanced Logging ---
    logging.basicConfig(level=logging.INFO) # Ensures logs are captured

    license_key = request.form.get('licenseKey')
    if not license_key:
        return jsonify({"message": "Missing license key."}), 401

    # --- THIS IS THE FUNCTION NAME TO VERIFY ---
    # Make sure this is the name of the NEW, corrected function in Supabase.
    # For example: 'use_one_credit'
    function_to_call = 'decrement_license' 
    
    logging.info(f"--- DIAGNOSTIC LOG: Attempting to call RPC function: '{function_to_call}' ---")
    # --- END: Enhanced Logging ---

    try:
        # Use the variable here to make it easy to change
        response = supabase.rpc(function_to_call, {'p_license_key': license_key}).execute()
        
        # --- START: Detailed Response Logging ---
        logging.info(f"Supabase RPC call successful. Response data: {response.data}")
        # --- END: Detailed Response Logging ---

        if not response.data or not response.data[0].get('success'):
             message = response.data[0].get('message', 'Invalid or expired license.')
             logging.warning(f"RPC call to '{function_to_call}' was not successful. Message: {message}")
             return jsonify({"message": message}), 403

    except Exception as e:
        # --- START: Critical Error Logging ---
        # This will log the full, detailed traceback of the PostgreSQL error
        logging.error(f"--- DIAGNOSTIC LOG: Supabase RPC error calling '{function_to_call}' ---", exc_info=True)
        # --- END: Critical Error Logging ---
        return jsonify({"message": "Could not validate license. Please try again."}), 500

    # ... (the rest of your file processing code remains unchanged) ...
    if 'files' not in request.files:
        return jsonify({"message": "No files were uploaded."}), 400
    files = request.files.getlist('files')
    if not files or files[0].filename == '':
        return jsonify({"message": "No selected files."}), 400

    all_processed_images = []
    temp_dirs_to_clean = []
    UPLOAD_FOLDER = 'uploads'
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    
    first_brushset_name = "Conversion"
    if files and files[0].filename:
        safe_name = secure_filename(files[0].filename)
        first_brushset_name = os.path.splitext(safe_name)[0]

    for file in files:
        if file and file.filename.endswith('.brushset'):
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            base_name = os.path.splitext(filename)[0]
            processed_images, error, output_dir = process_brushset(filepath, base_name)
            os.remove(filepath)
            if error:
                for d in temp_dirs_to_clean: shutil.rmtree(d, ignore_errors=True)
                return jsonify({"message": error}), 400
            all_processed_images.extend(processed_images)
            if output_dir: temp_dirs_to_clean.append(output_dir)

    if not all_processed_images:
        return jsonify({"message": "No valid stamps (min 1024x1024) were found."}), 400

    suffix = "-and-more" if len(files) > 1 else ""
    zip_base_filename = f"ArtyPacks_{first_brushset_name}{suffix}.zip"
    zip_filename = secure_filename(zip_base_filename)
    
    zip_filepath = os.path.join(UPLOAD_FOLDER, zip_filename)
    with zipfile.ZipFile(zip_filepath, 'w') as zf:
        for img_path in all_processed_images:
            zf.write(img_path, os.path.basename(img_path))
    for d in temp_dirs_to_clean:
        shutil.rmtree(d, ignore_errors=True)
    
    backend_url = request.host_url.rstrip('/')
    return jsonify({"downloadUrl": f"{backend_url}/download/{zip_filename}"})


# --- Helper Functions (Unchanged) ---
def process_brushset(filepath, original_filename_base):
    temp_extract_dir = os.path.join('uploads', f"extract_{uuid.uuid4().hex}")
    os.makedirs(temp_extract_dir, exist_ok=True)
    renamed_image_paths = []
    try:
        with zipfile.ZipFile(filepath, 'r') as brushset_zip:
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
        output_dir = os.path.join('uploads', f"processed_{uuid.uuid4().hex}")
        os.makedirs(output_dir, exist_ok=True)
        for i, img_path in enumerate(image_files):
            new_filename = f"{original_filename_base}_{i + 1}.png"
            new_filepath = os.path.join(output_dir, new_filename)
            shutil.copy(img_path, new_filepath)
            renamed_image_paths.append(new_filepath)
        return renamed_image_paths, None, output_dir
    except zipfile.BadZipFile:
        return None, "A provided file seems to be corrupted or isn't a valid .brushset.", None
    finally:
        shutil.rmtree(temp_extract_dir, ignore_errors=True)

@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory('uploads', filename, as_attachment=True)

@app.route('/')
def index():
    return "Artypacks Converter Backend is running."

if __name__ == '__main__':
    app.run(debug=True, port=5001)
