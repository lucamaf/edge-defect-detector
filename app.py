from flask import Flask, render_template, Response, request, jsonify
import cv2
from ultralytics import YOLO
import paho.mqtt.client as mqtt
import threading
import time
import os

app = Flask(__name__)

# --- Configuration from Environment Variables with Defaults ---
MQTT_BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_TOPIC_CONTROL = os.environ.get("MQTT_TOPIC_CONTROL", "defect_detection/control")
MQTT_TOPIC_STATUS = os.environ.get("MQTT_TOPIC_STATUS", "defect_detection/status")
MODEL_PATH = os.environ.get("MODEL_PATH", "models/best.pt")
FLASK_WEB_PORT = int(os.environ.get("FLASK_WEB_PORT", 5000))
CAM_INDEX = os.environ.get("CAM_INDEX", "/dev/video1")


# --- Global Variables ---
camera = None
analysis_active = False
detected_defects = 0
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

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

# --- The rest of the functions (get_camera, release_camera, generate_frames, etc.) remain the same ---

def get_camera():
    global camera
    if camera is None:
        # The camera index can also be an environment variable if needed
        camera = cv2.VideoCapture(CAM_INDEX)
    return camera

def release_camera():
    global camera
    if camera:
        camera.release()
        camera = None

def generate_frames():
    global analysis_active, detected_defects
    cam = get_camera()
    while True:
        success, frame = cam.read()
        if not success:
            break
        else:
            if analysis_active and get_model():
                yolo_model = get_model()
                results = yolo_model(frame, stream=True)
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

            ret, buffer = cv2.imencode('.jpg', frame)
            frame = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

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

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to MQTT Broker!")
        client.subscribe(MQTT_TOPIC_CONTROL)
        client.publish(MQTT_TOPIC_STATUS, "Detector online")
    else:
        print(f"Failed to connect, return code {rc}\n")

def on_message(client, userdata, msg):
    global analysis_active
    payload = msg.payload.decode()
    print(f"Received message on topic {msg.topic}: {payload}")
    if payload == "start":
        analysis_active = True
        client.publish(MQTT_TOPIC_STATUS, "Analysis started")
    elif payload == "stop":
        analysis_active = False
        client.publish(MQTT_TOPIC_STATUS, "Analysis stopped")

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
    for d in ['uploads', 'static']:
        if not os.path.exists(d):
            os.makedirs(d)

    mqtt_thread = threading.Thread(target=mqtt_thread_func)
    mqtt_thread.daemon = True
    mqtt_thread.start()

    # The host must be '0.0.0.0' to be reachable from outside the container
    app.run(host='0.0.0.0', port=FLASK_WEB_PORT, debug=False, use_reloader=False)