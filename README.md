# Python defect detection application

### How to Build and Run the Container

- Step 1: Place Your Model
  Make sure your trained model file (e.g., best.pt) is inside the models directory.

- Step 2: Build the Docker Image
  Open a terminal in the project root directory (/defect-detector-app/) and run:
 
```
podman build -t defect-detector .
```

- Step 3: Run the Container
  Now, run the container. The command below shows how to override the environment variables and map necessary resources.

    --device /dev/video1:/dev/video1: This maps your host's webcam into the container so OpenCV can access it.
    -v "$(pwd)/models":/app/models: This mounts your local models directory into the container. This is the best practice for handling large model files, as it keeps them out of the image itself.
    -p 8080:5000: This maps port 8080 on your host machine to port 5000 inside the container. You will access the UI at http://localhost:8080.

Make sure to start first the MQTT broker (either natively or containerized like shown)

```
podman run -d  --replace --privileged --name mosquitto -p 1883:1883 -v "$PWD/mosquitto/config:/mosquitto/config" -v "$PWD/mosquitto/data:/mosquitto/data" -v "$PWD/mosquitto/log:/mosquitto/log" --network kind --network shared eclipse-mosquitto
```

```
podman run -d --replace --privileged \
    --name my-detector \
    -p 5000:5000 \
    --device /dev/video1:/dev/video1 \
    -v "$(pwd)/models":/app/models \
    -e MQTT_BROKER="192.168.1.100" \
    -e MQTT_PORT="1883" \
    -e FLASK_WEB_PORT="5000" \
    -e MODEL_PATH="/app/models/yolov8n.pt" \
    --network shared
    defect-detector
```

(Replace 192.168.1.100 with your actual MQTT broker's IP address. If the broker is also a container on the same Podman network, you can use its container name.)

- Step 4: Access Your Application
  You can now open your web browser and navigate to http://localhost:8080 to see your application running.

### Controlling the Model with MQTT

You can control the real-time defect detection on the live video stream by publishing messages to the `defect_detection/control` MQTT topic.

    To start the analysis, publish the message: start
    To stop the analysis, publish the message: stop

You can use any MQTT client (e.g., MQTTX, mosquitto_pub) to send these commands. The application will also publish its status (Detector online, Analysis started, Analysis stopped) to the `defect_detection/status` topic.

### View live logs 

To view logs or stop the container:

```
podman logs -f my-detector
```

### Stop and remove the container
```
podman stop my-detector
podman rm my-detector
```