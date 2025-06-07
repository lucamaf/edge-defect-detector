# Stage 1: Use an official Python runtime as a parent image
#FROM python:3.11-slim
FROM ubi9/python-311

# Install system dependencies required by OpenCV
#RUN apt-get update && apt-get install -y --no-install-recommends \
#    libgl1-mesa-glx \
#    libglib2.0-0 \
#    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Set environment variables with default values.
# These can be overridden at runtime.
ENV MQTT_BROKER="mqtt_broker_address"
ENV MQTT_PORT="1883"
ENV MQTT_TOPIC_CONTROL="defect_detection/control"
ENV MQTT_TOPIC_STATUS="defect_detection/status"
ENV MODEL_PATH="/app/models/best.pt"
ENV FLASK_WEB_PORT="5000"

# Copy the requirements file and install dependencies
# This leverages Docker's layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Expose the port the app runs on
EXPOSE 5000

# The command to run the application
CMD ["python", "app.py"]
