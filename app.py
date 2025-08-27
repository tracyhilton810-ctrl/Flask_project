import os
import logging
import re
import json
import subprocess
from urllib.parse import urlparse, parse_qs
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify
import threading
import time
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.DEBUG)

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-secret-key-change-in-production")

# Global dictionary to store download progress
download_progress = {}

def is_youtube_url(url):
    """Validate if the URL is a YouTube URL"""
    youtube_regex = re.compile(
        r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/'
        r'(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})'
    )
    return youtube_regex.match(url) is not None

def get_video_id(url):
    """Extract video ID from YouTube URL"""
    parsed_url = urlparse(url)
    if parsed_url.hostname == 'youtu.be':
        return parsed_url.path[1:]
    if parsed_url.hostname in ('www.youtube.com', 'youtube.com'):
        if parsed_url.path == '/watch':
            p = parse_qs(parsed_url.query)
            return p['v'][0]
        if parsed_url.path[:7] == '/embed/':
            return parsed_url.path.split('/')[2]
        if parsed_url.path[:3] == '/v/':
            return parsed_url.path.split('/')[2]
    return None

def get_video_info(url):
    """Get video information using yt-dlp"""
    try:
        cmd = [
            'yt-dlp',
            '--dump-json',
            '--no-download',
            url
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            logging.error(f"yt-dlp error: {result.stderr}")
            return None
            
        video_data = json.loads(result.stdout)
        
        # Get available formats
        formats = video_data.get('formats', [])
        
        # Filter video formats (mp4 with video and audio)
        video_formats = []
        for fmt in formats:
            if (fmt.get('ext') == 'mp4' and 
                fmt.get('vcodec') != 'none' and 
                fmt.get('acodec') != 'none' and
                fmt.get('height')):
                video_formats.append({
                    'resolution': f"{fmt.get('height')}p",
                    'filesize': fmt.get('filesize', 0),
                    'filesize_mb': round(fmt.get('filesize', 0) / (1024 * 1024), 1) if fmt.get('filesize') else 0,
                    'format_id': fmt.get('format_id')
                })
        
        # Remove duplicates and sort by resolution
        seen_resolutions = set()
        unique_formats = []
        for fmt in sorted(video_formats, key=lambda x: int(x['resolution'][:-1]), reverse=True):
            if fmt['resolution'] not in seen_resolutions:
                seen_resolutions.add(fmt['resolution'])
                unique_formats.append(fmt)
        
        # Get audio format
        audio_format = None
        for fmt in formats:
            if (fmt.get('ext') in ['m4a', 'mp3'] and 
                fmt.get('acodec') != 'none' and 
                fmt.get('vcodec') == 'none'):
                audio_format = {
                    'filesize': fmt.get('filesize', 0),
                    'filesize_mb': round(fmt.get('filesize', 0) / (1024 * 1024), 1) if fmt.get('filesize') else 0,
                    'format_id': fmt.get('format_id')
                }
                break
        
        return {
            'title': video_data.get('title', 'Unknown'),
            'duration': video_data.get('duration', 0),
            'uploader': video_data.get('uploader', 'Unknown'),
            'view_count': video_data.get('view_count', 0),
            'thumbnail': video_data.get('thumbnail', ''),
            'video_formats': unique_formats,
            'audio_format': audio_format
        }
        
    except subprocess.TimeoutExpired:
        logging.error("yt-dlp command timed out")
        return None
    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse yt-dlp output: {e}")
        return None
    except Exception as e:
        logging.error(f"Error getting video info: {e}")
        return None

def download_video_thread(youtube_url, quality, download_id):
    """Download video in a separate thread using yt-dlp"""
    try:
        # Initialize progress
        download_progress[download_id] = {
            'percentage': 0,
            'bytes_downloaded': 0,
            'total_size': 0,
            'status': 'starting',
            'title': 'Getting video info...'
        }
        
        # Create downloads directory if it doesn't exist
        os.makedirs('downloads', exist_ok=True)
        
        # Prepare yt-dlp command
        if quality == 'audio':
            # Download audio only
            cmd = [
                'yt-dlp',
                '-f', 'bestaudio[ext=m4a]/bestaudio/best',
                '--extract-audio',
                '--audio-format', 'mp3',
                '-o', 'downloads/%(title)s_audio.%(ext)s',
                youtube_url
            ]
        else:
            # Download video with specific resolution
            cmd = [
                'yt-dlp',
                '-f', f'best[height<={quality[:-1]}][ext=mp4]/best[ext=mp4]/best',
                '-o', f'downloads/%(title)s_{quality}.%(ext)s',
                youtube_url
            ]
        
        download_progress[download_id]['status'] = 'downloading'
        
        # Run yt-dlp with progress tracking
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                 text=True, universal_newlines=True)
        
        filename = None
        if process.stdout:
            for line in process.stdout:
                line = line.strip()
                logging.debug(f"yt-dlp output: {line}")
                
                # Extract filename from output
                if '[download] Destination:' in line:
                    filename = line.split('[download] Destination:')[1].strip()
                    # Get just the filename without path
                    filename = os.path.basename(filename)
                    
                # Parse progress information
                if '[download]' in line and '%' in line:
                    try:
                        # Extract percentage
                        if 'of' in line:
                            parts = line.split()
                            for i, part in enumerate(parts):
                                if '%' in part:
                                    percentage = float(part.replace('%', ''))
                                    download_progress[download_id]['percentage'] = round(percentage, 1)
                                    
                                    # Try to extract size info
                                    if i + 2 < len(parts) and 'of' in parts[i + 1]:
                                        try:
                                            downloaded = parts[i + 1].split('of')[0].strip()
                                            total = parts[i + 1].split('of')[1].strip()
                                            
                                            # Convert sizes to bytes (simplified)
                                            download_progress[download_id]['status'] = 'downloading'
                                        except:
                                            pass
                                    break
                    except:
                        pass
                
                # Get video title if available
                if 'title' not in download_progress[download_id] or download_progress[download_id]['title'] == 'Getting video info...':
                    if filename:
                        # Extract title from filename
                        title = filename.replace(f'_{quality}', '').replace('_audio', '')
                        title = title.split('.')[0] if '.' in title else title
                        download_progress[download_id]['title'] = title
        
        process.wait()
        
        if process.returncode == 0:
            download_progress[download_id]['status'] = 'completed'
            download_progress[download_id]['percentage'] = 100
            download_progress[download_id]['filename'] = filename if filename else 'download_completed'
            
            # Find the actual downloaded file
            download_dir = 'downloads'
            if filename:
                filepath = os.path.join(download_dir, filename)
                if os.path.exists(filepath):
                    download_progress[download_id]['filepath'] = filepath
                else:
                    # Search for similar files
                    for file in os.listdir(download_dir):
                        if quality in file or 'audio' in file:
                            download_progress[download_id]['filepath'] = os.path.join(download_dir, file)
                            download_progress[download_id]['filename'] = file
                            break
        else:
            download_progress[download_id]['status'] = 'error'
            download_progress[download_id]['error'] = 'Download failed'
            
    except Exception as e:
        logging.error(f"Download error: {str(e)}")
        download_progress[download_id] = {
            'status': 'error',
            'error': str(e)
        }

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/analyze', methods=['POST'])
def analyze_video():
    url = request.form.get('url', '').strip()
    
    if not url:
        flash('Please enter a YouTube URL', 'error')
        return redirect(url_for('index'))
    
    if not is_youtube_url(url):
        flash('Please enter a valid YouTube URL', 'error')
        return redirect(url_for('index'))
    
    try:
        video_info = get_video_info(url)
        
        if not video_info:
            flash('Unable to analyze video. Please check the URL and try again.', 'error')
            return redirect(url_for('index'))
        
        # Format the data for template
        video_data = {
            'title': video_info['title'],
            'length': video_info['duration'],
            'author': video_info['uploader'],
            'views': video_info['view_count'],
            'thumbnail_url': video_info['thumbnail'],
            'video_streams': video_info['video_formats'],
            'audio_size_mb': video_info['audio_format']['filesize_mb'] if video_info['audio_format'] else 0,
            'url': url
        }
        
        return render_template('index.html', video_info=video_data)
        
    except Exception as e:
        logging.error(f"Error analyzing video: {str(e)}")
        flash(f'Error analyzing video: {str(e)}', 'error')
        return redirect(url_for('index'))

@app.route('/download', methods=['POST'])
def start_download():
    url = request.form.get('url')
    quality = request.form.get('quality')
    
    if not url or not quality:
        flash('Missing URL or quality selection', 'error')
        return redirect(url_for('index'))
    
    # Generate unique download ID
    download_id = f"{get_video_id(url)}_{quality}_{int(time.time())}"
    
    # Start download in background thread
    thread = threading.Thread(target=download_video_thread, args=(url, quality, download_id))
    thread.daemon = True
    thread.start()
    
    return render_template('download.html', download_id=download_id)

@app.route('/progress/<download_id>')
def get_progress(download_id):
    progress = download_progress.get(download_id, {
        'status': 'not_found',
        'error': 'Download not found'
    })
    return jsonify(progress)

@app.route('/download-file/<download_id>')
def download_file(download_id):
    progress = download_progress.get(download_id)
    if not progress or progress.get('status') != 'completed':
        flash('File not ready for download', 'error')
        return redirect(url_for('index'))
    
    filepath = progress.get('filepath')
    if not filepath or not os.path.exists(filepath):
        flash('File not found', 'error')
        return redirect(url_for('index'))
    
    return send_file(filepath, as_attachment=True)

@app.errorhandler(404)
def not_found_error(error):
    return render_template('index.html'), 404

@app.errorhandler(500)
def internal_error(error):
    flash('An internal error occurred. Please try again.', 'error')
    return render_template('index.html'), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)