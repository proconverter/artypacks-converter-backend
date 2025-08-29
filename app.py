import os
import uuid
import zipfile
import shutil
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from PIL import Image
from supabase import create_client, Client
from flask_cors import CORS # Import CORS

# --- Flask App Initialization ---
app = Flask(__name__)

# --- Configuration ---
# Allow requests only from your specific frontend domain
CORS(app, resources={r"/convert": {"origins": "https://www.artypacks.app"}} )

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- Supabase Configuration ---
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY') # Use the Service Role Key for backend operations

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL and Service Key must be set in environment variables.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Brushset Processing Function (with Renaming Logic) ---
def process_brushset(filepath, original_filename_base):
    temp_extract_dir = os.path.join(UPLOAD_FOLDER, f"extract_{uuid.uuid4().hex}")
    os.makedirs(temp_extract_dir, exist_ok=True)
    
    renamed_image_paths = []
    try:
        with zipfile.ZipFile(filepath, 'r') as brushset_zip:
            brushset_zip.extractall(temp_extract_dir)
            
            image_files = []
            # First, find all valid images
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
            
            # Sort files to ensure consistent numbering (e.g., by name)
            image_files.sort()
            
            # Now, rename and move them
            output_dir = os.path.join(UPLOAD_FOLDER, f"processed_{uuid.uuid4().hex}")
            os.makedirs(output_dir, exist_ok=True)

            for i, img_path in enumerate(image_files):
                new_filename = f"{original_filename_base}_{i + 1}.png"
                new_filepath = os.path.join(output_dir, new_filename)
                shutil.copy(img_path, new_filepath)
                renamed_image_paths.append(new_filepath)

        shutil.rmtree(temp_extract_dir, ignore_errors=True) # Clean up extraction folder
        return renamed_image_paths, None, output_dir

    except zipfile.BadZipFile:
        shutil.rmtree(temp_extract_dir, ignore_errors=True)
        return None, "A provided file seems to be corrupted or isn't a valid .brushset.", None
    except Exception as e:
        print(f"Error processing brushset: {e}")
        shutil.rmtree(temp_extract_dir, ignore_errors=True)
        return None, "An unexpected error occurred during file processing.", None

# --- Main Conversion Route ---
@app.route('/convert', methods=['POST'])
def convert_files():
    # 1. License Key Validation
    license_key = request.form.get('licenseKey')
    if not license_key:
        return jsonify({"error": "Missing license key."}), 401

    try:
        # Check key and decrement in one go
        response = supabase.rpc('decrement_license', {'p_license_key': license_key}).execute()
        
        if not response.data or not response.data[0].get('success'):
             # The RPC function can return a specific message
             message = response.data[0].get('message', 'Invalid or expired license.')
             return jsonify({"error": message}), 403

    except Exception as e:
        print(f"Supabase RPC error: {e}")
        return jsonify({"error": "Could not validate license. Please try again."}), 500

    # 2. File Handling
    if 'files' not in request.files:
        return jsonify({"error": "No files were uploaded."}), 400

    files = request.files.getlist('files')
    if not files or files[0].filename == '':
        return jsonify({"error": "No selected files."}), 400

    all_processed_images = []
    temp_dirs_to_clean = []

    # 3. Process each file
    for file in files:
        if file and file.filename.endswith('.brushset'):
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)

            base_name = os.path.splitext(filename)[0]
            
            processed_images, error, output_dir = process_brushset(filepath, base_name)
            
            os.remove(filepath) # Clean up original upload

            if error:
                # Clean up any directories created so far before exiting
                for d in temp_dirs_to_clean: shutil.rmtree(d, ignore_errors=True)
                return jsonify({"error": error}), 400
            
            all_processed_images.extend(processed_images)
            if output_dir: temp_dirs_to_clean.append(output_dir)

    # 4. Zip all processed files together
    if not all_processed_images:
        return jsonify({"error": "No valid stamps (min 1024x1024) were found in the provided files."}), 400

    zip_filename = f"artypacks_conversion_{uuid.uuid4().hex}.zip"
    zip_filepath = os.path.join(UPLOAD_FOLDER, zip_filename)
    
    with zipfile.ZipFile(zip_filepath, 'w') as zf:
        for img_path in all_processed_images:
            zf.write(img_path, os.path.basename(img_path))

    # 5. Clean up temporary directories
    for d in temp_dirs_to_clean:
        shutil.rmtree(d, ignore_errors=True)

    # Return the URL for the zip file
    return jsonify({"downloadUrl": f"/download/{zip_filename}"})

# --- Download Route ---
@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)

# --- Supabase Helper Function (Important!) ---
# You need to create this function in your Supabase SQL Editor:
# Go to Database -> Functions -> Create a new function
"""
CREATE OR REPLACE FUNCTION decrement_license(p_license_key TEXT)
RETURNS TABLE(success BOOLEAN, message TEXT) AS $$
DECLARE
  key_data RECORD;
BEGIN
  -- Find the key
  SELECT * INTO key_data FROM licenses
  WHERE license_key = p_license_key;

  -- Check if key exists
  IF NOT FOUND THEN
    RETURN QUERY SELECT FALSE, 'License key not found.';
    RETURN;
  END IF;

  -- Check if active
  IF NOT key_data.is_active THEN
    RETURN QUERY SELECT FALSE, 'This license is not active.';
    RETURN;
  END IF;

  -- Check for remaining sessions
  IF key_data.sessions_remaining <= 0 THEN
    RETURN QUERY SELECT FALSE, 'This license has no conversions left.';
    RETURN;
  END IF;

  -- Decrement the sessions
  UPDATE licenses
  SET sessions_remaining = sessions_remaining - 1
  WHERE license_key = p_license_key;

  RETURN QUERY SELECT TRUE, 'License validated and decremented.';
END;
$$ LANGUAGE plpgsql;
"""
