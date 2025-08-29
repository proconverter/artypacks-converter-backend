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

# --- Configuration ---
# Allow requests from your specific frontend domain for all routes
CORS(app, resources={r"/*": {"origins": "https://www.artypacks.app"}} )

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- Supabase Configuration ---
# Ensure these are set as environment variables in Render
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')

if not SUPABASE_URL or not SUPABASE_KEY:
    # This will cause a clean error on startup if variables are missing
    raise ValueError("Supabase URL and Service Key must be set in environment variables.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Brushset Processing Function ---
def process_brushset(filepath, original_filename_base):
    """Extracts, validates, and renames images from a .brushset file."""
    temp_extract_dir = os.path.join(UPLOAD_FOLDER, f"extract_{uuid.uuid4().hex}")
    os.makedirs(temp_extract_dir, exist_ok=True)
    
    renamed_image_paths = []
    try:
        with zipfile.ZipFile(filepath, 'r') as brushset_zip:
            brushset_zip.extractall(temp_extract_dir)
            
            image_files = []
            # Find all valid images (PNGs/JPEGs >= 1024x1024)
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
            
            # Sort files to ensure consistent numbering
            image_files.sort()
            
            # Create a dedicated output directory for this job's processed images
            output_dir = os.path.join(UPLOAD_FOLDER, f"processed_{uuid.uuid4().hex}")
            os.makedirs(output_dir, exist_ok=True)

            # Rename and copy the valid images
            for i, img_path in enumerate(image_files):
                new_filename = f"{original_filename_base}_{i + 1}.png"
                new_filepath = os.path.join(output_dir, new_filename)
                shutil.copy(img_path, new_filepath)
                renamed_image_paths.append(new_filepath)

        return renamed_image_paths, None, output_dir

    except zipfile.BadZipFile:
        return None, "A provided file seems to be corrupted or isn't a valid .brushset.", None
    except Exception as e:
        print(f"Error processing brushset: {e}")
        return None, "An unexpected error occurred during file processing.", None
    finally:
        # Clean up the temporary extraction folder
        shutil.rmtree(temp_extract_dir, ignore_errors=True)

# --- License Check Route ---
@app.route('/check-license', methods=['POST'])
def check_license():
    """Checks the validity and credit status of a license key."""
    data = request.get_json()
    if not data:
        return jsonify({"message": "Invalid request. No JSON data received."}), 400

    license_key = data.get('licenseKey')
    if not license_key:
        return jsonify({"message": "Missing license key in request."}), 400

    try:
        response = supabase.rpc('get_license_status', {'p_license_key': license_key}).execute()
        
        if not response.data:
            return jsonify({"isValid": False, "message": "License key not found."}), 404

        result = response.data[0]
        is_valid = result.get('is_valid')
        credits = result.get('sessions_remaining')
        message = result.get('message')

        return jsonify({"isValid": is_valid, "credits": credits, "message": message})

    except Exception as e:
        print(f"Supabase RPC error on /check-license: {e}")
        return jsonify({"message": "Could not validate license due to a server error."}), 500

# --- Main Conversion Route ---
@app.route('/convert', methods=['POST'])
def convert_files():
    """Validates license, decrements credit, and converts uploaded files."""
    # 1. License Key Validation (reading from form data)
    license_key = request.form.get('licenseKey')
    if not license_key:
        return jsonify({"message": "Missing license key."}), 401

    try:
        # Use the atomic 'decrement_license' function
        response = supabase.rpc('decrement_license', {'p_license_key': license_key}).execute()
        
        if not response.data or not response.data[0].get('success'):
             message = response.data[0].get('message', 'Invalid or expired license.')
             return jsonify({"message": message}), 403

    except Exception as e:
        print(f"Supabase RPC error during conversion: {e}")
        return jsonify({"message": "Could not validate license. Please try again."}), 500

    # 2. File Handling (license is now confirmed and decremented)
    if 'files' not in request.files:
        return jsonify({"message": "No files were uploaded."}), 400

    files = request.files.getlist('files')
    if not files or files[0].filename == '':
        return jsonify({"message": "No selected files."}), 400

    all_processed_images = []
    temp_dirs_to_clean = []

    # 3. Process each uploaded file
    for file in files:
        if file and file.filename.endswith('.brushset'):
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)

            base_name = os.path.splitext(filename)[0]
            
            processed_images, error, output_dir = process_brushset(filepath, base_name)
            
            os.remove(filepath) # Clean up original upload immediately

            if error:
                for d in temp_dirs_to_clean: shutil.rmtree(d, ignore_errors=True)
                return jsonify({"message": error}), 400
            
            all_processed_images.extend(processed_images)
            if output_dir: temp_dirs_to_clean.append(output_dir)

    # 4. Zip all processed files from this job together
    if not all_processed_images:
        return jsonify({"message": "No valid stamps (min 1024x1024) were found in the provided files."}), 400

    zip_filename = f"artypacks_conversion_{uuid.uuid4().hex}.zip"
    zip_filepath = os.path.join(UPLOAD_FOLDER, zip_filename)
    
    with zipfile.ZipFile(zip_filepath, 'w') as zf:
        for img_path in all_processed_images:
            zf.write(img_path, os.path.basename(img_path))

    # 5. Clean up temporary directories containing the processed PNGs
    for d in temp_dirs_to_clean:
        shutil.rmtree(d, ignore_errors=True)

    # 6. Return the URL for the final zip file
    return jsonify({"downloadUrl": f"/download/{zip_filename}"})

# --- Download Route ---
@app.route('/download/<filename>')
def download_file(filename):
    """Serves the generated zip file for download."""
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)

# --- Health Check Route ---
@app.route('/')
def index():
    """A simple health check endpoint."""
    return "Artypacks Converter Backend is running."

# This is for local development testing; Gunicorn will run the app in production.
if __name__ == '__main__':
    app.run(debug=True, port=5001)
