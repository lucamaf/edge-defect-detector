import logging

from flask import Flask, render_template, Response, request, jsonify
import cv2
from ultralytics import YOLO
import paho.mqtt.client as mqtt
import threading
import time
import os
import subprocess
import numpy as np
import uuid
import json
from collections import deque
from datetime import datetime

app = Flask(__name__)

# Suppress Werkzeug's per-request access log (INFO) while still surfacing its own
# warnings/errors, so REST endpoint calls stop showing up but real problems still do.
logging.getLogger('werkzeug').setLevel(logging.WARNING)


# --- Configuration from Environment Variables with Defaults ---
MQTT_BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_TOPIC_CONTROL = os.environ.get("MQTT_TOPIC_CONTROL", "defect_detection/control")
MQTT_TOPIC_STATUS = os.environ.get("MQTT_TOPIC_STATUS", "defect_detection/status")
MQTT_TOPIC_RESULTS = os.environ.get("MQTT_TOPIC_RESULTS", "defect_detection/results")
MODEL_PATH = os.environ.get("MODEL_PATH", "models/model.pt")
FLASK_WEB_PORT = int(os.environ.get("FLASK_WEB_PORT", 5000))
CAM_INDEX = os.environ.get("CAM_INDEX", "/dev/video0")
# Recording Configuration
RECORDING_PATH = os.environ.get("RECORDING_PATH", "recordings")
# FourCC is a 4-byte code used to specify the video codec.
# XVID is a good default for .avi. For .mp4, use 'mp4v'.
VIDEO_FORMAT_FOURCC = os.environ.get("VIDEO_FORMAT", "mp4v")  # Default to MP4V for .mp4 files
VIDEO_FPS = 20.0 # Frames per second for the output video

# --- Global Variables ---
video_capture = None
analysis_active = False
# Set to request a single-frame ("discrete") analysis; cleared automatically once
# that frame has been processed. Mutually exclusive with analysis_active.
discrete_requested = False
# Progressive counter, incremented once per discrete analysis result.
piece_counter = 0
detected_defects = 0
frames_analyzed = 0
# Timestamps of the last N analyzed frames, used to compute a rolling FPS.
analysis_frame_times = deque(maxlen=30)
# for newer versions of paho-mqtt, use CallbackAPIVersion
# clean_session=False so the broker queues any messages for our subscription while we're
# briefly disconnected, instead of dropping them -- required alongside qos=2 to actually
# avoid missed messages, not just duplicate/out-of-order ones.
mqtt_client = mqtt.Client(client_id="defect_detection_client", clean_session=False)
camera_lock = threading.Lock()  # To safely handle camera object access
upload_jobs = {} # Dictionary to store status of background jobs

# --- NEW: Recording State Management ---
is_recording = False
video_writer = None
recording_lock = threading.Lock()

# --- YOLO Model Loading ---
# Model is loaded only when first needed to avoid crashing if the path is invalid at startup
model = None
def get_model():
    global model
    if model is None:
        try:
            model = YOLO(MODEL_PATH)
            app.logger.warning(f"Successfully loaded YOLO model from {MODEL_PATH}")
        except Exception:
            app.logger.exception(f"Error loading YOLO model from {MODEL_PATH}")
            # The app will continue to run, but analysis will not work.
    return model

def get_analysis_fps():
    """Rolling FPS over the last few analyzed frames."""
    if len(analysis_frame_times) < 2:
        return 0.0
    elapsed = analysis_frame_times[-1] - analysis_frame_times[0]
    if elapsed <= 0:
        return 0.0
    return (len(analysis_frame_times) - 1) / elapsed

# get and release camera when was using default usb camera as input
#def get_camera():
#    global camera
#    if camera is None:
#        # The camera index can also be an environment variable if needed
#        camera = cv2.VideoCapture(CAM_INDEX)
#    return camera

#def release_camera():
#    global camera
#    if camera:
#        camera.release()
#        camera = None

def run_detection_on_frame(frame, yolo_model):
    """Runs YOLO detection on a frame, draws boxes in-place, and returns (defect_count, max_confidence)."""
    results = yolo_model(frame, verbose=False)
    boxes = results[0].boxes
    max_confidence = 0.0
    for box in boxes:
        x1, y1, x2, y2 = box.xyxy[0]
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        confidence = box.conf.item()
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
        label = f"{yolo_model.names[int(box.cls)]} {confidence:.2f}"
        cv2.putText(frame, label, (x1, y1 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
        max_confidence = max(max_confidence, confidence)
    return len(boxes), max_confidence

def publish_discrete_result(defective, confidence, piece):
    payload = json.dumps({
        'defective': defective,
        'confidence': round(confidence, 3),
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'piece': piece,
    })
    mqtt_client.publish(MQTT_TOPIC_RESULTS, payload, qos=2)

def create_message_frame(message):
    """Creates a black frame with a text message."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    # Add a border
    frame = cv2.rectangle(frame, (1, 1), (639, 479), (80, 80, 80), 1)
    # Put the message
    cv2.putText(frame, message, (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2, cv2.LINE_AA)
    ret, buffer = cv2.imencode('.jpg', frame)
    return buffer.tobytes()

# generate frames from the video capture 
# includes recording logic
def generate_frames():
    global analysis_active, detected_defects, video_writer, is_recording, frames_analyzed
    global discrete_requested, piece_counter
    
    while True:
        with camera_lock:
            if video_capture is None:                                # If recording was left on, stop it
                with recording_lock:
                    if video_writer is not None:
                        video_writer.release()
                        video_writer = None
                    is_recording = False

                frame_bytes = create_message_frame("No video source selected")
                time.sleep(1) # Don't spam the client if no source
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                continue

            success, frame = video_capture.read()

        if not success:
            frame_bytes = create_message_frame("Video stream disconnected or invalid")
            time.sleep(1) # Wait a moment before retrying
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            continue
        
        # video recording logic
        with recording_lock:
            if is_recording:
                # If this is the first frame of a new recording, initialize the VideoWriter
                if video_writer is None:
                    try:
                        h, w, _ = frame.shape
                        fourcc = cv2.VideoWriter_fourcc(*VIDEO_FORMAT_FOURCC)
                        file_ext = 'avi' if VIDEO_FORMAT_FOURCC == 'XVID' else 'mp4'
                        filename = f"rec_{time.strftime('%Y%m%d_%H%M%S')}.{file_ext}"
                        filepath = os.path.join(RECORDING_PATH, filename)
                        
                        video_writer = cv2.VideoWriter(filepath, fourcc, VIDEO_FPS, (w, h))
                        
                        status_message = f"Recording started: {filepath}"
                        app.logger.warning(status_message)
                        mqtt_client.publish(MQTT_TOPIC_STATUS, status_message, qos=2)

                    except Exception:
                        app.logger.exception("Error starting video writer")
                        is_recording = False # Stop recording attempt if it fails
                
                # If writer is active, write the frame
                if video_writer is not None:
                    video_writer.write(frame)

            # If recording has been stopped, release the writer
            elif not is_recording and video_writer is not None:
                video_writer.release()
                video_writer = None
                status_message = "Recording stopped."
                app.logger.warning(status_message)
                mqtt_client.publish(MQTT_TOPIC_STATUS, status_message, qos=2)
        # If continuous analysis is active, perform detection on every frame
        if analysis_active and get_model():
            yolo_model = get_model()
            current_defects, _ = run_detection_on_frame(frame, yolo_model)
            detected_defects = current_defects
            frames_analyzed += 1
            analysis_frame_times.append(time.time())

        # A discrete request analyzes exactly one frame, then clears itself
        elif discrete_requested and get_model():
            yolo_model = get_model()
            current_defects, max_confidence = run_detection_on_frame(frame, yolo_model)
            detected_defects = current_defects
            frames_analyzed += 1
            analysis_frame_times.append(time.time())

            piece_counter += 1
            publish_discrete_result(current_defects > 0, max_confidence, piece_counter)
            discrete_requested = False
            mqtt_client.publish(MQTT_TOPIC_STATUS, "Discrete analysis complete", qos=2)

        # encode and yield the frame for streaming 
        ret, buffer = cv2.imencode('.jpg', frame)
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

def process_video_job(job_id, input_path):
    """
    Processes a video in a background thread.
    - input_path: Path to the originally uploaded video.
    """
    try:
        yolo_model = get_model()
        if not yolo_model:
            raise Exception("Model could not be loaded.")
            
        cap = cv2.VideoCapture(input_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        # Get video properties for the output writer
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        
        # Define output paths in the static folder to be web-accessible.
        # OpenCV writes a raw mp4v file first, which browsers generally can't
        # play back directly, then it gets transcoded to H.264 below.
        raw_filename = f"processed_{job_id}_raw.mp4"
        raw_path = os.path.join('static', raw_filename)
        output_filename = f"processed_{job_id}.mp4"
        output_path = os.path.join('static', output_filename)

        # Create the video writer
        fourcc = cv2.VideoWriter_fourcc(*'mp4v') # Use 'mp4v' for .mp4 files
        writer = cv2.VideoWriter(raw_path, fourcc, fps, (w, h))

        upload_jobs[job_id]['status'] = 'processing'
        
        for frame_count in range(total_frames):
            success, frame = cap.read()
            if not success:
                break

            # Run YOLO detection
            results = yolo_model(frame, verbose=False)
            annotated_frame = results[0].plot() # .plot() returns a NumPy array with boxes drawn
            writer.write(annotated_frame)
            
            # Update progress
            progress = int(((frame_count + 1) / total_frames) * 100)
            upload_jobs[job_id]['progress'] = progress

        # Finalize the raw capture
        cap.release()
        writer.release()

        # Transcode to H.264/yuv420p so browsers can actually play the result back
        subprocess.run(
            ['ffmpeg', '-y', '-i', raw_path, '-vcodec', 'libx264', '-pix_fmt', 'yuv420p', output_path],
            check=True, capture_output=True, text=True,
        )
        os.remove(raw_path)

        upload_jobs[job_id]['status'] = 'complete'
        upload_jobs[job_id]['result_path'] = f'/static/{output_filename}'
        app.logger.warning(f"Job {job_id} completed. Output at {output_path}")

    except subprocess.CalledProcessError as e:
        app.logger.error(f"ffmpeg transcode failed for job {job_id}: {e.stderr}")
        upload_jobs[job_id]['status'] = 'failed'
        upload_jobs[job_id]['error'] = 'Video transcoding failed. See server logs.'
    except Exception as e:
        app.logger.exception(f"Error processing job {job_id}")
        upload_jobs[job_id]['status'] = 'failed'
        upload_jobs[job_id]['error'] = str(e)
    finally:
        # Clean up the original uploaded file
        if os.path.exists(input_path):
            os.remove(input_path)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/select_source', methods=['POST'])
def select_source():
    global video_capture
    data = request.get_json()
    source_type = data.get('source')
    url = data.get('url')
    
    with camera_lock:
        # Release the current capture if it exists
        if video_capture is not None:
            video_capture.release()
            video_capture = None

        if source_type == 'usb':
            # Use /dev/video0 for the default USB camera. This could also be made configurable.
            video_capture = cv2.VideoCapture(CAM_INDEX)
            message = "Switched to Local USB Camera"
        elif source_type == 'web' and url:
            video_capture = cv2.VideoCapture(url)
            message = f"Attempting to connect to stream: {url}"
        else:
            return jsonify({'status': 'error', 'message': 'Invalid source type or missing URL.'}), 400

    return jsonify({'status': 'success', 'message': message})

@app.route('/upload_media', methods=['POST'])
def upload_media():
    global detected_defects
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    yolo_model = get_model()
    if not yolo_model:
        app.logger.error("Upload failed: YOLO model is not loaded (MODEL_PATH=%s)", MODEL_PATH)
        return jsonify({'error': 'Model not loaded on server'}), 500

    os.makedirs('uploads', exist_ok=True)
    filename = file.filename
    filepath = os.path.join('uploads', filename)
    file.save(filepath)

    # --- IMAGE PROCESSING (remains the same) ---
    if filename.lower().endswith(('.png', '.jpg', '.jpeg')):
        results = yolo_model(filepath)
        os.makedirs('static', exist_ok=True)

        annotated_image = results[0].plot()
        annotated_image_path = os.path.join('static', 'annotated_image.jpg')
        cv2.imwrite(annotated_image_path, annotated_image)

        detected_defects = len(results[0].boxes)
        annotated_image_url = f'/static/annotated_image.jpg?t={time.time()}'
        os.remove(filepath) # Clean up original upload
        return jsonify({'type': 'image', 'annotated_image': annotated_image_url, 'defect_count': detected_defects})

    # --- VIDEO PROCESSING (NEW LOGIC) ---
    if filename.lower().endswith(('.mp4', '.avi', '.mov', '.webm')):
        job_id = uuid.uuid4().hex

        # Initialize job status
        upload_jobs[job_id] = {'status': 'queued', 'progress': 0}

        # Start background thread
        thread = threading.Thread(target=process_video_job, args=(job_id, filepath))
        thread.daemon = True
        thread.start()

        # Immediately return the job ID
        return jsonify({'type': 'video', 'status': 'processing', 'job_id': job_id})

    app.logger.error("Upload failed: unsupported file type for filename=%r", filename)
    os.remove(filepath)
    return jsonify({'error': f'Unsupported file type: {filename}'}), 400

@app.route('/upload_status/<job_id>')
def upload_status(job_id):
    """Endpoint for the frontend to poll for job status."""
    job = upload_jobs.get(job_id)
    if job:
        return jsonify(job)
    else:
        return jsonify({'status': 'not_found'}), 404

@app.route('/get_defect_count')
def get_defect_count():
    return jsonify({
        'defect_count': detected_defects,
        'analysis_active': analysis_active,
        'discrete_active': discrete_requested,
        'frames_analyzed': frames_analyzed,
        'fps': round(get_analysis_fps(), 1),
    })

@app.route('/toggle_analysis', methods=['POST'])
def toggle_analysis():
    global analysis_active, discrete_requested
    data = request.get_json(silent=True) or {}
    active = bool(data.get('active'))
    command = "start" if active else "stop"

    if mqtt_client.is_connected():
        # Publish the same control message an external MQTT client would send, so
        # on_message() stays the single place that flips analysis_active.
        mqtt_client.publish(MQTT_TOPIC_CONTROL, command, qos=2)
    else:
        # Broker unreachable: fall back to toggling locally so the switch still works.
        analysis_active = active
        if active:
            discrete_requested = False  # mutually exclusive with continuous mode
        app.logger.warning("MQTT broker not connected; toggled analysis locally without publishing")

    return jsonify({'status': 'ok', 'active': active})

@app.route('/toggle_discrete', methods=['POST'])
def toggle_discrete():
    global analysis_active, discrete_requested
    data = request.get_json(silent=True) or {}
    active = bool(data.get('active'))
    command = "discrete-on" if active else "discrete-off"

    if mqtt_client.is_connected():
        # Publish the same control message an external MQTT client would send, so
        # on_message() stays the single place that flips discrete_requested.
        mqtt_client.publish(MQTT_TOPIC_CONTROL, command, qos=2)
    else:
        # Broker unreachable: fall back to toggling locally so the switch still works.
        if active:
            analysis_active = False  # mutually exclusive with continuous mode
        discrete_requested = active
        app.logger.warning("MQTT broker not connected; toggled discrete analysis locally without publishing")

    return jsonify({'status': 'ok', 'active': active})

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        app.logger.warning("Connected to MQTT Broker!")
        client.subscribe(MQTT_TOPIC_CONTROL, qos=2)
        client.publish(MQTT_TOPIC_STATUS, "Detector online", qos=2)
    else:
        app.logger.error(f"Failed to connect to MQTT broker, return code {rc}")

def on_message(client, userdata, msg):
    global analysis_active, is_recording, discrete_requested
    payload = msg.payload.decode()
    app.logger.warning(f"Received message on topic {msg.topic}: {payload}")
    if payload == "start":
        analysis_active = True
        discrete_requested = False  # mutually exclusive with discrete mode
        client.publish(MQTT_TOPIC_STATUS, "Analysis started", qos=2)
    elif payload == "stop":
        analysis_active = False
        client.publish(MQTT_TOPIC_STATUS, "Analysis stopped", qos=2)
    elif payload == "discrete-on":
        analysis_active = False  # mutually exclusive with continuous mode
        discrete_requested = True
        client.publish(MQTT_TOPIC_STATUS, "Discrete analysis requested", qos=2)
    elif payload == "discrete-off":
        discrete_requested = False
        client.publish(MQTT_TOPIC_STATUS, "Discrete analysis cancelled", qos=2)
    # recording MQTT Commands
    elif payload == "start_recording":
        with recording_lock:
            if not is_recording:
                is_recording = True
                # The video_writer object itself will be created in the generate_frames loop
                # once a frame is available.
    elif payload == "stop_recording":
        with recording_lock:
            if is_recording:
                is_recording = False
                # The video_writer will be released in the generate_frames loop.

def mqtt_thread_func():
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_forever()
    except Exception:
        app.logger.exception(f"Could not connect to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}. MQTT control will be disabled.")


if __name__ == '__main__':
    # Create required directories if they don't exist
    for d in ['uploads', 'static', RECORDING_PATH]:
        if not os.path.exists(d):
            os.makedirs(d)

    mqtt_thread = threading.Thread(target=mqtt_thread_func)
    mqtt_thread.daemon = True
    mqtt_thread.start()

    # The host must be '0.0.0.0' to be reachable from outside the container
    app.run(host='0.0.0.0', port=FLASK_WEB_PORT, debug=False, use_reloader=False)
