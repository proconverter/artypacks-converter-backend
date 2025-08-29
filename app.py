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
# Allow requests from your specific frontend domain.
# For local testing, you might use "http://127.0.0.1:5500" or "*"
CORS(app, resources={r"/*": {"origins": "https://www.artypacks.app"}} )

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# --- Supabase Configuration ---
# Make sure these are set in your hosting environment (e.g., Vercel, Heroku)
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY') # IMPORTANT: Use the Service Role Key

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL and Service Key must be set in environment variables.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Brushset Processing Function (Your existing logic is good) ---
def process_brushset(filepath, original_filename_base):
    # ... (Your process_brushset function is well-structured, no changes needed here)
    # ... I'm omitting it for brevity, but it should be included in your file.
    pass # Placeholder for your existing function

# --- Main Conversion Route ---
@app.route('/convert', methods=['POST'])
def convert_files():
    # 1. License Key Validation
    license_key = request.form.get('licenseKey')
    if not license_key:
        return jsonify({"message": "Missing license key."}), 401

    try:
        # CORRECT: Call the RPC function you created
        response = supabase.rpc('decrement_license', {'p_license_key': license_key}).execute()
        
        # The RPC returns a list with one object, e.g., [{'success': True, 'message': '...'}]
        if not response.data or not response.data[0].get('success'):
             message = response.data[0].get('message', 'Invalid or expired license.')
             return jsonify({"message": message}), 403

    except Exception as e:
        print(f"Supabase RPC error: {e}")
        return jsonify({"message": "Could not validate license. Please try again."}), 500

    # 2. File Handling
    # CORRECT: The key from your JS FormData is 'brushsets'
    if 'brushsets' not in request.files:
        return jsonify({"message": "No files were uploaded."}), 400

    files = request.files.getlist('brushsets')
    if not files or files[0].filename == '':
        return jsonify({"message": "No selected files."}), 400

    all_processed_images = []
    temp_dirs_to_clean = []

    # 3. Process each file (Your logic here is solid)
    for file in files:
        if file and file.filename.endswith('.brushset'):
            # ... (The rest of your file processing logic)
            pass # Placeholder

    # 4. Zip all processed files together
    if not all_processed_images:
        return jsonify({"message": "No valid stamps (min 1024x1024) were found."}), 400

    zip_filename = f"artypacks_conversion_{uuid.uuid4().hex}.zip"
    zip_filepath = os.path.join(app.config['UPLOAD_FOLDER'], zip_filename)
    
    with zipfile.ZipFile(zip_filepath, 'w') as zf:
        for img_path in all_processed_images:
            zf.write(img_path, os.path.basename(img_path))

    # 5. Clean up temporary directories
    for d in temp_dirs_to_clean:
        shutil.rmtree(d, ignore_errors=True)

    # 6. Return the URL for the zip file
    # IMPORTANT: This URL must be the public URL where your backend is hosted
    # For example: https://your-backend-url.com/download/the_zip_file.zip
    # The JS expects a full URL, not a relative path.
    base_url = request.host_url 
    download_url = f"{base_url}download/{zip_filename}"
    
    return jsonify({"downloadUrl": download_url} )

# --- Download Route ---
@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)

# This is for running locally (e.g., python app.py)
if __name__ == '__main__':
    app.run(debug=True, port=5001) # Run on a different port than the frontend
