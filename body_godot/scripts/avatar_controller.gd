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
var _rendered_state: String = ""
var _is_tts_speaking: bool = false
var _mouth_open: bool = false
var _rms: float = 0.0
var _pulse_time: float = 0.0

var _dragging: bool = false
var _drag_mouse_start: Vector2i = Vector2i.ZERO
var _drag_win_start: Vector2i = Vector2i.ZERO
var _drag_moved: bool = false

var _is_hovered: bool = false
var _hover_weight: float = 0.0
var _cur_offset: Vector2 = Vector2.ZERO
var _cur_rot: float = 0.0
var _cur_scale: Vector2 = Vector2.ONE
var _bounce_energy: float = 0.0
var _bounce_phase: float = 0.0

var _poke_happy_timer: float = 0.0
var _idle_quirk_cooldown: float = 6.5
var _idle_quirk_active: float = 0.0
var _idle_quirk_dir: float = 1.0

var _receiving_hold_timer: float = 0.0
var _hide_failsafe_timer: float = 0.0
var _ui_interactive_rects: Array[Rect2] = []


func _ready() -> void:
	for k in SPRITE_PATHS.keys():
		var path: String = SPRITE_PATHS[k]
		var tex: Texture2D = null
		var abs_path := ProjectSettings.globalize_path(path)
		if FileAccess.file_exists(abs_path):
			var img := Image.load_from_file(abs_path)
			if img != null and not img.is_empty():
				tex = ImageTexture.create_from_image(img)
		if tex == null and ResourceLoader.exists(path):
			var res := load(path)
			if res is Texture2D:
				tex = res
		if tex != null:
			_textures[k] = tex

	_sprite_rect = TextureRect.new()
	_sprite_rect.name = "SpriteRect"
	_sprite_rect.position = Vector2.ZERO
	_sprite_rect.size = size
	_sprite_rect.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	_sprite_rect.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	_sprite_rect.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_sprite_rect.pivot_offset = Vector2(size.x * 0.5, size.y * 0.72)
	add_child(_sprite_rect)

	mouse_entered.connect(func() -> void: _is_hovered = true)
	mouse_exited.connect(func() -> void: _is_hovered = false)

	var win := get_window()
	if win != null:
		win.borderless = true
		win.transparent = true
		win.transparent_bg = true
		win.always_on_top = true
		win.files_dropped.connect(_on_window_files_dropped)
		var usable := DisplayServer.screen_get_usable_rect()
		if usable.size.x > win.size.x and usable.size.y > win.size.y:
			win.position = Vector2i(
				usable.position.x + usable.size.x - win.size.x - 36,
				usable.position.y + usable.size.y - win.size.y - 36
			)

	_apply_sprite("idle", false)
	call_deferred("refresh_mouse_passthrough")


func _notification(what: int) -> void:
	if what == NOTIFICATION_RESIZED and _sprite_rect != null:
		_sprite_rect.size = size
		_sprite_rect.pivot_offset = Vector2(size.x * 0.5, size.y * 0.72)
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
		_apply_current_visual(true)


func set_approval_alert(active: bool) -> void:
	if active:
		_base_state = "alert"
		_apply_current_visual(true)


func set_tts_speaking(speaking: bool) -> void:
	_is_tts_speaking = speaking
	if not speaking:
		_mouth_open = false
		_rms = 0.0
		if _base_state == "speaking":
			_base_state = "idle"
	_apply_current_visual(false)


func set_tts_amplitude(rms: float) -> void:
	_rms = rms
	_mouth_open = _is_tts_speaking and (rms > 0.05)
	_apply_current_visual(false)


func set_ui_interactive_rects(rects: Array[Rect2]) -> void:
	_ui_interactive_rects = rects
	refresh_mouse_passthrough()


func trigger_bounce(strength: float = 0.85) -> void:
	_bounce_energy = clampf(maxf(_bounce_energy, strength), 0.0, 1.35)
	_bounce_phase = 0.0


func _apply_current_visual(allow_bounce: bool = true) -> void:
	if _receiving_hold_timer > 0.0:
		_apply_sprite("receiving", allow_bounce)
		return
	if _poke_happy_timer > 0.0 and not _is_tts_speaking and _base_state == "idle":
		_apply_sprite("happy", allow_bounce)
		return
	if _is_tts_speaking and _mouth_open and _base_state in ["idle", "speaking", "happy"]:
		_apply_sprite("speaking", false)
	else:
		_apply_sprite(_base_state, allow_bounce)


func _apply_sprite(state_key: String, allow_bounce: bool = true) -> void:
	if _sprite_rect == null:
		return
	var tex: Texture2D = _textures.get(state_key, _textures.get("idle"))
	var changed := state_key != _rendered_state
	_rendered_state = state_key
	if tex != null and _sprite_rect.texture != tex:
		_sprite_rect.texture = tex
	if changed and allow_bounce:
		trigger_bounce(0.75)


func _process(delta: float) -> void:
	_pulse_time += delta
	if _receiving_hold_timer > 0.0:
		_receiving_hold_timer -= delta
		if _receiving_hold_timer <= 0.0:
			_apply_current_visual(false)

	if _poke_happy_timer > 0.0:
		_poke_happy_timer -= delta
		if _poke_happy_timer <= 0.0:
			_apply_current_visual(false)

	if _hide_failsafe_timer > 0.0:
		_hide_failsafe_timer -= delta
		if _hide_failsafe_timer <= 0.0:
			set_capture_hidden(false)

	# Idle life cues so the desktop companion feels alive even when quiet
	var visual_state := _rendered_state if _rendered_state != "" else _base_state
	if visual_state == "idle" and not _dragging and not _is_tts_speaking and _receiving_hold_timer <= 0.0:
		if _idle_quirk_active > 0.0:
			_idle_quirk_active -= delta
		else:
			_idle_quirk_cooldown -= delta
			if _idle_quirk_cooldown <= 0.0:
				_idle_quirk_cooldown = randf_range(8.5, 14.5)
				if randf() < 0.45:
					trigger_bounce(0.55)
				else:
					_idle_quirk_active = 1.25
					_idle_quirk_dir = -1.0 if randf() < 0.5 else 1.0
	else:
		_idle_quirk_active = 0.0

	_update_procedural_motion(delta, visual_state)
	queue_redraw()


func _update_procedural_motion(delta: float, state_key: String) -> void:
	if _sprite_rect == null:
		return

	_hover_weight = move_toward(_hover_weight, 1.0 if (_is_hovered and not _dragging) else 0.0, delta * 5.5)

	# 1. Base magnetic-levitation float & state-specific rhythm
	var float_y := sin(_pulse_time * 2.15) * 6.0
	var sway_rot := sin(_pulse_time * 1.1) * deg_to_rad(2.0)
	var breath := sin(_pulse_time * 2.15 - 0.5)
	var scale_x := 1.0 + breath * 0.014
	var scale_y := 1.0 - breath * 0.014
	var lean_x := 0.0

	match state_key:
		"thinking":
			float_y = sin(_pulse_time * 1.55) * 4.5 - 2.0
			sway_rot = sin(_pulse_time * 1.25) * deg_to_rad(5.2)
		"working":
			float_y = sin(_pulse_time * 4.0) * 3.5 + 1.0
			sway_rot = deg_to_rad(3.2) + sin(_pulse_time * 3.8) * deg_to_rad(1.4)
			lean_x = 2.0
		"alert", "receiving":
			float_y = sin(_pulse_time * 3.2) * 5.0 - 5.5
			sway_rot = sin(_pulse_time * 2.4) * deg_to_rad(2.8)
		"sorry":
			float_y = sin(_pulse_time * 1.4) * 3.0 + 4.5
			sway_rot = deg_to_rad(-3.2) + sin(_pulse_time * 1.1) * deg_to_rad(1.2)
		"happy":
			float_y = -absf(sin(_pulse_time * 3.8)) * 7.5
			sway_rot = sin(_pulse_time * 3.4) * deg_to_rad(4.2)

	# 2. Voice amplitude dance when speaking
	if _is_tts_speaking:
		float_y -= _rms * 7.0
		var v_boost := _rms * 0.065
		scale_x += v_boost * 0.65
		scale_y += v_boost

	# 3. Damped spring squash & stretch on state change / click poke
	if _bounce_energy > 0.004:
		_bounce_phase += delta * 14.0
		var wave := sin(_bounce_phase) * _bounce_energy
		float_y -= maxf(0.0, wave) * 12.0
		scale_x -= wave * 0.065
		scale_y += wave * 0.085
		_bounce_energy = lerpf(_bounce_energy, 0.0, minf(1.0, delta * 5.8))
	else:
		_bounce_energy = 0.0

	# 4. Playful idle head-tilt quirk
	if _idle_quirk_active > 0.0:
		var env := sin(PI * clampf(_idle_quirk_active / 1.25, 0.0, 1.0))
		sway_rot += _idle_quirk_dir * deg_to_rad(6.5) * env

	# 5. Mouse hover response: subtle lift & look toward pointer
	if _hover_weight > 0.001:
		var rel_x := clampf((get_local_mouse_position().x - size.x * 0.5) / maxf(1.0, size.x * 0.5), -1.0, 1.0)
		float_y -= 4.5 * _hover_weight
		sway_rot += rel_x * deg_to_rad(5.0) * _hover_weight
		scale_x += 0.03 * _hover_weight
		scale_y += 0.03 * _hover_weight

	var target_offset := Vector2(lean_x, float_y)
	var target_scale := Vector2(scale_x, scale_y)
	var smooth := minf(1.0, delta * 14.0)
	_cur_offset = _cur_offset.lerp(target_offset, smooth)
	_cur_rot = lerpf(_cur_rot, sway_rot, smooth)
	_cur_scale = _cur_scale.lerp(target_scale, smooth)

	_sprite_rect.position = _cur_offset
	_sprite_rect.rotation = _cur_rot
	_sprite_rect.scale = _cur_scale


func _draw() -> void:
	# Companion breathing status ring inversely coupled with levitation height
	var ring_state := "receiving" if _receiving_hold_timer > 0.0 else (
		"speaking" if _is_tts_speaking else (
			"happy" if (_poke_happy_timer > 0.0 and _base_state == "idle") else _base_state
		)
	)
	var base_col: Color = STATE_RING_COLORS.get(ring_state, STATE_RING_COLORS["idle"])
	var breath := 0.5 + 0.5 * sin(_pulse_time * (5.2 if _is_tts_speaking else 2.2))
	var lift_comp := clampf(-_cur_offset.y / 12.0, -0.5, 1.0)
	var center := Vector2(size.x * 0.5, size.y * 0.88)
	var rx := 72.0 - lift_comp * 6.0 + breath * 6.0 + _rms * 14.0 + _hover_weight * 5.0
	var ry := 20.0 - lift_comp * 1.5 + breath * 2.2

	var pts := PackedVector2Array()
	var segs := 44
	for i in range(segs + 1):
		var ang := TAU * float(i) / float(segs)
		pts.append(Vector2(center.x + cos(ang) * rx, center.y + sin(ang) * ry))

	var outer_col := Color(base_col.r, base_col.g, base_col.b, base_col.a * (0.48 + 0.42 * breath))
	draw_polyline(pts, outer_col, 3.0, true)

	var inner_col := Color(base_col.r, base_col.g, base_col.b, 0.14 + 0.10 * breath)
	draw_colored_polygon(pts, inner_col)

	# Rotating telemetry arc while thinking or executing tools
	if ring_state in ["working", "thinking", "speaking"]:
		var arc_pts := PackedVector2Array()
		var arc_start := _pulse_time * (3.6 if ring_state == "working" else 2.0)
		for i in range(14):
			var ang := arc_start + (PI * 0.55) * (float(i) / 13.0)
			arc_pts.append(Vector2(center.x + cos(ang) * (rx + 5.0), center.y + sin(ang) * (ry + 2.0)))
		draw_polyline(arc_pts, Color(base_col.r, base_col.g, base_col.b, 0.95), 2.5, true)


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
					_poke_happy_timer = 1.35
					trigger_bounce(1.0)
					_apply_current_visual(false)
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
	_apply_sprite("receiving", true)
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
	var win := get_window()
	if win == null:
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
	win.mouse_passthrough_polygon = poly
