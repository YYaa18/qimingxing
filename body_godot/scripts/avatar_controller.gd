extends Control

signal avatar_clicked
signal context_menu_requested
signal files_dropped_queued(paths: PackedStringArray)
signal scale_changed(new_scale: float)
signal dock_state_changed(edge: String, tucked: bool)

const FACE_DISPLAY_SCRIPT := preload("res://scripts/face_display.gd")
const PREFS_PATH := "user://qimingxing_body_prefs.cfg"

const MIN_SCALE := 0.50
const MAX_SCALE := 1.30
const DEFAULT_SCALE := 0.78

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

var _avatar_scale: float = DEFAULT_SCALE
var _base_state: String = "idle"
var _is_tts_speaking: bool = false
var _interaction_active: bool = false
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

# Spring physics & gaze state
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

# Autonomous desktop roaming state
var _roam_enabled: bool = true
var _roam_state: String = "rest"
var _roam_wait_timer: float = 4.0
var _roam_target_win_pos: Vector2 = Vector2.ZERO
var _roam_velocity: Vector2 = Vector2.ZERO
var _win_pos_f: Vector2 = Vector2.ZERO
var _win_pos_inited: bool = false

# Screen-edge auto-hide ("贴边隐藏") state
var _dock_edge: String = "" # "" | "left" | "right" | "top"
var _dock_peeking: bool = false
var _dock_retract_timer: float = 0.0
var _dock_shown_pos: Vector2 = Vector2.ZERO
var _dock_hidden_pos: Vector2 = Vector2.ZERO
var _last_reported_tucked: bool = false

var _receiving_hold_timer: float = 0.0
var _hide_failsafe_timer: float = 0.0
var _ui_interactive_rects: Array[Rect2] = []

const RIG_ANCHOR := Vector2(130.0, 146.0)
const LEFT_HAND_BASE := Vector2(-58.0, 17.0)
const RIGHT_HAND_BASE := Vector2(57.0, 17.0)


func _ready() -> void:
	_load_prefs()
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
			var start_pos := Vector2i(
				usable.position.x + usable.size.x - win.size.x - 42,
				usable.position.y + usable.size.y - win.size.y - 36
			)
			win.position = start_pos
			_win_pos_f = Vector2(start_pos)
			_win_pos_inited = true
		else:
			_win_pos_f = Vector2(win.position)
			_win_pos_inited = true

	call_deferred("refresh_mouse_passthrough")


func _load_prefs() -> void:
	var cfg := ConfigFile.new()
	if cfg.load(PREFS_PATH) == OK:
		_avatar_scale = clampf(float(cfg.get_value("prefs", "avatar_scale", DEFAULT_SCALE)), MIN_SCALE, MAX_SCALE)
		_roam_enabled = bool(cfg.get_value("prefs", "roam_enabled", true))


func _save_prefs() -> void:
	var cfg := ConfigFile.new()
	cfg.set_value("prefs", "avatar_scale", _avatar_scale)
	cfg.set_value("prefs", "roam_enabled", _roam_enabled)
	cfg.save(PREFS_PATH)


func get_avatar_scale() -> float:
	return _avatar_scale


func set_avatar_scale(new_scale: float) -> void:
	var clamped := clampf(new_scale, MIN_SCALE, MAX_SCALE)
	if absf(clamped - _avatar_scale) < 0.005:
		return
	_avatar_scale = clamped
	_save_prefs()
	if _dock_edge != "":
		_recompute_dock_positions()
	refresh_mouse_passthrough()
	scale_changed.emit(_avatar_scale)


func adjust_avatar_scale(delta_scale: float) -> void:
	set_avatar_scale(_avatar_scale + delta_scale)


func is_roam_enabled() -> bool:
	return _roam_enabled


func set_roam_enabled(enabled: bool) -> void:
	_roam_enabled = enabled
	if not enabled:
		_roam_state = "rest"
		_roam_velocity = Vector2.ZERO
	else:
		_roam_wait_timer = 1.8
	_save_prefs()


func reset_to_default_position() -> void:
	var win := get_window()
	if win == null:
		return
	_dock_edge = ""
	_dock_peeking = false
	_last_reported_tucked = false
	_roam_state = "rest"
	_roam_velocity = Vector2.ZERO
	var usable := DisplayServer.screen_get_usable_rect()
	var pos := Vector2i(
		usable.position.x + usable.size.x - win.size.x - 42,
		usable.position.y + usable.size.y - win.size.y - 36
	)
	_win_pos_f = Vector2(pos)
	_win_pos_inited = true
	DisplayServer.window_set_position(pos)
	dock_state_changed.emit("", false)
	trigger_bounce(0.75)


func set_interaction_active(active: bool) -> void:
	_interaction_active = active
	if active and _dock_edge != "":
		_dock_peeking = true
		_dock_retract_timer = 0.8


func is_docked_tucked() -> bool:
	return _dock_edge != "" and not _dock_peeking


func toggle_edge_hide() -> bool:
	if _dock_edge != "":
		reset_to_default_position()
		return false
	var usable := _get_usable_rect()
	var center := _avatar_center_in_window()
	var cur_center_x := _win_pos_f.x + center.x
	var screen_mid_x := usable.position.x + usable.size.x * 0.5
	_dock_edge = "left" if cur_center_x < screen_mid_x else "right"
	_roam_state = "rest"
	_roam_velocity = Vector2.ZERO
	_dock_peeking = false
	_dock_retract_timer = 0.0
	_recompute_dock_positions()
	_win_pos_f = _dock_hidden_pos
	DisplayServer.window_set_position(Vector2i(_win_pos_f))
	_last_reported_tucked = true
	_save_prefs()
	dock_state_changed.emit(_dock_edge, true)
	return true


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
	_rig_root.scale = Vector2(_avatar_scale, _avatar_scale)
	add_child(_rig_root)

	_hand_left = _make_sprite("hand_left", Vector2(-24.0, -8.0))
	_hand_left.position = LEFT_HAND_BASE
	_rig_root.add_child(_hand_left)

	_hand_right = _make_sprite("hand_right", Vector2(-7.0, -8.0))
	_hand_right.position = RIGHT_HAND_BASE
	_rig_root.add_child(_hand_right)

	_body_sprite = _make_sprite("body", Vector2(-48.0, -39.0))
	_body_sprite.position = Vector2(0.0, 38.0)
	_rig_root.add_child(_body_sprite)

	_head_pivot = Node2D.new()
	_head_pivot.name = "HeadPivot"
	_head_pivot.position = Vector2.ZERO
	_rig_root.add_child(_head_pivot)

	_antenna_pivot = Node2D.new()
	_antenna_pivot.name = "AntennaPivot"
	_antenna_pivot.position = Vector2(0.0, -102.0)
	_head_pivot.add_child(_antenna_pivot)

	_antenna_sprite = _make_sprite("antenna", Vector2(-13.0, -28.0))
	_antenna_pivot.add_child(_antenna_sprite)

	_head_sprite = _make_sprite("head_shell", Vector2(-64.0, -104.0))
	_head_pivot.add_child(_head_sprite)

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
	if speaking and _dock_edge != "":
		_dock_peeking = true
		_dock_retract_timer = 0.8
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
	var dt := minf(delta, 0.05)
	_pulse_time += dt
	if _receiving_hold_timer > 0.0:
		_receiving_hold_timer -= dt
	if _poke_happy_timer > 0.0:
		_poke_happy_timer -= dt
	if _hide_failsafe_timer > 0.0:
		_hide_failsafe_timer -= dt
		if _hide_failsafe_timer <= 0.0:
			set_capture_hidden(false)

	var pose_state := _active_pose_state()
	_update_window_motion(dt, pose_state)

	if pose_state == "idle" and _roam_state == "rest" and not is_docked_tucked() and not _dragging and not _is_tts_speaking:
		if _idle_quirk_active > 0.0:
			_idle_quirk_active -= dt
		else:
			_idle_quirk_cooldown -= dt
			if _idle_quirk_cooldown <= 0.0:
				_idle_quirk_cooldown = randf_range(8.5, 14.5)
				if randf() < 0.45:
					trigger_bounce(0.55)
				else:
					_idle_quirk_active = 1.25
					_idle_quirk_dir = -1.0 if randf() < 0.5 else 1.0
	else:
		_idle_quirk_active = 0.0

	_update_blink(dt)
	_update_rig_kinematics(dt, pose_state)
	queue_redraw()


func _avatar_center_in_window() -> Vector2:
	return position + RIG_ANCHOR


func _get_usable_rect() -> Rect2:
	var usable_i := DisplayServer.screen_get_usable_rect()
	return Rect2(Vector2(usable_i.position), Vector2(usable_i.size))


func _is_pointer_near_peek_edge() -> bool:
	if _dock_edge == "":
		return false
	var mp := Vector2(DisplayServer.mouse_get_position())
	var center_screen := _win_pos_f + _avatar_center_in_window()
	var half_w := 92.0 * _avatar_scale
	var half_h := 130.0 * _avatar_scale
	if _dock_edge == "left" or _dock_edge == "right":
		var usable := _get_usable_rect()
		var edge_x := usable.position.x if _dock_edge == "left" else usable.end.x
		var near_x := absf(mp.x - edge_x) <= (58.0 if not _dock_peeking else half_w * 2.2)
		var near_y := absf(mp.y - center_screen.y) <= half_h + 24.0
		return near_x and near_y
	if _dock_edge == "top":
		var usable := _get_usable_rect()
		var near_y := absf(mp.y - usable.position.y) <= (58.0 if not _dock_peeking else half_h * 1.9)
		var near_x := absf(mp.x - center_screen.x) <= half_w + 30.0
		return near_x and near_y
	return false


func _recompute_dock_positions() -> void:
	var usable := _get_usable_rect()
	var center := _avatar_center_in_window()
	var half_w := 82.0 * _avatar_scale
	var top_h := 132.0 * _avatar_scale
	var bot_h := 90.0 * _avatar_scale
	var peek_sliver := maxf(30.0, 40.0 * _avatar_scale)

	var clamped_y := clampf(
		_win_pos_f.y,
		usable.position.y - (center.y - top_h) + 8.0,
		usable.end.y - (center.y + bot_h) - 8.0
	)
	var clamped_x := clampf(
		_win_pos_f.x,
		usable.position.x - (center.x - half_w) + 8.0,
		usable.end.x - (center.x + half_w) - 8.0
	)

	if _dock_edge == "left":
		_dock_shown_pos = Vector2(usable.position.x - (center.x - half_w) + 4.0, clamped_y)
		_dock_hidden_pos = Vector2(usable.position.x - (center.x + half_w - peek_sliver), clamped_y)
	elif _dock_edge == "right":
		_dock_shown_pos = Vector2(usable.end.x - (center.x + half_w) - 4.0, clamped_y)
		_dock_hidden_pos = Vector2(usable.end.x - (center.x - half_w + peek_sliver), clamped_y)
	elif _dock_edge == "top":
		_dock_shown_pos = Vector2(clamped_x, usable.position.y - (center.y - top_h) + 4.0)
		_dock_hidden_pos = Vector2(clamped_x, usable.position.y - (center.y + bot_h - peek_sliver))


func _evaluate_edge_dock() -> void:
	var usable := _get_usable_rect()
	var center := _avatar_center_in_window()
	var half_w := 82.0 * _avatar_scale
	var top_h := 132.0 * _avatar_scale

	var vis_left := _win_pos_f.x + center.x - half_w
	var vis_right := _win_pos_f.x + center.x + half_w
	var vis_top := _win_pos_f.y + center.y - top_h

	var new_edge := ""
	if vis_left <= usable.position.x + 36.0:
		new_edge = "left"
	elif vis_right >= usable.end.x - 36.0:
		new_edge = "right"
	elif vis_top <= usable.position.y + 32.0:
		new_edge = "top"

	var prev_edge := _dock_edge
	_dock_edge = new_edge
	if _dock_edge != "":
		_roam_state = "rest"
		_roam_velocity = Vector2.ZERO
		_recompute_dock_positions()
		_dock_peeking = false
		_dock_retract_timer = 0.25
		_last_reported_tucked = true
		dock_state_changed.emit(_dock_edge, true)
	elif prev_edge != "":
		_dock_peeking = false
		_last_reported_tucked = false
		dock_state_changed.emit("", false)


func _pick_next_roam_target() -> void:
	var usable := _get_usable_rect()
	var center := _avatar_center_in_window()
	var half_w := 86.0 * _avatar_scale
	var top_h := 136.0 * _avatar_scale
	var bot_h := 96.0 * _avatar_scale

	var min_x := usable.position.x - (center.x - half_w) + 48.0
	var max_x := usable.end.x - (center.x + half_w) - 48.0
	var min_y := usable.position.y - (center.y - top_h) + 48.0
	var max_y := usable.end.y - (center.y + bot_h) - 24.0
	if min_x >= max_x or min_y >= max_y:
		_roam_target_win_pos = _win_pos_f
		return

	var step_x := randf_range(-340.0, 340.0)
	if absf(step_x) < 95.0:
		step_x = 150.0 if randf() < 0.5 else -150.0
	var step_y := randf_range(-110.0, 110.0)
	var prefer_low_min_y := lerpf(min_y, max_y, 0.30)

	var tx := clampf(_win_pos_f.x + step_x, min_x, max_x)
	var ty := clampf(_win_pos_f.y + step_y, prefer_low_min_y, max_y)
	_roam_target_win_pos = Vector2(tx, ty)


func _update_window_motion(dt: float, pose_state: String) -> void:
	var win := get_window()
	if win == null:
		return
	if not _win_pos_inited:
		_win_pos_f = Vector2(win.position)
		_win_pos_inited = true
	if _dragging:
		_roam_velocity = Vector2.ZERO
		return

	if _dock_edge != "":
		_roam_velocity = Vector2.ZERO
		var want_peek := (
			_is_hovered
			or _is_pointer_near_peek_edge()
			or _is_tts_speaking
			or _interaction_active
			or _receiving_hold_timer > 0.0
			or pose_state == "alert"
		)
		if want_peek:
			_dock_peeking = true
			_dock_retract_timer = 0.65
		else:
			if _dock_retract_timer > 0.0:
				_dock_retract_timer -= dt
			if _dock_retract_timer <= 0.0:
				_dock_peeking = false

		var tucked_now := not _dock_peeking
		if tucked_now != _last_reported_tucked:
			_last_reported_tucked = tucked_now
			dock_state_changed.emit(_dock_edge, tucked_now)

		var target := _dock_shown_pos if _dock_peeking else _dock_hidden_pos
		var next_pos := _win_pos_f.lerp(target, minf(1.0, dt * 11.5))
		if next_pos.distance_to(target) < 0.6:
			next_pos = target
		_win_pos_f = next_pos
		DisplayServer.window_set_position(Vector2i(roundi(_win_pos_f.x), roundi(_win_pos_f.y)))
		return

	var can_roam := (
		_roam_enabled
		and not _is_hovered
		and not _interaction_active
		and not _is_tts_speaking
		and _poke_happy_timer <= 0.0
		and pose_state == "idle"
	)
	if not can_roam:
		_roam_velocity = _roam_velocity.lerp(Vector2.ZERO, minf(1.0, dt * 8.0))
		if _roam_state == "walk":
			_roam_state = "rest"
			_roam_wait_timer = 3.5
		return

	if _roam_state == "rest":
		_roam_velocity = _roam_velocity.lerp(Vector2.ZERO, minf(1.0, dt * 8.0))
		_roam_wait_timer -= dt
		if _roam_wait_timer <= 0.0:
			_pick_next_roam_target()
			if _roam_target_win_pos.distance_to(_win_pos_f) > 20.0:
				_roam_state = "walk"
			else:
				_roam_wait_timer = 4.0
	else:
		var to_target := _roam_target_win_pos - _win_pos_f
		var dist := to_target.length()
		if dist <= 4.0:
			_win_pos_f = _roam_target_win_pos
			DisplayServer.window_set_position(Vector2i(roundi(_win_pos_f.x), roundi(_win_pos_f.y)))
			_roam_state = "rest"
			_roam_wait_timer = randf_range(5.0, 11.0)
			_roam_velocity = Vector2.ZERO
			return
		var dir := to_target / dist
		var speed := clampf(dist * 1.6, 26.0, 96.0)
		var desired_vel := dir * speed
		_roam_velocity = _roam_velocity.lerp(desired_vel, minf(1.0, dt * 5.0))
		_win_pos_f += _roam_velocity * dt
		DisplayServer.window_set_position(Vector2i(roundi(_win_pos_f.x), roundi(_win_pos_f.y)))


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
	var head_screen := Vector2(win.position) + position + RIG_ANCHOR + Vector2(0.0, -40.0 * _avatar_scale)
	var diff := global_mouse - head_screen
	return Vector2(
		clampf(diff.x / 460.0, -1.0, 1.0),
		clampf(diff.y / 380.0, -1.0, 1.0)
	)


func _update_rig_kinematics(dt: float, pose_state: String) -> void:
	_hover_weight = move_toward(_hover_weight, 1.0 if (_is_hovered and not _dragging) else 0.0, dt * 5.5)

	var is_walking := _roam_state == "walk" and _roam_velocity.length() > 8.0
	var walk_dir_x := clampf(_roam_velocity.x / 85.0, -1.0, 1.0) if is_walking else 0.0

	var target_gaze := _compute_global_pointer_offset()
	if is_walking:
		target_gaze = Vector2(clampf(_roam_velocity.x / 65.0, -0.85, 0.85), clampf(_roam_velocity.y / 90.0, -0.35, 0.35))
	elif pose_state == "thinking":
		target_gaze = Vector2(0.65, -0.55) + target_gaze * 0.25
	elif pose_state == "sorry":
		target_gaze = Vector2(target_gaze.x * 0.3, 0.55)
	_cur_gaze = _cur_gaze.lerp(target_gaze, minf(1.0, dt * 8.5))

	var float_y := sin(_pulse_time * 2.15) * 5.5
	var breath := sin(_pulse_time * 2.15 - 0.6)
	var root_sx := 1.0 + breath * 0.012
	var root_sy := 1.0 - breath * 0.012
	var root_rot := sin(_pulse_time * 1.1) * deg_to_rad(1.4)

	if is_walking:
		float_y = -absf(sin(_pulse_time * 6.5)) * 7.5
		root_rot = walk_dir_x * deg_to_rad(6.5) + sin(_pulse_time * 6.5) * deg_to_rad(2.0)
	elif pose_state == "working":
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

	_cur_offset = _cur_offset.lerp(Vector2(0.0, float_y * _avatar_scale), minf(1.0, dt * 12.0))
	_rig_root.position = RIG_ANCHOR + _cur_offset
	_rig_root.rotation = lerpf(_rig_root.rotation, root_rot, minf(1.0, dt * 10.0))
	var target_root_scale := Vector2(root_sx * _avatar_scale, root_sy * _avatar_scale)
	_rig_root.scale = _rig_root.scale.lerp(target_root_scale, minf(1.0, dt * 12.0))
	_body_sprite.scale = Vector2(1.0 + breath * 0.015, 1.0 - breath * 0.015)

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

	var ant_rest := sin(_pulse_time * 2.4) * deg_to_rad(3.0) - new_head_rot * 0.25 - walk_dir_x * deg_to_rad(9.0)
	var stiffness := 130.0
	var damping := 7.5
	var ant_acc := -stiffness * (_antenna_rot - ant_rest) - damping * _antenna_vel - head_ang_vel * 0.55
	_antenna_vel += ant_acc * dt
	_antenna_rot = clampf(_antenna_rot + _antenna_vel * dt, deg_to_rad(-24.0), deg_to_rad(24.0))
	_antenna_pivot.rotation = _antenna_rot

	var hl_float := Vector2(sin(_pulse_time * 1.9) * 1.5, sin(_pulse_time * 2.5 + 0.7) * 4.5)
	var hr_float := Vector2(sin(_pulse_time * 1.9 + 1.4) * 1.5, sin(_pulse_time * 2.5 + 2.2) * 4.5)
	var hl_pos := LEFT_HAND_BASE + hl_float
	var hr_pos := RIGHT_HAND_BASE + hr_float
	var hl_rot := sin(_pulse_time * 1.7 + 0.4) * deg_to_rad(4.0)
	var hr_rot := sin(_pulse_time * 1.7 + 2.0) * deg_to_rad(4.0)

	if is_walking:
		var stride := sin(_pulse_time * 6.5)
		hl_pos = LEFT_HAND_BASE + Vector2(stride * 6.0, -absf(stride) * 4.0)
		hr_pos = RIGHT_HAND_BASE + Vector2(-stride * 6.0, -absf(stride) * 4.0)
		hl_rot = deg_to_rad(stride * 22.0)
		hr_rot = deg_to_rad(-stride * 22.0)
	else:
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

	_face_display.set("expression", pose_state)
	_face_display.set("gaze", _cur_gaze)
	_face_display.set("is_speaking", _is_tts_speaking)
	_face_display.set("rms", _rms)


func _draw() -> void:
	var ring_state := _active_pose_state()
	if _is_tts_speaking:
		ring_state = "speaking"
	var base_col: Color = STATE_RING_COLORS.get(ring_state, STATE_RING_COLORS["idle"])
	var breath := 0.5 + 0.5 * sin(_pulse_time * (5.2 if _is_tts_speaking else 2.2))
	var lift_comp := clampf(-_cur_offset.y / maxf(1.0, 12.0 * _avatar_scale), -0.5, 1.0)
	var center := RIG_ANCHOR + Vector2(0.0, 88.0 * _avatar_scale)
	var rx := (72.0 - lift_comp * 6.0 + breath * 6.0 + _rms * 14.0 + _hover_weight * 5.0) * _avatar_scale
	var ry := (20.0 - lift_comp * 1.5 + breath * 2.2) * _avatar_scale

	var pts := PackedVector2Array()
	var segs := 44
	for i in range(segs + 1):
		var ang := TAU * float(i) / float(segs)
		pts.append(Vector2(center.x + cos(ang) * rx, center.y + sin(ang) * ry))

	var outer_col := Color(base_col.r, base_col.g, base_col.b, base_col.a * (0.48 + 0.42 * breath))
	draw_polyline(pts, outer_col, maxf(2.0, 3.0 * _avatar_scale), true)

	var inner_col := Color(base_col.r, base_col.g, base_col.b, 0.14 + 0.10 * breath)
	draw_colored_polygon(pts, inner_col)

	if ring_state in ["working", "thinking", "speaking"]:
		var arc_pts := PackedVector2Array()
		var arc_start := _pulse_time * (3.6 if ring_state == "working" else 2.0)
		for i in range(14):
			var ang := arc_start + (PI * 0.55) * (float(i) / 13.0)
			arc_pts.append(Vector2(center.x + cos(ang) * (rx + 5.0 * _avatar_scale), center.y + sin(ang) * (ry + 2.0 * _avatar_scale)))
		draw_polyline(arc_pts, Color(base_col.r, base_col.g, base_col.b, 0.95), 2.2, true)

	if is_docked_tucked():
		var tab_col := Color(0.32, 0.92, 1.0, 0.55 + 0.35 * breath)
		if _dock_edge == "left":
			draw_line(RIG_ANCHOR + Vector2(72.0 * _avatar_scale, -38.0 * _avatar_scale), RIG_ANCHOR + Vector2(72.0 * _avatar_scale, 38.0 * _avatar_scale), tab_col, 3.5, true)
		elif _dock_edge == "right":
			draw_line(RIG_ANCHOR + Vector2(-72.0 * _avatar_scale, -38.0 * _avatar_scale), RIG_ANCHOR + Vector2(-72.0 * _avatar_scale, 38.0 * _avatar_scale), tab_col, 3.5, true)
		elif _dock_edge == "top":
			draw_line(RIG_ANCHOR + Vector2(-36.0 * _avatar_scale, 76.0 * _avatar_scale), RIG_ANCHOR + Vector2(36.0 * _avatar_scale, 76.0 * _avatar_scale), tab_col, 3.5, true)


func _gui_input(event: InputEvent) -> void:
	if event is InputEventMouseButton:
		var mb := event as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_WHEEL_UP and mb.pressed:
			adjust_avatar_scale(0.06)
			return
		if mb.button_index == MOUSE_BUTTON_WHEEL_DOWN and mb.pressed:
			adjust_avatar_scale(-0.06)
			return
		if mb.button_index == MOUSE_BUTTON_RIGHT and mb.pressed:
			context_menu_requested.emit()
			return
		if mb.button_index == MOUSE_BUTTON_LEFT:
			if mb.pressed:
				_dragging = true
				_drag_moved = false
				_roam_state = "rest"
				_roam_velocity = Vector2.ZERO
				_drag_mouse_start = DisplayServer.mouse_get_position()
				_drag_win_start = DisplayServer.window_get_position()
			else:
				var was_click := _dragging and not _drag_moved
				_dragging = false
				_win_pos_f = Vector2(DisplayServer.window_get_position())
				if was_click:
					_poke_happy_timer = 1.6
					trigger_bounce(1.0)
					avatar_clicked.emit()
				else:
					_evaluate_edge_dock()
	elif event is InputEventMouseMotion and _dragging:
		var cur := DisplayServer.mouse_get_position()
		var diff := cur - _drag_mouse_start
		if diff.length() > 3.0:
			_drag_moved = true
		var next_win := _drag_win_start + diff
		_win_pos_f = Vector2(next_win)
		DisplayServer.window_set_position(next_win)


func _on_window_files_dropped(files: PackedStringArray) -> void:
	if files.is_empty():
		return
	_receiving_hold_timer = 1.8
	if _dock_edge != "":
		_dock_peeking = true
		_dock_retract_timer = 2.0
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
	var half_w := 92.0 * _avatar_scale
	var top_h := 134.0 * _avatar_scale
	var bot_h := 96.0 * _avatar_scale
	var center := position + RIG_ANCHOR
	var avatar_rect := Rect2(
		Vector2(center.x - half_w, center.y - top_h),
		Vector2(half_w * 2.0, top_h + bot_h)
	)
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
