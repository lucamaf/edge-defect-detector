from flask import Flask, render_template, Response, request, jsonify
import cv2
from ultralytics import YOLO
import paho.mqtt.client as mqtt
import threading
import time
import os
import numpy as np

app = Flask(__name__)

# --- Configuration from Environment Variables with Defaults ---
MQTT_BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_TOPIC_CONTROL = os.environ.get("MQTT_TOPIC_CONTROL", "defect_detection/control")
MQTT_TOPIC_STATUS = os.environ.get("MQTT_TOPIC_STATUS", "defect_detection/status")
MODEL_PATH = os.environ.get("MODEL_PATH", "models/best.pt")
FLASK_WEB_PORT = int(os.environ.get("FLASK_WEB_PORT", 5000))
CAM_INDEX = os.environ.get("CAM_INDEX", "/dev/video1")
# Recording Configuration
RECORDING_PATH = os.environ.get("RECORDING_PATH", "recordings")
# FourCC is a 4-byte code used to specify the video codec.
# XVID is a good default for .avi. For .mp4, use 'mp4v'.
VIDEO_FORMAT_FOURCC = os.environ.get("VIDEO_FORMAT", "mp4v")  # Default to MP4V for .mp4 files
VIDEO_FPS = 20.0 # Frames per second for the output video

# --- Global Variables ---
video_capture = None
analysis_active = False
detected_defects = 0
# for newer versions of paho-mqtt, use CallbackAPIVersion
mqtt_client = mqtt.Client(client_id="defect_detection_client")
camera_lock = threading.Lock()  # To safely handle camera object access

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
            print(f"Successfully loaded YOLO model from {MODEL_PATH}")
        except Exception as e:
            print(f"Error loading YOLO model: {e}")
            # The app will continue to run, but analysis will not work.
    return model

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
    global analysis_active, detected_defects, video_writer, is_recording
    
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
                        print(status_message)
                        mqtt_client.publish(MQTT_TOPIC_STATUS, status_message)

                    except Exception as e:
                        print(f"Error starting video writer: {e}")
                        is_recording = False # Stop recording attempt if it fails
                
                # If writer is active, write the frame
                if video_writer is not None:
                    video_writer.write(frame)

            # If recording has been stopped, release the writer
            elif not is_recording and video_writer is not None:
                video_writer.release()
                video_writer = None
                status_message = "Recording stopped."
                print(status_message)
                mqtt_client.publish(MQTT_TOPIC_STATUS, status_message)
        # If analysis is active, perform detection
        if analysis_active and get_model():
            yolo_model = get_model()
            results = yolo_model(frame, stream=True, verbose=False)
            current_defects = 0
            for r in results:
                boxes = r.boxes
                for box in boxes:
                    x1, y1, x2, y2 = box.xyxy[0]
                    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
                    label = f"{yolo_model.names[int(box.cls)]} {box.conf.item():.2f}"
                    cv2.putText(frame, label, (x1, y1 - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
                    current_defects += 1
            detected_defects = current_defects

        # encode and yield the frame for streaming 
        ret, buffer = cv2.imencode('.jpg', frame)
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')


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
        return jsonify({'error': 'No file part'})
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'})
    
    yolo_model = get_model()
    if file and yolo_model:
        if not os.path.exists('uploads'):
            os.makedirs('uploads')
        filename = file.filename
        filepath = os.path.join('uploads', filename)
        file.save(filepath)

        if filename.lower().endswith(('.png', '.jpg', '.jpeg')):
            results = yolo_model(filepath)
            
            if not os.path.exists('static'):
                os.makedirs('static')
            
            annotated_image = results[0].plot()
            annotated_image_path = os.path.join('static', 'annotated_image.jpg')
            cv2.imwrite(annotated_image_path, annotated_image)
            
            detected_defects = len(results[0].boxes)
            # Add a timestamp to the image url to force browser refresh
            annotated_image_url = f'/static/annotated_image.jpg?t={time.time()}'
            return jsonify({'annotated_image': annotated_image_url, 'defect_count': detected_defects})
        elif filename.lower().endswith(('.mp4', '.avi', '.mov')):
            return jsonify({'message': 'Video processing not yet implemented.'})

    return jsonify({'error': 'Analysis failed or model not loaded.'})


@app.route('/get_defect_count')
def get_defect_count():
    global detected_defects
    return jsonify({'defect_count': detected_defects})

def on_connect(client, userdata, flags, rc, properties):
    if rc == 0:
        print("Connected to MQTT Broker!")
        client.subscribe(MQTT_TOPIC_CONTROL)
        client.publish(MQTT_TOPIC_STATUS, "Detector online")
    else:
        print(f"Failed to connect, return code {rc}\n")

def on_message(client, userdata, msg):
    global analysis_active, is_recording
    payload = msg.payload.decode()
    print(f"Received message on topic {msg.topic}: {payload}")
    if payload == "start":
        analysis_active = True
        client.publish(MQTT_TOPIC_STATUS, "Analysis started")
    elif payload == "stop":
        analysis_active = False
        client.publish(MQTT_TOPIC_STATUS, "Analysis stopped")
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
    except Exception as e:
        print(f"Could not connect to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}. MQTT control will be disabled. Error: {e}")


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