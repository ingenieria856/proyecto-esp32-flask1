from gevent import monkey
monkey.patch_all()
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt
import threading
import time
import os
import json
import re
import sqlite3  # Nueva librería para persistencia

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'mi_clave_secreta')  # Mejora seguridad
socketio = SocketIO(app, async_mode='gevent')

# Base de datos para persistencia
def init_db():
    conn = sqlite3.connect('devices.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS devices
                 (id TEXT PRIMARY KEY, name TEXT, topic TEXT, type TEXT, 
                  created_at REAL, last_update REAL, is_connected INTEGER,
                  value REAL, humidity REAL, temperature REAL)''')
    conn.commit()
    conn.close()

# Cargar dispositivos desde DB
def load_devices():
    devices = {}
    conn = sqlite3.connect('devices.db')
    c = conn.cursor()
    c.execute("SELECT * FROM devices")
    rows = c.fetchall()
    for row in rows:
        devices[row[0]] = {
            'name': row[1],
            'topic': row[2],
            'type': row[3],
            'created_at': row[4],
            'last_update': row[5],
            'is_connected': bool(row[6]),
            'value': row[7],
            'humidity': row[8],
            'temperature': row[9]
        }
    conn.close()
    return devices

# Guardar dispositivo en DB
def save_device(device_id, data):
    conn = sqlite3.connect('devices.db')
    c = conn.cursor()
    c.execute('''REPLACE INTO devices VALUES 
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (device_id, data['name'], data['topic'], data['type'],
               data.get('created_at', time.time()), data.get('last_update', 0),
               int(data.get('is_connected', False)), data.get('value', 0),
               data.get('humidity', 0), data.get('temperature', 0)))
    conn.commit()
    conn.close()

# Eliminar dispositivo de DB
def delete_device_db(device_id):
    conn = sqlite3.connect('devices.db')
    c = conn.cursor()
    c.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    conn.commit()
    conn.close()

# Inicializar DB y cargar dispositivos
init_db()
devices = load_devices()

# Configuración MQTT (mejorada con reconexión automática)
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883

mqtt_client = mqtt.Client(reconnect_on_failure=True)

def on_connect(client, userdata, flags, rc):
    print(f"✅ Conectado al broker MQTT (código {rc})")
    # Suscribirse dinámicamente a los tópicos existentes
    for device_id, data in devices.items():
        client.subscribe(data['topic'])
    socketio.emit('mqtt_status', {'connected': True})

def on_disconnect(client, userdata, rc):
    print(f"❌ Desconectado del broker MQTT (código {rc})")
    socketio.emit('mqtt_status', {'connected': False})

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        device_id = next((id for id, data in devices.items() if data['topic'] == msg.topic), None)
        
        if device_id:
            # Actualizar datos
            devices[device_id]['last_update'] = time.time()
            devices[device_id]['is_connected'] = True
            
            # Guardar valores según tipo
            if devices[device_id]['type'] == 'dht22':
                devices[device_id]['humidity'] = payload.get('humidity', 0)
                devices[device_id]['temperature'] = payload.get('temperature', 0)
            elif devices[device_id]['type'] == 'sensor':
                devices[device_id]['value'] = payload.get('value', 0)
            
            # Persistir en DB
            save_device(device_id, devices[device_id])
            
            # Enviar actualización
            socketio.emit('device_update', {
                'device_id': device_id,
                'data': devices[device_id]
            })
    except Exception as e:
        print(f"❌ Error procesando datos: {e}")

mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect
mqtt_client.on_message = on_message
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)

mqtt_thread = threading.Thread(target=mqtt_client.loop_forever)
mqtt_thread.daemon = True
mqtt_thread.start()

# Verificador de estado de conexión
def check_device_connection():
    while True:
        current_time = time.time()
        for device_id, device_data in devices.items():
            was_connected = device_data.get('is_connected', False)
            is_connected = (current_time - device_data.get('last_update', 0)) < 300
            
            if was_connected != is_connected:
                devices[device_id]['is_connected'] = is_connected
                save_device(device_id, devices[device_id])
                socketio.emit('connection_status', {
                    'device_id': device_id,
                    'is_connected': is_connected
                })
        time.sleep(60)

connection_thread = threading.Thread(target=check_device_connection)
connection_thread.daemon = True
connection_thread.start()

# Endpoints
@app.route("/")
def control_panel():
    return render_template("control_led.html", devices=devices)

@app.route("/add_device", methods=["POST"])
def add_device():
    data = request.json
    device_id = data['id']
    
    # Validación mejorada
    if not re.match(r'^\w{3,20}$', device_id):
        return jsonify(success=False, error="ID inválido (3-20 caracteres alfanuméricos)"), 400
        
    if device_id in devices:
        return jsonify(success=False, error="El ID ya existe"), 400
    
    # Crear nuevo dispositivo
    devices[device_id] = {
        'name': data.get('name', device_id),
        'topic': data['topic'],
        'type': data['type'],
        'created_at': time.time(),
        'last_update': 0,
        'is_connected': False,
        'value': 0,
        'humidity': 0,
        'temperature': 0
    }
    
    # Persistir y suscribir
    save_device(device_id, devices[device_id])
    mqtt_client.subscribe(data['topic'])
    
    return jsonify(success=True, device_id=device_id)

@app.route("/delete_device", methods=["POST"])
def delete_device():
    device_id = request.json['device_id']
    
    if device_id in devices:
        # Eliminar suscripción y dispositivo
        mqtt_client.unsubscribe(devices[device_id]['topic'])
        delete_device_db(device_id)
        del devices[device_id]
        return jsonify(success=True)
    
    return jsonify(success=False, error="Dispositivo no encontrado"), 404

@app.route("/control_device", methods=["POST"])
def control_device():
    data = request.json
    device_id = data['device_id']
    
    if device_id in devices:
        control_topic = f"{devices[device_id]['topic']}/control"
        mqtt_client.publish(control_topic, data['command'])
        return jsonify(success=True)
    
    return jsonify(success=False, error="Dispositivo no encontrado"), 404

# Mantener activo en Render
def keep_alive():
    import requests
    while True:
        try:
            requests.get("https://tu-app.onrender.com")
            print("✅ Ping enviado")
        except: pass
        time.sleep(300)

if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    port = int(os.environ.get("PORT", 8000))
    socketio.run(app, host="0.0.0.0", port=port)
