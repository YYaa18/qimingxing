extends Control

signal avatar_clicked
signal files_dropped_queued(paths: PackedStringArray)

const FACE_DISPLAY_SCRIPT := preload("res://scripts/face_display.gd")

const RIG_PATHS := {
	"body": "res://assets/rig/body.png",
	"head_shell": "res://assets/rig/head_shell.png",
	"antenna": "res://assets/rig/antenna.png",
	"hand_left": "res://assets/rig/hand_left.png",
	"hand_right": "res://assets/rig/hand_right.png",
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

var _rig_textures: Dictionary = {}
var _rig_root: Node2D
var _body_sprite: Sprite2D
var _hand_left: Sprite2D
var _hand_right: Sprite2D
var _head_pivot: Node2D
var _head_sprite: Sprite2D
var _antenna_pivot: Node2D
var _antenna_sprite: Sprite2D
var _face_display: Node2D

var _base_state: String = "idle"
var _last_pose_state: String = "idle"
var _is_tts_speaking: bool = false
var _rms: float = 0.0
var _pulse_time: float = 0.0

var _dragging: bool = false
var _drag_mouse_start: Vector2i = Vector2i.ZERO
var _drag_win_start: Vector2i = Vector2i.ZERO
var _drag_moved: bool = false

var _is_hovered: bool = false
var _hover_weight: float = 0.0
var _cur_offset: Vector2 = Vector2.ZERO
var _bounce_energy: float = 0.0
var _bounce_phase: float = 0.0

# Spring physics & gaze state for option-3 multi-part rig
var _prev_head_rot: float = 0.0
var _antenna_rot: float = 0.0
var _antenna_vel: float = 0.0
var _cur_gaze: Vector2 = Vector2.ZERO
var _blink_timer: float = 3.2
var _blink_progress: float = 0.0
var _is_blinking: bool = false

var _poke_happy_timer: float = 0.0
var _idle_quirk_cooldown: float = 7.0
var _idle_quirk_active: float = 0.0
var _idle_quirk_dir: float = 1.0

var _receiving_hold_timer: float = 0.0
var _hide_failsafe_timer: float = 0.0
var _ui_interactive_rects: Array[Rect2] = []

const RIG_ANCHOR := Vector2(130.0, 146.0)
const LEFT_HAND_BASE := Vector2(-58.0, 17.0)
const RIGHT_HAND_BASE := Vector2(57.0, 17.0)


func _ready() -> void:
	for k in RIG_PATHS.keys():
		var tex := _load_tex(RIG_PATHS[k])
		if tex != null:
			_rig_textures[k] = tex

	_build_rig_hierarchy()

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

	call_deferred("refresh_mouse_passthrough")


func _load_tex(res_path: String) -> Texture2D:
	var abs_path := ProjectSettings.globalize_path(res_path)
	if FileAccess.file_exists(abs_path):
		var img := Image.load_from_file(abs_path)
		if img != null and not img.is_empty():
			return ImageTexture.create_from_image(img)
	if ResourceLoader.exists(res_path):
		var res := load(res_path)
		if res is Texture2D:
			return res
	return null


func _make_sprite(tex_key: String, sprite_offset: Vector2) -> Sprite2D:
	var sp := Sprite2D.new()
	sp.texture = _rig_textures.get(tex_key, null)
	sp.centered = false
	sp.offset = sprite_offset
	return sp


func _build_rig_hierarchy() -> void:
	_rig_root = Node2D.new()
	_rig_root.name = "RigRoot"
	_rig_root.position = RIG_ANCHOR
	add_child(_rig_root)

	# 1. Floating hands & torso underneath head collar
	_hand_left = _make_sprite("hand_left", Vector2(-24.0, -8.0))
	_hand_left.position = LEFT_HAND_BASE
	_rig_root.add_child(_hand_left)

	_hand_right = _make_sprite("hand_right", Vector2(-7.0, -8.0))
	_hand_right.position = RIGHT_HAND_BASE
	_rig_root.add_child(_hand_right)

	_body_sprite = _make_sprite("body", Vector2(-48.0, -39.0))
	_body_sprite.position = Vector2(0.0, 38.0)
	_rig_root.add_child(_body_sprite)

	# 2. Articulated Head Pivot at neck joint (0, 0)
	_head_pivot = Node2D.new()
	_head_pivot.name = "HeadPivot"
	_head_pivot.position = Vector2.ZERO
	_rig_root.add_child(_head_pivot)

	# Antenna socket tucks cleanly behind helmet dome (crown = (0, -102))
	_antenna_pivot = Node2D.new()
	_antenna_pivot.name = "AntennaPivot"
	_antenna_pivot.position = Vector2(0.0, -102.0)
	_head_pivot.add_child(_antenna_pivot)

	_antenna_sprite = _make_sprite("antenna", Vector2(-13.0, -28.0))
	_antenna_pivot.add_child(_antenna_sprite)

	_head_sprite = _make_sprite("head_shell", Vector2(-64.0, -104.0))
	_head_pivot.add_child(_head_sprite)

	# 3. Real-time vector cyan LED face inside visor
	_face_display = Node2D.new()
	_face_display.name = "FaceDisplay"
	_face_display.set_script(FACE_DISPLAY_SCRIPT)
	_face_display.position = Vector2(0.0, -40.0)
	_head_pivot.add_child(_face_display)


func _notification(what: int) -> void:
	if what == NOTIFICATION_RESIZED:
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
	if mapped != _base_state:
		_base_state = mapped
		trigger_bounce(0.75)


func set_approval_alert(active: bool) -> void:
	if active and _base_state != "alert":
		_base_state = "alert"
		trigger_bounce(0.85)


func set_tts_speaking(speaking: bool) -> void:
	_is_tts_speaking = speaking
	if not speaking:
		_rms = 0.0
		if _base_state == "speaking":
			_base_state = "idle"


func set_tts_amplitude(rms: float) -> void:
	_rms = rms


func set_ui_interactive_rects(rects: Array[Rect2]) -> void:
	_ui_interactive_rects = rects
	refresh_mouse_passthrough()


func trigger_bounce(strength: float = 0.85) -> void:
	_bounce_energy = clampf(maxf(_bounce_energy, strength), 0.0, 1.35)
	_bounce_phase = 0.0


func _active_pose_state() -> String:
	if _receiving_hold_timer > 0.0:
		return "receiving"
	if _poke_happy_timer > 0.0 and not _is_tts_speaking and _base_state == "idle":
		return "happy"
	return _base_state


func _process(delta: float) -> void:
	_pulse_time += delta
	if _receiving_hold_timer > 0.0:
		_receiving_hold_timer -= delta
	if _poke_happy_timer > 0.0:
		_poke_happy_timer -= delta
	if _hide_failsafe_timer > 0.0:
		_hide_failsafe_timer -= delta
		if _hide_failsafe_timer <= 0.0:
			set_capture_hidden(false)

	var pose_state := _active_pose_state()
	if pose_state != _last_pose_state:
		_last_pose_state = pose_state

	# Random idle quirks
	if pose_state == "idle" and not _dragging and not _is_tts_speaking:
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

	_update_blink(delta)
	_update_rig_kinematics(delta, pose_state)
	queue_redraw()


func _update_blink(delta: float) -> void:
	if _is_blinking:
		_blink_progress += delta * 9.5
		if _blink_progress >= PI:
			_is_blinking = false
			_blink_progress = 0.0
			_blink_timer = 0.14 if randf() < 0.2 else randf_range(2.6, 5.4)
		_face_display.set("blink_amount", sin(_blink_progress))
	else:
		_blink_timer -= delta
		if _blink_timer <= 0.0:
			_is_blinking = true
			_blink_progress = 0.0
		_face_display.set("blink_amount", 0.0)


func _compute_global_pointer_offset() -> Vector2:
	var win := get_window()
	if win == null:
		return Vector2.ZERO
	var global_mouse := Vector2(DisplayServer.mouse_get_position())
	var head_screen := Vector2(win.position) + position + RIG_ANCHOR + Vector2(0.0, -40.0)
	var diff := global_mouse - head_screen
	return Vector2(
		clampf(diff.x / 460.0, -1.0, 1.0),
		clampf(diff.y / 380.0, -1.0, 1.0)
	)


func _update_rig_kinematics(delta: float, pose_state: String) -> void:
	var dt := minf(delta, 0.05)
	_hover_weight = move_toward(_hover_weight, 1.0 if (_is_hovered and not _dragging) else 0.0, dt * 5.5)

	# 1. Global mouse tracking for gaze + gentle head look
	var target_gaze := _compute_global_pointer_offset()
	if pose_state == "thinking":
		target_gaze = Vector2(0.65, -0.55) + target_gaze * 0.25
	elif pose_state == "sorry":
		target_gaze = Vector2(target_gaze.x * 0.3, 0.55)
	_cur_gaze = _cur_gaze.lerp(target_gaze, minf(1.0, dt * 8.5))

	# 2. Whole-body magnetic levitation & spring squash/stretch
	var float_y := sin(_pulse_time * 2.15) * 5.5
	var breath := sin(_pulse_time * 2.15 - 0.6)
	var root_sx := 1.0 + breath * 0.012
	var root_sy := 1.0 - breath * 0.012
	var root_rot := sin(_pulse_time * 1.1) * deg_to_rad(1.4)

	if pose_state == "working":
		float_y = sin(_pulse_time * 4.2) * 3.2 + 1.5
		root_rot = deg_to_rad(2.5)
	elif pose_state == "alert" or pose_state == "receiving":
		float_y = sin(_pulse_time * 3.0) * 4.5 - 5.0
	elif pose_state == "sorry":
		float_y = sin(_pulse_time * 1.4) * 2.5 + 4.5
		root_rot = deg_to_rad(-2.2)
	elif pose_state == "happy":
		float_y = -absf(sin(_pulse_time * 3.8)) * 7.0

	if _is_tts_speaking:
		float_y -= _rms * 6.5
		root_sx += _rms * 0.045
		root_sy += _rms * 0.06

	if _bounce_energy > 0.004:
		_bounce_phase += dt * 14.0
		var wave := sin(_bounce_phase) * _bounce_energy
		float_y -= maxf(0.0, wave) * 11.0
		root_sx -= wave * 0.06
		root_sy += wave * 0.08
		_bounce_energy = lerpf(_bounce_energy, 0.0, minf(1.0, dt * 5.8))
	else:
		_bounce_energy = 0.0

	if _hover_weight > 0.001:
		float_y -= 4.0 * _hover_weight
		root_sx += 0.025 * _hover_weight
		root_sy += 0.025 * _hover_weight

	var target_root_pos := RIG_ANCHOR + Vector2(0.0, float_y)
	_cur_offset = _cur_offset.lerp(Vector2(0.0, float_y), minf(1.0, dt * 12.0))
	_rig_root.position = RIG_ANCHOR + _cur_offset
	_rig_root.rotation = lerpf(_rig_root.rotation, root_rot, minf(1.0, dt * 10.0))
	_rig_root.scale = _rig_root.scale.lerp(Vector2(root_sx, root_sy), minf(1.0, dt * 12.0))
	_body_sprite.scale = Vector2(1.0 + breath * 0.015, 1.0 - breath * 0.015)

	# 3. Articulated head rotation (tracks cursor + idle quirk + thinking tilt)
	var target_head_rot := _cur_gaze.x * deg_to_rad(6.0)
	var target_head_pos := Vector2(_cur_gaze.x * 1.8, sin(_pulse_time * 2.15 - 0.3) * 1.4)
	if pose_state == "thinking":
		target_head_rot += deg_to_rad(-5.5) + sin(_pulse_time * 1.3) * deg_to_rad(3.5)
	elif pose_state == "sorry":
		target_head_rot += deg_to_rad(-5.0)
		target_head_pos.y += 3.0
	elif pose_state == "working":
		target_head_rot += deg_to_rad(3.5)
	elif _idle_quirk_active > 0.0:
		var qenv := sin(PI * clampf(_idle_quirk_active / 1.25, 0.0, 1.0))
		target_head_rot += _idle_quirk_dir * deg_to_rad(9.5) * qenv

	var new_head_rot := lerpf(_head_pivot.rotation, target_head_rot, minf(1.0, dt * 10.0))
	var head_ang_vel := (new_head_rot - _prev_head_rot) / maxf(0.001, dt)
	_prev_head_rot = new_head_rot
	_head_pivot.rotation = new_head_rot
	_head_pivot.position = _head_pivot.position.lerp(target_head_pos, minf(1.0, dt * 10.0))

	# 4. Spring pendulum physics on star antenna
	var ant_rest := sin(_pulse_time * 2.4) * deg_to_rad(3.0) - new_head_rot * 0.25
	var stiffness := 130.0
	var damping := 7.5
	var ant_acc := -stiffness * (_antenna_rot - ant_rest) - damping * _antenna_vel - head_ang_vel * 0.55
	_antenna_vel += ant_acc * dt
	_antenna_rot = clampf(_antenna_rot + _antenna_vel * dt, deg_to_rad(-24.0), deg_to_rad(24.0))
	_antenna_pivot.rotation = _antenna_rot

	# 5. Independent floating hands with out-of-phase levitation & pose targets
	var hl_float := Vector2(sin(_pulse_time * 1.9) * 1.5, sin(_pulse_time * 2.5 + 0.7) * 4.5)
	var hr_float := Vector2(sin(_pulse_time * 1.9 + 1.4) * 1.5, sin(_pulse_time * 2.5 + 2.2) * 4.5)
	var hl_pos := LEFT_HAND_BASE + hl_float
	var hr_pos := RIGHT_HAND_BASE + hr_float
	var hl_rot := sin(_pulse_time * 1.7 + 0.4) * deg_to_rad(4.0)
	var hr_rot := sin(_pulse_time * 1.7 + 2.0) * deg_to_rad(4.0)

	match pose_state:
		"happy":
			hr_pos = Vector2(56.0, -8.0) + hr_float * 0.4
			hr_rot = deg_to_rad(-115.0) + sin(_pulse_time * 11.0) * deg_to_rad(20.0)
			hl_pos = LEFT_HAND_BASE + Vector2(-2.0, 2.0) + hl_float * 0.7
		"thinking":
			hr_pos = Vector2(26.0, 4.0) + hr_float * 0.25
			hr_rot = deg_to_rad(-130.0)
			hl_pos = Vector2(-46.0, 22.0) + hl_float * 0.5
			hl_rot = deg_to_rad(-10.0)
		"working":
			var work_bob := sin(_pulse_time * 9.0) * 3.0
			hl_pos = Vector2(-34.0, 16.0 + work_bob)
			hr_pos = Vector2(34.0, 16.0 - work_bob)
			hl_rot = deg_to_rad(-55.0)
			hr_rot = deg_to_rad(55.0)
		"alert":
			hr_pos = Vector2(54.0, -4.0) + hr_float * 0.35
			hr_rot = deg_to_rad(-118.0) + sin(_pulse_time * 4.5) * deg_to_rad(6.0)
		"sorry":
			hl_pos = Vector2(-38.0, 25.0) + hl_float * 0.4
			hr_pos = Vector2(38.0, 25.0) + hr_float * 0.4
			hl_rot = deg_to_rad(-28.0)
			hr_rot = deg_to_rad(28.0)
		"receiving":
			hl_pos = Vector2(-64.0, 4.0) + hl_float * 0.5
			hr_pos = Vector2(64.0, 4.0) + hr_float * 0.5
			hl_rot = deg_to_rad(85.0) + sin(_pulse_time * 4.5) * deg_to_rad(8.0)
			hr_rot = deg_to_rad(-85.0) - sin(_pulse_time * 4.5) * deg_to_rad(8.0)

	var h_smooth := minf(1.0, dt * 11.0)
	_hand_left.position = _hand_left.position.lerp(hl_pos, h_smooth)
	_hand_right.position = _hand_right.position.lerp(hr_pos, h_smooth)
	_hand_left.rotation = lerpf(_hand_left.rotation, hl_rot, h_smooth)
	_hand_right.rotation = lerpf(_hand_right.rotation, hr_rot, h_smooth)

	# 6. Update procedural LED face state
	_face_display.set("expression", pose_state)
	_face_display.set("gaze", _cur_gaze)
	_face_display.set("is_speaking", _is_tts_speaking)
	_face_display.set("rms", _rms)


func _draw() -> void:
	# Companion breathing status ring inversely coupled with levitation height
	var ring_state := _active_pose_state()
	if _is_tts_speaking:
		ring_state = "speaking"
	var base_col: Color = STATE_RING_COLORS.get(ring_state, STATE_RING_COLORS["idle"])
	var breath := 0.5 + 0.5 * sin(_pulse_time * (5.2 if _is_tts_speaking else 2.2))
	var lift_comp := clampf(-_cur_offset.y / 12.0, -0.5, 1.0)
	var center := Vector2(size.x * 0.5, size.y * 0.90)
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
					_poke_happy_timer = 1.6
					trigger_bounce(1.0)
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
	trigger_bounce(0.9)
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
	var avatar_rect := Rect2(position + Vector2(30.0, 14.0), size - Vector2(60.0, 16.0))
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
