#!/usr/bin/python3

import io
import logging
import socketserver
from http import server
from threading import Condition, Thread, Lock
import time
import json
import cv2
import numpy as np
from picamera2 import Picamera2

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')

# Global frame info for frontend
latest_frame_info = {
    'frame_id': None,
    'server_timestamp': None
}

PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Alex Remote Camera Control Center</title>
    <style>
        body {
            background-color: #0e0e0e;
            color: #ffffff;
            font-family: 'Courier New', Courier, monospace;
            margin: 0;
            padding: 20px;
            display: flex;
            flex-direction: column;
            align-items: center;
        }
        h1 {
            font-size: 28px;
            margin-bottom: 10px;
        }
        #stream-container {
            width: 640px;
            height: 480px;
            display: flex;
            justify-content: center;
            align-items: center;
            border: 4px solid #666;
            background-color: #444;
        }
        #stream-container img {
            width: 640px;
            height: 480px;
        }
        button {
            background-color: #007bff;
            border: none;
            color: white;
            padding: 12px 24px;
            font-size: 16px;
            cursor: pointer;
            border-radius: 6px;
            margin-top: 20px;
            width: 640px;
            display: block;
        }
        button:disabled {
            background-color: #555;
            cursor: not-allowed;
        }
        #countdown {
            margin-top: 10px;
            font-size: 18px;
            color: #ffcc00;
        }
        #log-container {
            margin-top: 30px;
            width: 640px;
            max-width: 800px;
            height: 200px;
            background-color: black;
            color: #00ff00;
            padding: 10px;
            overflow-y: auto;
            border: 2px solid #444;
            font-size: 14px;
        }
    </style>
    <script>
        let streamInterval;
        let countdownInterval;
        let cooldown = false;

        function logMessage(msg) {
            const log = document.getElementById('log');
            const timestamp = new Date().toLocaleTimeString();
            log.innerText += `[${timestamp}] ${msg}\\n`;
            log.scrollTop = log.scrollHeight;
        }

        function updateBorder(color) {
            document.getElementById('stream-container').style.borderColor = color;
        }

        function startStream() {
            if (cooldown) {
                logMessage("Command rejected: cooling down...");
                return;
            }

            cooldown = true;
            document.getElementById('start-btn').disabled = true;

            fetch('/start_stream')
                .then(() => {
                    logMessage("Handshake success: stream acknowledged by rover.");
                    const container = document.getElementById('stream-container');
                    container.innerHTML = '';
                    const img = document.createElement('img');
                    img.id = "stream";
                    img.src = "stream.mjpg";
                    container.appendChild(img);

                    updateBorder("#00ff00");
                    let secondsLeft = 10;
                    document.getElementById('start-btn').innerText = "Streaming... (" + secondsLeft + ")";

                    countdownInterval = setInterval(() => {
                        secondsLeft--;
                        document.getElementById('start-btn').innerText = "Streaming... (" + secondsLeft + ")";
                        if (secondsLeft <= 0) {
                            stopStream();
                        }
                    }, 1000);

                    streamInterval = setInterval(() => {
                        const start = Date.now();
                        fetch('/frame_info')
                            .then(resp => resp.json())
                            .then(data => {
                                const end = Date.now();
                                const delay = end - start;

                                if (data.frame_id === null || data.server_timestamp === null) {
                                    document.getElementById('countdown').innerText = "Streaming Stopped...";
                                    updateBorder("#666");
                                    
                                } else {
                                    const info = `Frame: ${data.frame_id} | Delay: ${delay} ms`;
                                    document.getElementById('countdown').innerText = info;

                                    if (delay > 100) updateBorder("#ff0000");
                                    else if (delay > 50) updateBorder("#ffff00");
                                    else updateBorder("#00ff00");
                                }
                            })
                            .catch(() => {
                                document.getElementById('countdown').innerText = "Streaming Stopped...";
                                updateBorder("#666");
                            });
                    }, 500);

                    setTimeout(() => {
                        cooldown = false;
                        document.getElementById('start-btn').disabled = false;
                        document.getElementById('start-btn').innerText = "Activate Camera";
                        logMessage("Cooldown ended. Ready for next activation.");
                    }, 10000);
                });
        }

        function stopStream() {
            fetch('/stop_stream')
                .then(() => {
                    const container = document.getElementById('stream-container');
                    const img = document.getElementById('stream');
                    if (img) img.remove();
                    container.innerHTML = '<span>Video stream will appear here</span>';

                    updateBorder("#666");
                    document.getElementById('countdown').innerText = "Streaming Stopped...";
                    clearInterval(countdownInterval);
                    clearInterval(streamInterval);
                    delay(1000);
                    document.getElementById('countdown').innerText = "Streaming Stopped...";
                    updateBorder("#666");
                });
        }

        window.onload = () => {
            logMessage("Lunar Rover Alex: Control center loaded.");
            updateBorder("#666");
        }
    </script>
</head>
<body>
    <h1>Alex Remote Camera Control Center</h1>
    <div id="stream-container">
        <span>Video stream will appear here</span>
    </div>
    <button id="start-btn" onclick="startStream()">Activate Camera</button>
    <div id="countdown">Streaming Stopped...</div>
    <div id="log-container">
        <pre id="log"></pre>
    </div>
</body>
</html>
"""

class EdgeStreamingOutput:
    def __init__(self, camera):
        self.camera = camera
        self.frame = None
        self.condition = Condition()
        self.lock = Lock()
        self.active = False
        self.frame_id = 0
        self.thread = Thread(target=self._update_frames)
        self.thread.daemon = True
        self.thread.start()

    def start_streaming(self):
        with self.lock:
            self.frame = None
            self.active = True
            self.frame_id = 0
            logging.info("Lunar Rover Alex: Streaming handshake ACK - camera activated.")

    def stop_streaming(self):
        with self.lock:
            self.active = False
            self.frame = None
            logging.info("Lunar Rover Alex: Streaming deactivated - handshake closed.")

    def _update_frames(self):
        global latest_frame_info
        while True:
            if self.active:
                try:
                    frame = self.camera.capture_array()
                    self.frame_id += 1
                    timestamp = int(time.time() * 1000)

                    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                    edges = cv2.Canny(gray, 100, 200)
                    edges_colored = cv2.cvtColor(edges, cv2.COLOR_GRAY2RGB)

                    _, jpeg = cv2.imencode('.jpg', edges_colored)
                    with self.condition:
                        self.frame = jpeg.tobytes()
                        self.condition.notify_all()

                    latest_frame_info = {
                        'frame_id': self.frame_id,
                        'server_timestamp': timestamp
                    }

                except Exception as e:
                    logging.error(f"Streaming error: {e}")
                    self.frame = None
            else:
                latest_frame_info = {'frame_id': None, 'server_timestamp': None}
                time.sleep(0.05)

class StreamingHandler(server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(301)
            self.send_header('Location', '/index.html')
            self.end_headers()
        elif self.path == '/index.html':
            content = PAGE.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == '/start_stream':
            output.start_streaming()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ACK: stream started")
        elif self.path == '/stop_stream':
            output.stop_streaming()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ACK: stream stopped")
        elif self.path == '/frame_info':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(latest_frame_info).encode('utf-8'))
        elif self.path == '/stream.mjpg':
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            logging.info("Lunar Rover Alex: MJPEG connection accepted.")
            try:
                while True:
                    if not output.active:
                        time.sleep(0.1)
                        continue
                    with output.condition:
                        output.condition.wait(timeout=1.0)
                        frame = output.frame
                        if frame:
                            self.wfile.write(b'--FRAME\r\n')
                            self.send_header('Content-Type', 'image/jpeg')
                            self.send_header('Content-Length', len(frame))
                            self.end_headers()
                            self.wfile.write(frame)
                            self.wfile.write(b'\r\n')
            except Exception as e:
                logging.warning('Client disconnected: %s', str(e))
        else:
            self.send_error(404)
            self.end_headers()

class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

picam2 = Picamera2()
picam2.configure(picam2.create_video_configuration(main={"size": (640, 480)}))
picam2.start()

output = EdgeStreamingOutput(picam2)
latest_frame_info = {'frame_id': None, 'server_timestamp': None}

logging.info("Lunar Rover Alex: Communication channel established.")

try:
    address = ('', 8000)
    server = StreamingServer(address, StreamingHandler)
    logging.info("Lunar Rover Alex: Control server active at http://localhost:8000")
    server.serve_forever()
finally:
    output.stop_streaming()
    picam2.stop()
    logging.info("Lunar Rover Alex: System shutdown.")
