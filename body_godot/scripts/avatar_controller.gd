extends Control
class_name QimingxingAvatarController

signal avatar_clicked
signal files_dropped_queued(paths: PackedStringArray)

const SPRITE_PATHS := {
	"idle": "res://assets/sprites/idle.png",
	"speaking": "res://assets/sprites/speaking.png",
	"happy": "res://assets/sprites/happy.png",
	"thinking": "res://assets/sprites/thinking.png",
	"working": "res://assets/sprites/working.png",
	"alert": "res://assets/sprites/alert.png",
	"sorry": "res://assets/sprites/sorry.png",
	"receiving": "res://assets/sprites/receiving.png",
}

const STATE_RING_COLORS := {
	"idle": Color(0.28, 0.78, 0.96, 0.55),
	"speaking": Color(0.20, 0.92, 0.85, 0.88),
	"happy": Color(0.35, 0.95, 0.65, 0.82),
	"thinking": Color(0.98, 0.78, 0.25, 0.85),
	"working": Color(0.30, 0.90, 0.50, 0.85),
	"alert": Color(1.00, 0.48, 0.25, 0.95),
	"sorry": Color(0.95, 0.36, 0.42, 0.85),
	"receiving": Color(0.42, 0.88, 1.00, 0.92),
}

var _textures: Dictionary = {}
var _sprite_rect: TextureRect
var _base_state: String = "idle"
var _is_tts_speaking: bool = false
var _mouth_open: bool = false
var _rms: float = 0.0
var _pulse_time: float = 0.0

var _dragging: bool = false
var _drag_mouse_start: Vector2i = Vector2i.ZERO
var _drag_win_start: Vector2i = Vector2i.ZERO
var _drag_moved: bool = false

var _receiving_hold_timer: float = 0.0
var _hide_failsafe_timer: float = 0.0
var _ui_interactive_rects: Array[Rect2] = []


func _ready() -> void:
	for k in SPRITE_PATHS.keys():
		var res := load(SPRITE_PATHS[k])
		if res is Texture2D:
			_textures[k] = res

	_sprite_rect = TextureRect.new()
	_sprite_rect.name = "SpriteRect"
	_sprite_rect.anchors_preset = PRESET_FULL_RECT
	_sprite_rect.anchor_right = 1.0
	_sprite_rect.anchor_bottom = 1.0
	_sprite_rect.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	_sprite_rect.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	_sprite_rect.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_sprite_rect.pivot_offset = size * 0.5
	add_child(_sprite_rect)

	var win := get_window()
	if win != null:
		win.files_dropped.connect(_on_window_files_dropped)

	_apply_sprite("idle")
	call_deferred("refresh_mouse_passthrough")


func _notification(what: int) -> void:
	if what == NOTIFICATION_RESIZED and _sprite_rect != null:
		_sprite_rect.pivot_offset = size * 0.5
		refresh_mouse_passthrough()


func set_avatar_state(emotion: String, activity: String) -> void:
	var mapped := "idle"
	if activity == "tool_running":
		mapped = "working"
	elif activity == "thinking":
		mapped = "thinking"
	else:
		match emotion:
			"happy":
				mapped = "happy"
			"thinking":
				mapped = "thinking"
			"warning", "surprised":
				mapped = "alert"
			"confused":
				mapped = "sorry"
			_:
				mapped = "speaking" if activity == "speaking" else "idle"
	_base_state = mapped
	if _receiving_hold_timer <= 0.0:
		_apply_current_visual()


func set_approval_alert(active: bool) -> void:
	if active:
		_base_state = "alert"
		_apply_current_visual()


func set_tts_speaking(speaking: bool) -> void:
	_is_tts_speaking = speaking
	if not speaking:
		_mouth_open = false
		_rms = 0.0
		if _sprite_rect != null:
			_sprite_rect.scale = Vector2.ONE
		if _base_state == "speaking":
			_base_state = "idle"
	_apply_current_visual()


func set_tts_amplitude(rms: float) -> void:
	_rms = rms
	_mouth_open = _is_tts_speaking and (rms > 0.05)
	if _sprite_rect != null:
		var boost := 1.0 + rms * 0.06
		_sprite_rect.scale = Vector2(boost, boost)
	_apply_current_visual()


func set_ui_interactive_rects(rects: Array[Rect2]) -> void:
	_ui_interactive_rects = rects
	refresh_mouse_passthrough()


func _apply_current_visual() -> void:
	if _receiving_hold_timer > 0.0:
		_apply_sprite("receiving")
		return
	if _is_tts_speaking and _mouth_open and _base_state in ["idle", "speaking", "happy"]:
		_apply_sprite("speaking")
	else:
		_apply_sprite(_base_state)


func _apply_sprite(state_key: String) -> void:
	if _sprite_rect == null:
		return
	var tex: Texture2D = _textures.get(state_key, _textures.get("idle"))
	if tex != null and _sprite_rect.texture != tex:
		_sprite_rect.texture = tex


func _process(delta: float) -> void:
	_pulse_time += delta
	if _receiving_hold_timer > 0.0:
		_receiving_hold_timer -= delta
		if _receiving_hold_timer <= 0.0:
			_apply_current_visual()

	if _hide_failsafe_timer > 0.0:
		_hide_failsafe_timer -= delta
		if _hide_failsafe_timer <= 0.0:
			set_capture_hidden(false)

	queue_redraw()


func _draw() -> void:
	# Companion breathing status ring anchored smoothly behind character base
	var ring_state := "receiving" if _receiving_hold_timer > 0.0 else (
		"speaking" if _is_tts_speaking else _base_state
	)
	var base_col: Color = STATE_RING_COLORS.get(ring_state, STATE_RING_COLORS["idle"])
	var breath := 0.5 + 0.5 * sin(_pulse_time * (5.2 if _is_tts_speaking else 2.2))
	var center := Vector2(size.x * 0.5, size.y * 0.88)
	var rx := 72.0 + breath * 8.0 + _rms * 14.0
	var ry := 20.0 + breath * 2.5

	var pts := PackedVector2Array()
	var segs := 40
	for i in range(segs + 1):
		var ang := TAU * float(i) / float(segs)
		pts.append(Vector2(center.x + cos(ang) * rx, center.y + sin(ang) * ry))

	var outer_col := Color(base_col.r, base_col.g, base_col.b, base_col.a * (0.45 + 0.45 * breath))
	draw_polyline(pts, outer_col, 3.0, true)

	var inner_col := Color(base_col.r, base_col.g, base_col.b, 0.14 + 0.12 * breath)
	draw_colored_polygon(pts, inner_col)


func _gui_input(event: InputEvent) -> void:
	if event is InputEventMouseButton:
		var mb := event as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_LEFT:
			if mb.pressed:
				_dragging = true
				_drag_moved = false
				_drag_mouse_start = DisplayServer.mouse_get_position()
				_drag_win_start = DisplayServer.window_get_position()
			else:
				var was_click := _dragging and not _drag_moved
				_dragging = false
				if was_click:
					avatar_clicked.emit()
	elif event is InputEventMouseMotion and _dragging:
		var cur := DisplayServer.mouse_get_position()
		var diff := cur - _drag_mouse_start
		if diff.length() > 3.0:
			_drag_moved = true
		DisplayServer.window_set_position(_drag_win_start + diff)


func _on_window_files_dropped(files: PackedStringArray) -> void:
	if files.is_empty():
		return
	_receiving_hold_timer = 1.8
	_apply_sprite("receiving")
	files_dropped_queued.emit(files)


func set_capture_hidden(hidden: bool, failsafe_sec: float = 2.0) -> void:
	var win := get_window()
	if win == null:
		return
	if hidden:
		win.visible = false
		_hide_failsafe_timer = maxf(0.5, failsafe_sec)
	else:
		_hide_failsafe_timer = 0.0
		win.visible = true
		call_deferred("refresh_mouse_passthrough")


func refresh_mouse_passthrough() -> void:
	if not DisplayServer.has_feature(DisplayServer.FEATURE_MOUSE_PASSTHROUGH):
		return
	# Compute convex bounding region covering character body + any active UI overlays
	var avatar_rect := Rect2(position + Vector2(36.0, 20.0), size - Vector2(72.0, 24.0))
	var combined := avatar_rect
	for r in _ui_interactive_rects:
		if r.size.x > 1.0 and r.size.y > 1.0:
			combined = combined.merge(r)
	var poly := PackedVector2Array([
		Vector2(combined.position.x, combined.position.y),
		Vector2(combined.end.x, combined.position.y),
		Vector2(combined.end.x, combined.end.y),
		Vector2(combined.position.x, combined.end.y),
	])
	DisplayServer.window_set_mouse_passthrough(poly)
