import os
import uuid
import datetime
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, flash, abort, jsonify
from werkzeug.utils import secure_filename
import shutil

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB limit per bin
app.config['DATABASE'] = 'smplbin.db'

# Create uploads directory if it doesn't exist
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

def get_db():
    db = sqlite3.connect(app.config['DATABASE'])
    db.row_factory = sqlite3.Row
    return db

def init_db():
    with app.app_context():
        db = get_db()
        with app.open_resource('schema.sql') as f:
            db.executescript(f.read().decode('utf8'))
        db.commit()

def query_db(query, args=(), one=False):
    db = get_db()
    cur = db.execute(query, args)
    rv = cur.fetchall()
    db.close()
    return (rv[0] if rv else None) if one else rv

def modify_db(query, args=()):
    db = get_db()
    cur = db.execute(query, args)
    db.commit()
    lastrowid = cur.lastrowid
    db.close()
    return lastrowid

# Initialize the database if it doesn't exist
if not os.path.exists(app.config['DATABASE']):
    with open('schema.sql', 'w') as f:
        f.write('''
        CREATE TABLE bins (
            id TEXT PRIMARY KEY,
            upload_date TIMESTAMP NOT NULL,
            total_size INTEGER NOT NULL
        );
        
        CREATE TABLE files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bin_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            FOREIGN KEY (bin_id) REFERENCES bins (id) ON DELETE CASCADE
        );
        ''')
    init_db()

def clean_expired_files():
    """Remove files older than 3 days"""
    now = datetime.datetime.now()
    cutoff_date = now - datetime.timedelta(days=3)
    
    # Get expired bins
    expired_bins = query_db(
        "SELECT id FROM bins WHERE upload_date < ?", 
        (cutoff_date.isoformat(),)
    )
    
    for bin in expired_bins:
        bin_id = bin['id']
        bin_path = os.path.join(app.config['UPLOAD_FOLDER'], bin_id)
        if os.path.exists(bin_path):
            shutil.rmtree(bin_path)
        
        # Delete bin from database
        modify_db("DELETE FROM bins WHERE id = ?", (bin_id,))

@app.route('/')
def index():
    clean_expired_files()
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        flash('No file part')
        return redirect(request.url)
    
    files = request.files.getlist('file')
    
    if not files or files[0].filename == '':
        flash('No selected file')
        return redirect(request.url)
    
    # Generate a unique ID for this upload
    file_id = str(uuid.uuid4())
    os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], file_id))
    
    # Calculate total size of all files
    total_size = 0
    uploaded_files = []
    
    for file in files:
        if file:
            file_size = 0
            file.seek(0, os.SEEK_END)
            file_size = file.tell()
            file.seek(0)
            total_size += file_size
            
            # Check if adding this file would exceed the 5MB limit
            if total_size > 5 * 1024 * 1024:
                flash('Total upload size exceeds 5MB limit')
                # Clean up the partial upload
                shutil.rmtree(os.path.join(app.config['UPLOAD_FOLDER'], file_id))
                return redirect(request.url)
            
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id, filename)
            file.save(file_path)
            uploaded_files.append((filename, file_size))
    
    # Store in database
    modify_db(
        "INSERT INTO bins (id, upload_date, total_size) VALUES (?, ?, ?)",
        (file_id, datetime.datetime.now().isoformat(), total_size)
    )
    
    # Store each file
    for filename, file_size in uploaded_files:
        modify_db(
            "INSERT INTO files (bin_id, filename, file_size) VALUES (?, ?, ?)",
            (file_id, filename, file_size)
        )
    
    return redirect(url_for('file_info', file_id=file_id))

@app.route('/files/<file_id>')
def file_info(file_id):
    bin_data = query_db("SELECT * FROM bins WHERE id = ?", (file_id,), one=True)
    
    if not bin_data:
        abort(404)
    
    files = query_db("SELECT filename, file_size FROM files WHERE bin_id = ?", (file_id,))
    filenames = [file['filename'] for file in files]
    
    upload_date = datetime.datetime.fromisoformat(bin_data['upload_date'])
    expiry_date = upload_date + datetime.timedelta(days=3)
    
    if not filenames:
        return render_template('file_info.html', 
                              file_id=file_id,
                              files=[],
                              is_empty=True,
                              upload_date=upload_date,
                              expiry_date=expiry_date)
    
    return render_template('file_info.html', 
                          file_id=file_id, 
                          files=filenames,
                          is_empty=False,
                          upload_date=upload_date,
                          expiry_date=expiry_date,
                          current_bin_size=bin_data['total_size'])

@app.route('/download/<file_id>/<filename>')
def download_file(file_id, filename):
    # Verify the file exists in database
    file = query_db(
        "SELECT 1 FROM files WHERE bin_id = ? AND filename = ?",
        (file_id, filename),
        one=True
    )
    
    if not file:
        abort(404)
    
    return send_from_directory(os.path.join(app.config['UPLOAD_FOLDER'], file_id), filename, as_attachment=True)

@app.route('/delete_file/<file_id>/<filename>', methods=['DELETE'])
def delete_file(file_id, filename):
    # Get file size before deleting
    file = query_db(
        "SELECT file_size FROM files WHERE bin_id = ? AND filename = ?",
        (file_id, filename),
        one=True
    )
    
    if not file:
        abort(404)
        
    file_size = file['file_size']
    
    # Remove the file from filesystem
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id, filename)
    if os.path.exists(file_path):
        os.remove(file_path)
    
    # Delete from database
    modify_db("DELETE FROM files WHERE bin_id = ? AND filename = ?", (file_id, filename))
    
    # Update bin total size
    modify_db(
        "UPDATE bins SET total_size = total_size - ? WHERE id = ?",
        (file_size, file_id)
    )
    
    # Check if bin is now empty
    remaining_files = query_db(
        "SELECT COUNT(*) as count FROM files WHERE bin_id = ?",
        (file_id,),
        one=True
    )
    
    if remaining_files['count'] == 0:
        # If all files are deleted, remove the bin directory
        bin_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id)
        if os.path.exists(bin_path):
            shutil.rmtree(bin_path)
        # Keep the bin record for viewing the "empty bin" message
        return jsonify({'success': True, 'redirectTo': url_for('file_info', file_id=file_id)})
    
    return jsonify({'success': True})

@app.route('/delete_bin/<file_id>', methods=['DELETE'])
def delete_bin(file_id):
    bin_exists = query_db("SELECT 1 FROM bins WHERE id = ?", (file_id,), one=True)
    
    if not bin_exists:
        abort(404)
    
    # Remove the directory and all its files
    bin_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id)
    if os.path.exists(bin_path):
        shutil.rmtree(bin_path)
    
    # Delete from database
    modify_db("DELETE FROM bins WHERE id = ?", (file_id,))
    
    return jsonify({'success': True})

@app.route('/api_docs')
def api_docs():
    return render_template('api_docs.html')

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/contact')
def contact():
    return render_template('contact.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/add_file/<file_id>', methods=['POST'])
def add_file(file_id):
    bin_data = query_db("SELECT total_size FROM bins WHERE id = ?", (file_id,), one=True)
    
    if not bin_data:
        abort(404)
    
    if 'file' not in request.files:
        flash('No file part')
        return redirect(url_for('file_info', file_id=file_id))
    
    files = request.files.getlist('file')
    
    if not files or files[0].filename == '':
        flash('No selected file')
        return redirect(url_for('file_info', file_id=file_id))
    
    # Get current bin size
    current_size = bin_data['total_size']
    
    # Calculate total size of all new files
    new_files_size = 0
    uploaded_files = []
    
    for file in files:
        if file and file.filename:
            file_size = 0
            file.seek(0, os.SEEK_END)
            file_size = file.tell()
            file.seek(0)
            new_files_size += file_size
            
            filename = secure_filename(file.filename)
            uploaded_files.append((filename, file_size))
    
    # Check if adding these files would exceed the 5MB limit
    if current_size + new_files_size > 5 * 1024 * 1024:
        flash('Adding these files would exceed the 5MB bin limit')
        return redirect(url_for('file_info', file_id=file_id))
    
    # Ensure directory exists
    bin_dir = os.path.join(app.config['UPLOAD_FOLDER'], file_id)
    if not os.path.exists(bin_dir):
        os.makedirs(bin_dir)
    
    # Save all files
    for filename, file_size in uploaded_files:
        file = next((f for f in files if secure_filename(f.filename) == filename), None)
        if file:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id, filename)
            file.save(file_path)
            
            # Add to database
            modify_db(
                "INSERT INTO files (bin_id, filename, file_size) VALUES (?, ?, ?)",
                (file_id, filename, file_size)
            )
    
    # Update total bin size
    modify_db(
        "UPDATE bins SET total_size = total_size + ? WHERE id = ?",
        (new_files_size, file_id)
    )
    
    return redirect(url_for('file_info', file_id=file_id))

# API Routes
@app.route('/api/v1/bins', methods=['POST'])
def api_create_bin():
    # Generate a unique ID for this upload
    file_id = str(uuid.uuid4())
    os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], file_id))
    
    # Initialize database entry
    modify_db(
        "INSERT INTO bins (id, upload_date, total_size) VALUES (?, ?, ?)",
        (file_id, datetime.datetime.now().isoformat(), 0)
    )
    
    # Check if files were included in the request
    if 'file' in request.files:
        files = request.files.getlist('file')
        uploaded_files = []
        total_size = 0
        
        for file in files:
            if file and file.filename:
                # Calculate file size
                file_size = 0
                file.seek(0, os.SEEK_END)
                file_size = file.tell()
                file.seek(0)
                
                # Check if adding this file would exceed the 5MB limit
                if total_size + file_size > 5 * 1024 * 1024:
                    return jsonify({'error': 'Total upload size exceeds 5MB limit'}), 400
                
                total_size += file_size
                filename = secure_filename(file.filename)
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id, filename)
                file.save(file_path)
                uploaded_files.append((filename, file_size))
        
        # Update bin size
        modify_db(
            "UPDATE bins SET total_size = ? WHERE id = ?",
            (total_size, file_id)
        )
        
        # Add files to database
        for filename, file_size in uploaded_files:
            modify_db(
                "INSERT INTO files (bin_id, filename, file_size) VALUES (?, ?, ?)",
                (file_id, filename, file_size)
            )
    
    return jsonify({
        'success': True,
        'file_id': file_id,
        'message': 'Bin created successfully'
    })

@app.route('/api/v1/bins/<file_id>', methods=['POST'])
def api_add_file(file_id):
    bin_data = query_db("SELECT total_size FROM bins WHERE id = ?", (file_id,), one=True)
    
    if not bin_data:
        return jsonify({'error': 'Bin not found'}), 404
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    
    file = request.files['file']
    
    if not file or file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    # Check file size
    file_size = 0
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    
    # Get current bin size
    current_size = bin_data['total_size']
    
    # Check if adding this file would exceed the 5MB limit
    if current_size + file_size > 5 * 1024 * 1024:
        return jsonify({'error': 'Adding this file would exceed the 5MB bin limit'}), 400
    
    # Ensure directory exists
    bin_dir = os.path.join(app.config['UPLOAD_FOLDER'], file_id)
    if not os.path.exists(bin_dir):
        os.makedirs(bin_dir)
    
    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id, filename)
    file.save(file_path)
    
    # Update database
    modify_db(
        "INSERT INTO files (bin_id, filename, file_size) VALUES (?, ?, ?)",
        (file_id, filename, file_size)
    )
    
    modify_db(
        "UPDATE bins SET total_size = total_size + ? WHERE id = ?",
        (file_size, file_id)
    )
    
    return jsonify({
        'success': True,
        'file_id': file_id,
        'filename': filename,
        'message': 'File added successfully'
    })

@app.route('/api/v1/bins/<file_id>', methods=['GET'])
def api_get_file_info(file_id):
    bin_data = query_db("SELECT * FROM bins WHERE id = ?", (file_id,), one=True)
    
    if not bin_data:
        return jsonify({'error': 'Bin not found'}), 404
    
    files = query_db("SELECT filename FROM files WHERE bin_id = ?", (file_id,))
    filenames = [file['filename'] for file in files]
    
    upload_date = datetime.datetime.fromisoformat(bin_data['upload_date'])
    expiry_date = upload_date + datetime.timedelta(days=3)
    
    return jsonify({
        'file_id': file_id,
        'filenames': filenames,
        'upload_date': upload_date.isoformat(),
        'expiry_date': expiry_date.isoformat(),
        'is_empty': len(filenames) == 0
    })

@app.route('/api/v1/bins/<file_id>', methods=['DELETE'])
def api_delete_bin(file_id):
    bin_exists = query_db("SELECT 1 FROM bins WHERE id = ?", (file_id,), one=True)
    
    if not bin_exists:
        return jsonify({'error': 'Bin not found'}), 404
    
    # Remove the directory and all its files
    bin_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id)
    if os.path.exists(bin_path):
        shutil.rmtree(bin_path)
    
    # Delete from database
    modify_db("DELETE FROM bins WHERE id = ?", (file_id,))
    
    return jsonify({'success': True, 'message': 'Bin deleted successfully'})

@app.route('/api/v1/bins/<file_id>/files/<filename>', methods=['DELETE'])
def api_delete_file(file_id, filename):
    # Get the file information
    file = query_db(
        "SELECT file_size FROM files WHERE bin_id = ? AND filename = ?",
        (file_id, filename),
        one=True
    )
    
    if not file:
        return jsonify({'error': 'File not found'}), 404
    
    # Remove the file
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id, filename)
    if os.path.exists(file_path):
        os.remove(file_path)
    
    # Update the database
    file_size = file['file_size']
    modify_db("DELETE FROM files WHERE bin_id = ? AND filename = ?", (file_id, filename))
    modify_db(
        "UPDATE bins SET total_size = total_size - ? WHERE id = ?",
        (file_size, file_id)
    )
    
    # Check if bin is now empty
    remaining_files = query_db(
        "SELECT COUNT(*) as count FROM files WHERE bin_id = ?",
        (file_id,),
        one=True
    )
    
    return jsonify({
        'success': True, 
        'message': 'File deleted successfully',
        'is_empty': remaining_files['count'] == 0
    })

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

if __name__ == '__main__':
    app.run(debug=True)