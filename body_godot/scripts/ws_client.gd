extends Node
class_name QimingxingWSClient

signal connected_changed(is_connected: bool)
signal envelope_received(event_type: String, turn_id: String, payload: Dictionary)
signal raw_error(message: String)

var ws_url: String = "ws://127.0.0.1:18790/ws"
var http_base: String = "http://127.0.0.1:18790"

var _ws: WebSocketPeer = WebSocketPeer.new()
var _is_open: bool = false
var _reconnect_delay: float = 1.0
var _reconnect_timer: float = 0.0
var _ping_timer: float = 0.0
var _seq_id: int = 0


func _ready() -> void:
	var env_ws := OS.get_environment("QIMINGXING_WS_URL")
	if env_ws != "":
		ws_url = env_ws
	var env_http := OS.get_environment("QIMINGXING_HTTP_BASE")
	if env_http != "":
		http_base = env_http.trim_suffix("/")
	_connect_now()


func _connect_now() -> void:
	_ws = WebSocketPeer.new()
	_ws.inbound_buffer_size = 4194304
	_ws.outbound_buffer_size = 1048576
	_is_open = false
	var err := _ws.connect_to_url(ws_url)
	if err != OK:
		raw_error.emit("WebSocket 连接发起失败: %d" % err)
		_schedule_reconnect()


func _schedule_reconnect() -> void:
	_reconnect_timer = _reconnect_delay
	_reconnect_delay = minf(_reconnect_delay * 2.0, 5.0)


func _process(delta: float) -> void:
	if _reconnect_timer > 0.0:
		_reconnect_timer -= delta
		if _reconnect_timer <= 0.0:
			_connect_now()
		return

	_ws.poll()
	var state := _ws.get_ready_state()

	if state == WebSocketPeer.STATE_OPEN:
		if not _is_open:
			_is_open = true
			_reconnect_delay = 1.0
			_ping_timer = 0.0
			connected_changed.emit(true)
			send_event("system.hello", {
				"client_name": "body_godot",
				"role": "body",
				"client_type": "body_godot",
				"capabilities": ["audio_out", "pin_image", "auto_hide"],
			})

		_ping_timer += delta
		if _ping_timer >= 15.0:
			_ping_timer = 0.0
			send_event("system.ping", {"client_ts": int(Time.get_unix_time_from_system() * 1000.0)})

		while _ws.get_available_packet_count() > 0:
			var pkt := _ws.get_packet()
			if _ws.was_string_packet():
				_handle_text_packet(pkt.get_string_from_utf8())
	elif state == WebSocketPeer.STATE_CLOSED:
		if _is_open:
			_is_open = false
			connected_changed.emit(false)
		_schedule_reconnect()


func _handle_text_packet(raw_text: String) -> void:
	var parsed: Variant = JSON.parse_string(raw_text)
	if typeof(parsed) != TYPE_DICTIONARY:
		return
	var env: Dictionary = parsed
	var ev_type := str(env.get("type", ""))
	if ev_type == "":
		return
	var turn_id_val: Variant = env.get("turn_id", "")
	var turn_id := "" if turn_id_val == null else str(turn_id_val)
	var payload: Dictionary = {}
	var raw_payload: Variant = env.get("payload", {})
	if typeof(raw_payload) == TYPE_DICTIONARY:
		payload = raw_payload
	envelope_received.emit(ev_type, turn_id, payload)


func send_event(event_type: String, payload: Dictionary = {}, turn_id: String = "") -> void:
	if not _is_open:
		return
	_seq_id += 1
	var env := {
		"v": "1.0",
		"id": "godot_%d_%d" % [Time.get_ticks_msec(), _seq_id],
		"ts": int(Time.get_unix_time_from_system() * 1000.0),
		"source": "body",
		"type": event_type,
		"payload": payload,
	}
	if turn_id != "":
		env["turn_id"] = turn_id
	_ws.send_text(JSON.stringify(env))
