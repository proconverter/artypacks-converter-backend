import os
import uuid
import zipfile
import shutil
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from PIL import Image
from supabase import create_client, Client
from flask_cors import CORS

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
    "http://127.0.0.1:5500" # Added for local development if needed
]
CORS(app, origins=allowed_origins, supports_credentials=True  )

# --- Temporary Storage Configuration ---
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- License Check Route (Unchanged) ---
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

# --- Main Conversion Route (REBUILT FOR STATELESS OPERATION) ---
@app.route('/convert', methods=['POST'])
def convert_file_stateless():
    # 1. Check for license key and file in the form data
    license_key = request.form.get('licenseKey')
    if not license_key:
        return jsonify({"message": "Missing license key."}), 401

    if 'file' not in request.files:
        return jsonify({"message": "No file was included in the request."}), 400
    
    file = request.files.get('file')
    if not file or file.filename == '':
        return jsonify({"message": "No file selected."}), 400

    # 2. Validate license and decrement credit BEFORE processing
    try:
        decrement_response = supabase.rpc('use_one_credit', {'p_license_key': license_key}).execute()
        if not decrement_response.data or not decrement_response.data[0].get('success'):
             message = decrement_response.data[0].get('message', 'Invalid license or no credits remaining.')
             return jsonify({"message": message}), 403
    except Exception as e:
        print(f"CRITICAL ERROR in /convert during credit use: {e}")
        return jsonify({"message": "Failed to update credits due to a database error. Please try again."}), 500

    # 3. Process the file
    temp_dirs_to_clean = []
    processed_images = []
    
    try:
        if file and file.filename.endswith('.brushset'):
            filename = secure_filename(file.filename)
            # Save file to a unique temporary directory to avoid conflicts
            session_dir = os.path.join(UPLOAD_FOLDER, f"session_{uuid.uuid4().hex}")
            os.makedirs(session_dir, exist_ok=True)
            temp_dirs_to_clean.append(session_dir)

            filepath = os.path.join(session_dir, filename)
            file.save(filepath)
            
            base_name = os.path.splitext(filename)[0]
            # Pass the session_dir to the processing function
            processed_images, error, output_dir = process_brushset(filepath, base_name, session_dir)
            
            if error:
                shutil.rmtree(session_dir, ignore_errors=True)
                return jsonify({"message": error}), 400
            if output_dir: temp_dirs_to_clean.append(output_dir)
        else:
            return jsonify({"message": "Invalid file type. Only .brushset files are accepted."}), 400

    except Exception as e:
        print(f"CRITICAL ERROR during file processing: {e}")
        for d in temp_dirs_to_clean: shutil.rmtree(d, ignore_errors=True)
        return jsonify({"message": f"A critical error occurred while processing the file: {str(e)}"}), 500

    # 4. Check if any images were actually extracted
    if not processed_images:
        for d in temp_dirs_to_clean: shutil.rmtree(d, ignore_errors=True)
        return jsonify({"message": "No valid stamps (min 1024x1024) were found in the brushset."}), 400

    # 5. Create the ZIP file
    zip_base_filename = f"ArtyPacks_{base_name}.zip"
    zip_filename = secure_filename(zip_base_filename)
    zip_filepath = os.path.join(UPLOAD_FOLDER, zip_filename)
    
    with zipfile.ZipFile(zip_filepath, 'w') as zf:
        for img_path in processed_images:
            zf.write(img_path, os.path.basename(img_path))
    
    # 6. Clean up all temporary directories
    for d in temp_dirs_to_clean:
        shutil.rmtree(d, ignore_errors=True)
    
    # 7. Return the download URL
    backend_url = request.host_url.rstrip('/')
    return jsonify({"downloadUrl": f"{backend_url}/download/{zip_filename}"})


# --- Helper Functions (Modified to use session directories) ---
def process_brushset(filepath, original_filename_base, session_dir):
    # Use subdirectories within the unique session directory
    temp_extract_dir = os.path.join(session_dir, f"extract_{uuid.uuid4().hex}")
    os.makedirs(temp_extract_dir, exist_ok=True)
    
    output_dir = os.path.join(session_dir, f"processed_{uuid.uuid4().hex}")
    os.makedirs(output_dir, exist_ok=True)
    
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
        for i, img_path in enumerate(image_files):
            new_filename = f"{original_filename_base}_{i + 1}.png"
            new_filepath = os.path.join(output_dir, new_filename)
            shutil.copy(img_path, new_filepath)
            renamed_image_paths.append(new_filepath)
            
        return renamed_image_paths, None, output_dir
    except zipfile.BadZipFile:
        return None, "A provided file seems to be corrupted or isn't a valid .brushset.", output_dir
    finally:
        # The parent session_dir will be cleaned up by the main function
        pass

@app.route('/download/<filename>')
def download_file(filename):
    # This function now needs to handle cleaning up the zip file after sending it
    # For simplicity in this stateless model, a separate cleanup job would be ideal.
    # For now, we send it directly.
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)

@app.route('/')
def index():
    return "Artypacks Converter Backend is running."

if __name__ == '__main__':
    # Use a different port for local testing if needed, e.g., port=5001
    app.run(debug=True, port=os.environ.get('PORT', 5001))
